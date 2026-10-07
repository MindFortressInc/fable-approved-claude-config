#!/usr/bin/env python3
"""
reconcile.py — board-parameterized Bulldozer 1-offs premise reconciler.

Scope: this is the read-the-prose reconciler only. Recording a structured
"resolution signature" at filing time, and closing 1-offs on merge from the
ship flow, are separate follow-ups and deliberately NOT built here.

PROBLEM this closes: a CR-deferred 1-off ticket filed under a board's
standing "Bulldozer 1-offs" epic can be fixed by a LATER, unrelated PR
(often the very same parent PR that spawned it) with nothing ever closing
the ticket. It sits in Backlog describing a defect that no longer exists
until `/bulldozer` burns a full worker subagent (~70k tokens) rediscovering
that fact. Measured hit rate on one wake: 62% of examined 1-offs were
already dead.

WHAT THIS DOES: given a board's "Bulldozer 1-offs" epic + a local clone of
the repo the findings live in, walk the epic's OPEN children, best-effort
parse each ticket's recorded `file:line` + defect pattern out of its own
prose (the "Files:" line convention the 1-offs use), check that pattern
against the repo's CURRENT DEFAULT BRANCH, and close ONLY the tickets whose
premise is CONFIRMED gone — with a comment citing the exact file:line and
the commit sha that landed the fix, verified via
`git merge-base --is-ancestor <fix-sha> <default-branch>` so a fix that
exists only on an unmerged PR branch is never mistaken for shipped.

SAFETY RAILS (all deliberate, all tested):
  - Dry-run is the DEFAULT. --live is required to write anything.
  - Ambiguous or unparseable tickets are LEFT OPEN, never auto-closed —
    evidence-before-assertion: a wrongly-closed real bug costs far more than
    a ticket left open one more day.
  - Only unassigned tickets — or ones assigned to the configured owner
    (--owner-email / $BULLDOZER_OWNER_EMAIL) — are ever touched. Assignee is
    never stolen, and someone else's active ticket is left alone entirely
    (no comment, no status change).
  - A "still real" or "ambiguous" ticket gets ZERO Linear writes — no state
    change, no comment noise. Only a CONFIRMED_GONE + assignee-eligible
    ticket is ever written to, and only under --live.
  - The parser is best-effort BY DESIGN. Many historical 1-off tickets do
    NOT use the clean "Files:" line shape this parser targets (e.g. a
    "## Finding" narrative with no "Files:" line at all) — those correctly
    come back UNPARSEABLE and are left untouched. This is the intended, safe
    failure mode: a missed close costs a stale ticket, a wrong close costs
    trust in the whole mechanism.

USAGE:
  python3 reconcile.py --epic ENG-123 --repo ~/code/my-repo
  python3 reconcile.py --epic ENG-123 --repo ~/code/my-repo --live

Credentials: $LINEAR_API_KEY, else a JSON file named by --linear-key-file /
$LINEAR_KEY_FILE (holding .env.LINEAR_API_KEY) — the same convention as
skills/assign/assign.py and hooks/reconcile-ticket.sh.
No third-party deps: urllib + json + subprocess only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from typing import List, Optional

GRAPHQL_URL = "https://api.linear.app/graphql"
# The assignee whose tickets (besides unassigned ones) the reconciler may
# close. Unset → only unassigned tickets are eligible.
OWNER_EMAIL_DEFAULT = os.environ.get("BULLDOZER_OWNER_EMAIL")


# ==========================================================================
# Prose parsing — best-effort file:line + defect-pattern extraction
# ==========================================================================

# A "Files:" line, optionally bold-wrapped in any combination
# (`Files:`, `**Files:**`, `**Files: **`, ...), capturing the rest of the line.
FILES_LINE_RE = re.compile(r"(?im)^\s*\*{0,2}\s*files\s*\*{0,2}\s*:\s*\*{0,2}\s*(.+)$")

# A backtick-quoted file path: word chars/slashes/dots/hyphens, ending in a
# dotted extension (so it doesn't also match arbitrary code spans).
FILE_PATH_RE = re.compile(r"`([\w./-]+\.\w+)`")

# A bare file path shape (used to EXCLUDE plain paths from pattern candidates).
BARE_PATH_RE = re.compile(r"^[\w./-]+\.\w+$")

# "lines 15-21" / "~lines 15-21" / "line 42" / "lines ~15-21", tilde on
# either side of the word, tolerated.
LINE_HINT_RE = re.compile(r"(?i)~?\s*lines?\s*~?\s*(\d+)(?:\s*-\s*(\d+))?")

# Any backtick-quoted span anywhere in the description.
CODE_SPAN_RE = re.compile(r"`([^`]+)`")

MIN_PATTERN_LEN = 10
CODE_HINT_CHARS = set("()$/=;|&<>\"'{}[]")


def _looks_like_code(span: str) -> bool:
    return any(ch in CODE_HINT_CHARS for ch in span)


@dataclass
class Finding:
    identifier: str
    file: str
    line_hint: Optional[str]
    pattern: str


def _select_pattern(description: str, exclude: List[str]) -> Optional[str]:
    candidates = []
    for m in CODE_SPAN_RE.finditer(description):
        span = m.group(1)
        if span in exclude:
            continue
        if BARE_PATH_RE.match(span.strip()):
            continue
        if len(span) < MIN_PATTERN_LEN:
            continue
        candidates.append(span)
    if not candidates:
        return None
    strong = [c for c in candidates if _looks_like_code(c)]
    pool = strong if strong else candidates
    # FIRST occurrence wins, not longest. These 1-off tickets are authored
    # "the bug: ... (`<defect pattern>`) ..." followed LATER by
    # "Demonstrated live" repro quotes / "Suggested fix" examples that also
    # contain code-shaped backtick spans (sometimes longer ones — e.g. a
    # "Scenario 2: shell comment mentioning gh pr create (never executed as
    # a command)" repro quote, at 83 chars, outsizing the real ~40-char
    # defect snippet quoted earlier in the same ticket). Picking "longest"
    # silently grabbed that repro quote instead of the defect pattern on a
    # real ticket — caught by a real dry-run, not by a trimmed unit fixture.
    # First-occurrence matches how these tickets are actually written.
    return pool[0]


def parse_finding(identifier: str, description: Optional[str]) -> Optional[Finding]:
    """Best-effort extraction of {file, line_hint, pattern} from a ticket's
    markdown description. Returns None (unparseable) rather than guessing —
    callers must treat None as "leave open, flag for a human", never as a
    reason to auto-close.
    """
    if not description:
        return None

    files_match = FILES_LINE_RE.search(description)
    if not files_match:
        return None
    files_segment = files_match.group(1)

    file_matches = FILE_PATH_RE.findall(files_segment)
    if not file_matches:
        return None
    primary_file = file_matches[0]

    line_hint = None
    lm = LINE_HINT_RE.search(files_segment)
    if lm:
        line_hint = lm.group(1) if not lm.group(2) else f"{lm.group(1)}-{lm.group(2)}"

    pattern = _select_pattern(description, exclude=file_matches)
    if not pattern:
        return None

    return Finding(identifier=identifier, file=primary_file, line_hint=line_hint, pattern=pattern)


# ==========================================================================
# Git-side premise check
# ==========================================================================

@dataclass
class Verdict:
    status: str  # STILL_PRESENT | CONFIRMED_GONE | AMBIGUOUS
    detail: str
    fix_sha: str = ""


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def get_file_at_ref(repo: str, ref: str, path: str):
    """Return (content, None) or (None, error_message)."""
    proc = _git(repo, "show", f"{ref}:{path}")
    if proc.returncode != 0:
        return None, proc.stderr.strip()
    return proc.stdout, None


def find_removal_commit(repo: str, ref: str, path: str, pattern: str) -> Optional[str]:
    """The most recent commit (within `ref`'s own history) that toggled
    `pattern`'s occurrence count in `path` (git pickaxe, -S). Since the
    caller only calls this once the pattern is confirmed ABSENT at `ref`'s
    tip, the most recent toggle — if any — must be the removal. Restricting
    `git log` to `ref` means a commit that exists only on some OTHER local
    branch (e.g. an unmerged PR branch happening to share this repo's git
    object store) can never surface here.
    """
    proc = _git(repo, "log", ref, "--format=%H", f"-S{pattern}", "--", path)
    if proc.returncode != 0:
        return None
    shas = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return shas[0] if shas else None


def is_ancestor(repo: str, sha: str, ref: str) -> bool:
    proc = _git(repo, "merge-base", "--is-ancestor", sha, ref)
    return proc.returncode == 0


def commit_at(repo: str, ref: str, when: str) -> Optional[str]:
    """The tip of `ref` as of `when` (an ISO-8601 timestamp), or None."""
    proc = _git(repo, "rev-list", "-1", f"--before={when}", ref)
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    return sha or None


def check_premise(repo: str, default_branch: str, finding: Finding,
                  filed_at: Optional[str] = None) -> Verdict:
    """`filed_at` (the ticket's createdAt, when known) requires the pattern to
    have been on `default_branch` when the ticket was filed — so a misparsed
    pattern that merely existed at some point in history can't cite an
    unrelated old removal as "the fix"."""
    content, err = get_file_at_ref(repo, default_branch, finding.file)
    if content is None:
        return Verdict(
            "AMBIGUOUS",
            f"cannot read {finding.file} at {default_branch}: {err}",
        )

    if finding.pattern in content:
        return Verdict(
            "STILL_PRESENT",
            f"pattern still present in {finding.file} at {default_branch}",
        )

    if filed_at:
        base = commit_at(repo, default_branch, filed_at)
        at_filing, _ = get_file_at_ref(repo, base, finding.file) if base else (None, None)
        if at_filing is None or finding.pattern not in at_filing:
            return Verdict(
                "AMBIGUOUS",
                f"pattern was not in {finding.file} on {default_branch} when the ticket "
                f"was filed ({filed_at}) — the parsed pattern may not be the defect, "
                f"so this is NOT auto-closeable",
            )

    fix_sha = find_removal_commit(repo, default_branch, finding.file, finding.pattern)
    if not fix_sha:
        return Verdict(
            "AMBIGUOUS",
            f"pattern absent from {finding.file} at {default_branch}, but no "
            f"removal commit was found via `git log -S` — cannot cite a fix sha, "
            f"so this is NOT auto-closeable",
        )

    # Belt-and-suspenders: `find_removal_commit` already restricted its
    # search to `default_branch`'s own history, so this is provably true —
    # but it is the explicit shipped-proof check, and it is what protects
    # this function against a future caller that widens the search to all
    # refs (e.g. to also look at open-PR branches) without updating this
    # guard.
    if not is_ancestor(repo, fix_sha, default_branch):
        return Verdict(
            "AMBIGUOUS",
            f"candidate fix commit {fix_sha} is not confirmed as an ancestor "
            f"of {default_branch} — treating as still-outstanding",
        )

    # The cited commit must actually be the one that removed the pattern: its
    # parent has to contain it. Anything else is not evidence of a fix.
    before, _ = get_file_at_ref(repo, f"{fix_sha}^", finding.file)
    if before is None or finding.pattern not in before:
        return Verdict(
            "AMBIGUOUS",
            f"candidate {fix_sha} did not remove the pattern from {finding.file} "
            f"(absent from its parent) — cannot cite it as the fix",
        )

    loc = f"{finding.file}:{finding.line_hint}" if finding.line_hint else finding.file
    return Verdict(
        "CONFIRMED_GONE",
        f"{loc} — pattern absent from {default_branch}, fixed by {fix_sha}",
        fix_sha=fix_sha,
    )


# ==========================================================================
# Orchestration
# ==========================================================================

@dataclass
class Ticket:
    id: str
    identifier: str
    title: str
    description: str
    assignee_email: Optional[str] = None
    created_at: Optional[str] = None  # ISO-8601; gates "was this real when filed?"


@dataclass
class Result:
    identifier: str
    verdict: str  # STILL_PRESENT | CONFIRMED_GONE | AMBIGUOUS | UNPARSEABLE | SKIPPED_ASSIGNEE
    detail: str
    action: str = "none"  # "closed" | "none"
    fix_sha: str = ""


def _assignee_eligible(ticket: Ticket, owner_email: Optional[str]) -> bool:
    """Never steal an assignee: only unassigned-or-owner tickets get touched.
    With no owner configured, only unassigned tickets are eligible."""
    if not ticket.assignee_email:
        return True
    if not owner_email:
        return False
    return ticket.assignee_email.strip().lower() == owner_email.strip().lower()


def run_reconcile(client, epic: str, repo: str, default_branch: str,
                   owner_email: Optional[str], live: bool) -> List[Result]:
    epic_uuid = client.resolve_epic(epic)
    tickets = client.list_open_children(epic_uuid)

    results: List[Result] = []
    for ticket in tickets:
        if not _assignee_eligible(ticket, owner_email):
            results.append(Result(
                ticket.identifier, "SKIPPED_ASSIGNEE",
                f"assigned to {ticket.assignee_email} (not unassigned or the owner) — left untouched",
            ))
            continue

        finding = parse_finding(ticket.identifier, ticket.description)
        if finding is None:
            results.append(Result(
                ticket.identifier, "UNPARSEABLE",
                "could not extract a file:line + pattern from the description "
                "(no recognizable 'Files:' line, or no code-shaped span found) "
                "— left open, flagged for a human",
            ))
            continue

        verdict = check_premise(repo, default_branch, finding, filed_at=ticket.created_at)
        action = "none"
        if verdict.status == "CONFIRMED_GONE" and live:
            comment = (
                "bulldozer-reconcile: premise confirmed gone — "
                f"{verdict.detail}. Verified via "
                f"`git merge-base --is-ancestor {verdict.fix_sha} {default_branch}`. "
                "Closing."
            )
            client.close_with_comment(ticket.id, comment)
            action = "closed"

        results.append(Result(
            ticket.identifier, verdict.status, verdict.detail, action, verdict.fix_sha,
        ))

    return results


# ==========================================================================
# Live Linear client (network I/O lives ONLY here — never exercised by tests)
# ==========================================================================

def _read_linear_key(key_file: Optional[str]) -> Optional[str]:
    key = os.environ.get("LINEAR_API_KEY")
    if key:
        return key
    if not key_file:
        return None
    try:
        with open(os.path.expanduser(key_file)) as f:
            return json.load(f).get("env", {}).get("LINEAR_API_KEY")
    except Exception:
        return None


class LiveLinearClient:
    def __init__(self, key_file: Optional[str] = None,
                 done_state_name: str = "Done"):
        self.key = _read_linear_key(key_file)
        self.done_state_name = done_state_name
        # Resolved lazily from whatever epic/ticket is actually fetched (via
        # the `team { id }` field on the GraphQL response) — never taken from
        # a caller-supplied team key, since the epic's own team is always
        # known once `list_open_children` has run once, making a separate
        # "which team" input redundant for `_resolve_done_state_id` below.
        self._team_id = None
        self._done_state_id = None

    def _gql(self, query: str, variables: Optional[dict] = None) -> dict:
        if not self.key:
            raise RuntimeError(
                "No LINEAR_API_KEY (checked $LINEAR_API_KEY and the key file)."
            )
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            GRAPHQL_URL, data=body,
            headers={"Authorization": self.key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Linear HTTP {e.code}: {e.read().decode()[:500]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Linear network error: {e}")
        if payload.get("errors"):
            raise RuntimeError(f"Linear GraphQL error: {json.dumps(payload['errors'])[:800]}")
        return payload["data"]

    def resolve_epic(self, identifier: str) -> str:
        # Already a Linear UUID? Pass through untouched.
        if re.match(r"^[0-9a-f]{8}-[0-9a-f-]{27}$", identifier, re.I):
            return identifier
        if "-" not in identifier:
            raise RuntimeError(
                f"epic identifier {identifier!r} is neither a UUID nor a TEAM-NUMBER identifier"
            )
        team_key, num = identifier.split("-", 1)
        data = self._gql(
            "query($num: Float!, $key: String!) { "
            "issues(filter: {number: {eq: $num}, team: {key: {eq: $key}}}) { "
            "nodes { id } } }",
            {"num": float(num), "key": team_key},
        )
        nodes = data["issues"]["nodes"]
        if not nodes:
            raise RuntimeError(f"epic {identifier} not found")
        return nodes[0]["id"]

    def list_open_children(self, epic_uuid: str) -> List[Ticket]:
        query = (
            "query($id: String!, $after: String) { issue(id: $id) { "
            "team { id key } "
            "children(first: 100, after: $after) { "
            "pageInfo { hasNextPage endCursor } "
            "nodes { id identifier title description createdAt "
            "state { name type } assignee { email } } } } }"
        )
        out: List[Ticket] = []
        after = None
        while True:
            data = self._gql(query, {"id": epic_uuid, "after": after})
            issue = data.get("issue")
            if issue is None:
                # Never let a missing epic read as "no open children" — that
                # renders as a clean report over nothing.
                raise RuntimeError(f"epic {epic_uuid} not found")
            if self._team_id is None:
                self._team_id = issue["team"]["id"]
            conn = issue["children"]
            for n in conn["nodes"]:
                if n["state"]["type"] in ("completed", "canceled"):
                    continue
                out.append(Ticket(
                    id=n["id"],
                    identifier=n["identifier"],
                    title=n["title"],
                    description=n.get("description") or "",
                    assignee_email=(n.get("assignee") or {}).get("email"),
                    created_at=n.get("createdAt"),
                ))
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
        return out

    def _resolve_done_state_id(self) -> str:
        if self._done_state_id:
            return self._done_state_id
        if not self._team_id:
            raise RuntimeError("team id not yet known — call list_open_children first")
        data = self._gql(
            "query($id: String!) { team(id: $id) { states { nodes { id name type } } } }",
            {"id": self._team_id},
        )
        for s in data["team"]["states"]["nodes"]:
            if s["name"].lower() == self.done_state_name.lower():
                self._done_state_id = s["id"]
                return self._done_state_id
        raise RuntimeError(f"no {self.done_state_name!r} state found for this team")

    def close_with_comment(self, issue_id: str, body: str) -> None:
        self._gql(
            "mutation($id: String!, $body: String!) { "
            "commentCreate(input: {issueId: $id, body: $body}) { success } }",
            {"id": issue_id, "body": body},
        )
        done_id = self._resolve_done_state_id()
        self._gql(
            "mutation($id: String!, $sid: String!) { "
            "issueUpdate(id: $id, input: {stateId: $sid}) { success } }",
            {"id": issue_id, "sid": done_id},
        )


# ==========================================================================
# CLI
# ==========================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Board-parameterized Bulldozer 1-offs premise reconciler.",
    )
    p.add_argument("--epic", required=True,
                   help="the board's 'Bulldozer 1-offs' epic — a TEAM-NUMBER identifier "
                        "(e.g. ENG-123) or a raw Linear issue UUID")
    p.add_argument("--repo", required=True,
                   help="local path to a clone of the repo the findings live in")
    p.add_argument("--default-branch", default="origin/main",
                   help="git ref for the repo's current default branch (default: origin/main)")
    p.add_argument("--owner-email", default=OWNER_EMAIL_DEFAULT,
                   help="assignee email whose tickets (besides unassigned ones) may be "
                        "closed (default $BULLDOZER_OWNER_EMAIL; unset → unassigned only)")
    p.add_argument("--live", action="store_true",
                   help="perform Linear writes for confirmed-dead tickets "
                        "(default: dry-run, zero writes)")
    p.add_argument("--linear-key-file", default=os.environ.get("LINEAR_KEY_FILE"),
                   help="JSON file holding .env.LINEAR_API_KEY (default $LINEAR_KEY_FILE; "
                        "$LINEAR_API_KEY wins if set)")
    p.add_argument("--done-state", default="Done")
    return p


def print_report(results: List[Result], live: bool) -> None:
    mode = "LIVE" if live else "DRY-RUN"
    marker = {
        "CONFIRMED_GONE": "✅",
        "STILL_PRESENT": "\U0001F534",
        "AMBIGUOUS": "\U0001F50E",
        "UNPARSEABLE": "❓",
        "SKIPPED_ASSIGNEE": "⏭️",
    }
    print(f"=== bulldozer-reconcile ({mode}) ===")
    for r in results:
        m = marker.get(r.verdict, "•")
        print(f"{m} {r.identifier:<10} {r.verdict:<18} action={r.action:<6} {r.detail}")
    counts = Counter(r.verdict for r in results)
    closed = [r.identifier for r in results if r.action == "closed"]
    print("---")
    print(f"counts: {dict(counts)}")
    print(f"closed: {closed if closed else 'none'}")
    print(json.dumps({"mode": mode, "results": [asdict(r) for r in results]}))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    client = LiveLinearClient(
        key_file=args.linear_key_file,
        done_state_name=args.done_state,
    )
    results = run_reconcile(
        client, epic=args.epic, repo=os.path.expanduser(args.repo),
        default_branch=args.default_branch, owner_email=args.owner_email,
        live=args.live,
    )
    print_report(results, args.live)
    return 0


if __name__ == "__main__":
    sys.exit(main())
