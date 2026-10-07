#!/usr/bin/env python3
"""
done_without_evidence.py — recurring "done without evidence" sweep for Linear.

Origin: a bulk close once marked a batch of tickets Done that had no work
behind them, and a one-time audit was needed to find the unbuilt ones. This is
the RECURRING TOOL that catches the same failure mode going forward, so the
next bulk close is caught by a standing check instead of another audit.

Local-only tooling. No third-party deps (urllib + json only, same pattern as
skills/assign/assign.py). Linear API key is read from $LINEAR_API_KEY, or a
JSON file named by $LINEAR_KEY_FILE (holding .env.LINEAR_API_KEY).

What it flags: tickets on the given team in state type "completed" (Done) that
have NONE of the three evidence signals Linear itself carries:
  - startedAt is set        (the issue ever entered a started-type state)
  - a GitHub PR attachment  (Attachment.url matches .../pull/<n>)
  - a GitHub commit attachment (Attachment.url matches .../commit/<sha>)
and are not whitelisted via the `no-code-expected` label (for tickets that
are legitimately done with no code — docs, spec, research, a build-vs-remove
decision). Canceled tickets are never flagged: canceling doesn't claim work
was done, only completing ("Done") does.

The fields/patterns above were checked against the live Linear GraphQL schema
and real closed tickets when this was built: PR/commit attachments carry the
github.com/<org>/<repo>/pull/<n> or /commit/<sha> URL shape regardless of
sourceType (which is generically "api" for both and does not distinguish
them); Issue.startedAt/Attachment.url are real schema fields.

REPORT-ONLY — this script NEVER mutates a ticket's state, assignee, or labels.
Its only writes, and only outside --dry-run, are:
  1. lazily creating the `no-code-expected` LABEL DEFINITION on the team
     if it doesn't exist yet (never applied to any ticket by this script —
     a human applies it by hand to whitelist a specific ticket),
  2. find-or-create ONE standing report ticket (same pattern as
     skills/scorecard: local state.json cache -> Linear search -> create),
  3. posting the sweep result as a COMMENT on that standing ticket.

Commands
  sweep --team NAME [--project NAME] [--limit N] [--dry-run]
      Run the sweep and post the report as a comment on the standing ticket.
      --project names the project the standing ticket is filed under; it is
      only needed the first time (when no standing ticket exists yet).
      --dry-run fetches + prints the report only — no label lookup/creation,
      no standing-ticket search/creation, no comment (proves report-only via
      the CLI, not just via code review).
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_PATH = os.path.join(HERE, "state.json")
GRAPHQL_URL = "https://api.linear.app/graphql"

STANDING_TITLE = "Done-without-evidence sweep — standing (reports as comments)"
WHITELIST_LABEL = "no-code-expected"
STANDING_PRIORITY = 3  # Medium — file every ticket with a project and a priority

# Matches a GitHub PR/commit URL regardless of host owner (personal and org
# repos both appear in a typical workspace).
PR_RE = re.compile(r"github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)
COMMIT_RE = re.compile(r"github\.com/[^/\s]+/[^/\s]+/commit/[0-9a-f]{7,40}", re.IGNORECASE)


# ---------------------------------------------------------------------------
# credentials / transport (mirrors skills/assign/assign.py)
# ---------------------------------------------------------------------------

def linear_key():
    k = os.environ.get("LINEAR_API_KEY")
    if k:
        return k
    kf = os.environ.get("LINEAR_KEY_FILE")
    if kf:
        try:
            with open(os.path.expanduser(kf)) as f:
                return json.load(f).get("env", {}).get("LINEAR_API_KEY")
        except Exception:
            return None
    return None


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def gql(query, variables=None):
    key = linear_key()
    if not key:
        die("No LINEAR_API_KEY (checked $LINEAR_API_KEY and $LINEAR_KEY_FILE).")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={"Authorization": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        die(f"Linear HTTP {e.code}: {e.read().decode()[:500]}")
    except urllib.error.URLError as e:
        die(f"Linear network error: {e}")
    if payload.get("errors"):
        die("Linear GraphQL errors: " + json.dumps(payload["errors"])[:800])
    return payload["data"]


# ---------------------------------------------------------------------------
# pure classification — unit-tested, no network (tests/test_done_without_evidence.py)
# ---------------------------------------------------------------------------

def is_closed(issue):
    """Only state type "completed" (Done) counts as the claim this sweep audits.
    Canceled tickets don't claim work was done, so they're never flagged."""
    return (issue.get("state") or {}).get("type") == "completed"


def has_started(issue):
    return bool(issue.get("startedAt"))


def _attachment_urls(issue):
    return [a.get("url") or "" for a in (issue.get("attachments") or {}).get("nodes", [])]


def has_pr_evidence(issue):
    return any(PR_RE.search(u) for u in _attachment_urls(issue))


def has_commit_evidence(issue):
    return any(COMMIT_RE.search(u) for u in _attachment_urls(issue))


def is_whitelisted(issue, label=WHITELIST_LABEL):
    label_l = label.lower()
    names = (n.get("name") or "" for n in (issue.get("labels") or {}).get("nodes", []))
    return any(n.lower() == label_l for n in names)


def classify_issue(issue, label=WHITELIST_LABEL):
    """Return a violation dict for a closed issue with none of the three
    evidence signals and not whitelisted, else None."""
    if not is_closed(issue):
        return None
    if has_started(issue):
        return None
    if has_pr_evidence(issue) or has_commit_evidence(issue):
        return None
    if is_whitelisted(issue, label):
        return None
    return {
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "state": (issue.get("state") or {}).get("name"),
    }


def sweep_issues(issues, label=WHITELIST_LABEL):
    """Pure filter: list of issue dicts -> list of violation dicts."""
    out = []
    for iss in issues:
        v = classify_issue(iss, label)
        if v:
            out.append(v)
    return out


def render_report(violations, team_name, now, truncated_at=None):
    """Pure markdown rendering — no network, no filesystem. `truncated_at` is
    the number of issues scanned when the fetch stopped with more remaining;
    the report then says so, so a partial sweep can never read as "clean"."""
    lines = []
    lines.append(f"### Done-without-evidence sweep — {team_name}")
    lines.append(f"_generated {now.strftime('%Y-%m-%d %H:%MZ')}_")
    lines.append("")
    if truncated_at:
        lines.append(
            f"⚠ **Partial sweep:** stopped after {truncated_at} closed issue(s) with more "
            "remaining — older tickets were NOT examined."
        )
        lines.append("")
    if not violations:
        lines.append(
            "**Result:** clean — no closed ticket lacks started/PR/commit evidence."
        )
        return "\n".join(lines)
    lines.append(
        f"Found **{len(violations)}** closed ticket(s) with no started state, no PR, "
        f"and no commit (and not labeled `{WHITELIST_LABEL}`):"
    )
    lines.append("")
    lines.append("| Ticket | State | Title |")
    lines.append("|---|---|---|")
    for v in violations:
        title = (v["title"] or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        lines.append(f"| [{v['identifier']}]({v['url']}) | {v['state']} | {title} |")
    lines.append("")
    lines.append(
        f"Apply the `{WHITELIST_LABEL}` label to any of these that are legitimately done "
        "with no code (docs/spec/research/build-vs-remove decisions), or reopen the rest — "
        "this sweep never changes ticket state itself."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# state.json cache — filesystem only (same shape as skills/scorecard)
# ---------------------------------------------------------------------------

def load_state(path=DEFAULT_STATE_PATH):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f)
        f.write("\n")


# ---------------------------------------------------------------------------
# live Linear calls — every call takes gql_fn so tests can stub it (no
# network in the test path); production callers omit it and get `gql` above.
# ---------------------------------------------------------------------------

CLOSED_ISSUES_QUERY = """
query ClosedIssues($team: String!, $after: String) {
  issues(filter: {team: {name: {eq: $team}}, state: {type: {eq: "completed"}}},
         first: 100, after: $after, orderBy: updatedAt) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id identifier title url startedAt
      state { name type }
      labels { nodes { name } }
      attachments { nodes { url } }
    }
  }
}
"""


MAX_PAGES = 50  # backstop: 5,000 issues


def fetch_closed_issues(team_name, limit=None, gql_fn=gql):
    """Return (issues, truncated). `truncated` is True when the fetch stopped —
    at --limit or at the MAX_PAGES backstop — while more issues remained."""
    out, after, pages = [], None, 0
    while True:
        data = gql_fn(CLOSED_ISSUES_QUERY, {"team": team_name, "after": after})
        blk = data["issues"]
        out.extend(blk["nodes"])
        pages += 1
        more = blk["pageInfo"]["hasNextPage"]
        if limit and len(out) >= limit:
            return out[:limit], (len(out) > limit or more)
        if not more:
            return out, False
        if pages >= MAX_PAGES:
            return out, True
        after = blk["pageInfo"]["endCursor"]


TEAM_ID_QUERY = """
query TeamId($team: String!) { teams(filter: {name: {eq: $team}}) { nodes { id } } }
"""

TEAM_WITH_PROJECTS_QUERY = """
query TeamWithProjects($team: String!) {
  teams(filter: {name: {eq: $team}}) {
    nodes { id projects { nodes { id name } } }
  }
}
"""

# Case-insensitive, and includes workspace-scoped labels (team: null): Linear
# rejects creating a team label whose name collides with either.
LABEL_LOOKUP_QUERY = """
query FindLabel($team: String!, $name: String!) {
  issueLabels(filter: {
    name: {eqIgnoreCase: $name},
    or: [{team: {name: {eq: $team}}}, {team: {null: true}}]
  }) { nodes { id name } }
}
"""

LABEL_CREATE_MUT = """
mutation CreateLabel($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) { success issueLabel { id name } }
}
"""


def ensure_label(team_name, label_name=WHITELIST_LABEL, gql_fn=gql):
    """Find-or-lazily-create the whitelist LABEL DEFINITION on the team.
    Never applies the label to any ticket — that's a human's call."""
    data = gql_fn(LABEL_LOOKUP_QUERY, {"team": team_name, "name": label_name})
    nodes = data["issueLabels"]["nodes"]
    if nodes:
        return nodes[0]["id"], False
    tdata = gql_fn(TEAM_ID_QUERY, {"team": team_name})
    tnodes = tdata["teams"]["nodes"]
    if not tnodes:
        die(f"team '{team_name}' not found")
    team_id = tnodes[0]["id"]
    cdata = gql_fn(LABEL_CREATE_MUT, {"input": {
        "teamId": team_id,
        "name": label_name,
        "description": (
            "Ticket is legitimately done with no code (docs/spec/research/"
            "build-vs-remove decision) — whitelists it out of the "
            "done-without-evidence sweep."
        ),
    }})
    res = cdata["issueLabelCreate"]
    if not res["success"]:
        die("issueLabelCreate returned success=false")
    return res["issueLabel"]["id"], True


FIND_STANDING_QUERY = """
query FindStanding($team: String!, $title: String!) {
  issues(filter: {team: {name: {eq: $team}}, title: {eq: $title}}, first: 5) {
    nodes { id identifier url }
  }
}
"""

CREATE_STANDING_MUT = """
mutation CreateStanding($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier url } }
}
"""


def find_or_create_standing_ticket(team_name, gql_fn=gql, state_path=DEFAULT_STATE_PATH,
                                    project_name=None, title=STANDING_TITLE):
    """Find-or-create the ONE standing ticket sweep reports post to, caching
    its id in state.json (same pattern as skills/scorecard). A cache hit
    short-circuits — no Linear call at all. `project_name` is required only
    when the ticket has to be created."""
    state = load_state(state_path)
    # The cache is scoped to the team it was resolved for — a run on another
    # team must never post onto this team's standing ticket.
    if state.get("issue_id") and state.get("team") == team_name:
        return state["issue_id"], state.get("identifier"), state.get("url")

    data = gql_fn(FIND_STANDING_QUERY, {"team": team_name, "title": title})
    nodes = data["issues"]["nodes"]
    if nodes:
        iss = nodes[0]
        save_state(state_path, {"team": team_name, "issue_id": iss["id"],
                                "identifier": iss["identifier"], "url": iss["url"]})
        return iss["id"], iss["identifier"], iss["url"]

    if not project_name:
        die(f"no standing ticket titled '{title}' exists on team '{team_name}' yet — pass "
            "--project NAME so it can be filed (every ticket needs a home)")

    tdata = gql_fn(TEAM_WITH_PROJECTS_QUERY, {"team": team_name})
    tnodes = tdata["teams"]["nodes"]
    if not tnodes:
        die(f"team '{team_name}' not found")
    team = tnodes[0]
    team_id = team["id"]
    project_id = None
    for p in team["projects"]["nodes"]:
        if p["name"] == project_name:
            project_id = p["id"]
            break
    if not project_id:
        die(f"project '{project_name}' not found on team '{team_name}' — cannot file the "
            "standing ticket without a project (every ticket needs a home)")

    cdata = gql_fn(CREATE_STANDING_MUT, {"input": {
        "teamId": team_id,
        "projectId": project_id,
        "priority": STANDING_PRIORITY,
        "title": title,
        "description": (
            "Standing home for `done-without-evidence` sweep reports — each run "
            "posts a comment; the ticket itself stays open."
        ),
    }})
    res = cdata["issueCreate"]
    if not res["success"]:
        die("issueCreate returned success=false")
    iss = res["issue"]
    save_state(state_path, {"team": team_name, "issue_id": iss["id"],
                            "identifier": iss["identifier"], "url": iss["url"]})
    return iss["id"], iss["identifier"], iss["url"]


COMMENT_CREATE_MUT = """
mutation CreateComment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id url } }
}
"""


def post_comment(issue_id, body, gql_fn=gql):
    data = gql_fn(COMMENT_CREATE_MUT, {"input": {"issueId": issue_id, "body": body}})
    res = data["commentCreate"]
    if not res["success"]:
        die("commentCreate returned success=false")
    return res["comment"]


# ---------------------------------------------------------------------------
# orchestration — the one function the CLI calls; takes gql_fn/state_path so
# it's fully testable without live credentials
# ---------------------------------------------------------------------------

def run_sweep(team, project=None, limit=None, dry_run=False, gql_fn=gql,
              state_path=DEFAULT_STATE_PATH):
    issues, truncated = fetch_closed_issues(team, limit=limit, gql_fn=gql_fn)
    violations = sweep_issues(issues)
    now = datetime.now(timezone.utc)
    report = render_report(violations, team, now, truncated_at=len(issues) if truncated else None)

    result = {
        "report": report,
        "violations": violations,
        "dry_run": dry_run,
        "comment_url": None,
        "standing_ticket": None,
        "label_id": None,
        "label_created": None,
    }
    if dry_run:
        return result

    label_id, label_created = ensure_label(team, gql_fn=gql_fn)
    result["label_id"] = label_id
    result["label_created"] = label_created

    issue_id, identifier, url = find_or_create_standing_ticket(
        team, gql_fn=gql_fn, state_path=state_path, project_name=project)
    result["standing_ticket"] = {"id": issue_id, "identifier": identifier, "url": url}

    comment = post_comment(issue_id, report, gql_fn=gql_fn)
    result["comment_url"] = comment.get("url")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_sweep(args):
    out = run_sweep(team=args.team, project=args.project, limit=args.limit,
                    dry_run=args.dry_run)
    print(out["report"])
    if out["dry_run"]:
        print("\n(--dry-run: nothing written to Linear — no label, no ticket, no comment)")
    else:
        st = out["standing_ticket"]
        print(f"\nposted to {st['identifier']} ({st['url']})")
        if out["comment_url"]:
            print(f"comment: {out['comment_url']}")
        if out["label_created"]:
            print(f"(lazily created the '{WHITELIST_LABEL}' label on team '{args.team}')")


def main():
    p = argparse.ArgumentParser(prog="done_without_evidence.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sweep")
    s.add_argument("--team", required=True, help="Linear team NAME to audit")
    s.add_argument("--project", default=None,
                   help="project NAME the standing report ticket is filed under "
                        "(needed only the first time, when it does not exist yet)")
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_sweep)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
