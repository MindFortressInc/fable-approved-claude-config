#!/usr/bin/env python3
"""PreToolUse gate: moving a Linear ticket to In Review requires a linked PR.

"In Review" is supposed to mean a PR exists. Linear's GitHub integration
auto-links a PR only for repos the integration covers, so tickets in other
repos (personal forks, config repos) pile up In Review with no PR behind them.

Covers mcp__linear__save_issue updates whose `state` targets In Review:
  - allow if the SAME call carries a `links` entry with a GitHub PR URL
  - else allow if the ticket already has a PR-shaped attachment (Linear API)
  - else deny with the fix spelled out

Known bypass (accepted): raw Bash GraphQL issueUpdate. The Bash-side
automations here only move tickets to In Progress (linear-startwork.sh) or
Deployed (reconcile-ticket.sh, which itself requires merged PRs), so the MCP
path is the one that strands review tickets.

Fail-open by design: parse errors, missing key, or API failures allow the call
rather than blocking unrelated work. Denies only on a positive determination
that no PR link exists. Unconfigured (no API key) it never denies: without a
key the attachment lookup is indeterminate, which allows.

Config via environment (all optional):
  LINEAR_API_KEY             your Linear personal API key, OR
  LINEAR_KEY_FILE            path to a JSON file holding .env.LINEAR_API_KEY
  LINEAR_IN_REVIEW_STATE_ID  UUID of your "In Review" workflow state, matched in
                             addition to the state NAMES "In Review"/"Review"
                             (find it with list_issue_statuses in the Linear MCP)
  LINEAR_API_URL             tracker GraphQL endpoint (default: Linear's)

Install in ~/.claude/settings.json:
  "hooks": { "PreToolUse": [ { "matcher": "mcp__linear__save_issue", "hooks": [
    { "type": "command", "command": "~/.claude/hooks/linear-review-gate.py", "timeout": 10 }
  ] } ] }
"""
import json
import os
import re
import sys
import urllib.request

PR_URL = re.compile(r"github\.com/[^/\s]+/[^/\s]+/pull/\d+")
# State NAMES always match; a state UUID matches only when configured. Exact
# matches only, so "Code Review"-style states on other teams are untouched.
IN_REVIEW = {"in review", "review"}
_STATE_ID = os.environ.get("LINEAR_IN_REVIEW_STATE_ID", "").strip()
if _STATE_ID:
    IN_REVIEW.add(_STATE_ID.lower())
API_URL = os.environ.get("LINEAR_API_URL", "https://api.linear.app/graphql")


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def api_key() -> str:
    key = os.environ.get("LINEAR_API_KEY", "")
    if key:
        return key
    key_file = os.environ.get("LINEAR_KEY_FILE", "")
    if not key_file:
        return ""
    with open(os.path.expanduser(key_file)) as f:
        return (json.load(f).get("env") or {}).get("LINEAR_API_KEY", "")


def has_pr_attachment(issue_ref: str) -> bool | None:
    """True/False if determinable via the Linear API, None on any failure."""
    try:
        key = api_key()
        if not key:
            return None
        q = ('query($id: String!) { issue(id: $id) '
             '{ attachments { nodes { url } } } }')
        body = json.dumps({"query": q, "variables": {"id": issue_ref}}).encode()
        req = urllib.request.Request(
            API_URL, data=body,
            headers={"Authorization": key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
        nodes = data["data"]["issue"]["attachments"]["nodes"]
        return any(PR_URL.search(n.get("url") or "") for n in nodes)
    except Exception:
        return None


try:
    payload = json.load(sys.stdin)
    if payload.get("tool_name", "") != "mcp__linear__save_issue":
        sys.exit(0)
    tool_input = payload.get("tool_input") or {}
    issue_ref = tool_input.get("id")
    state = tool_input.get("state")
    if not issue_ref or not isinstance(state, str):
        sys.exit(0)  # creation or no state change — not this gate's concern
    if state.strip().lower() not in IN_REVIEW:
        sys.exit(0)

    links = tool_input.get("links") or []
    if any(PR_URL.search((lk.get("url") or "")) for lk in links
           if isinstance(lk, dict)):
        sys.exit(0)  # PR link travels with this very call

    linked = has_pr_attachment(str(issue_ref))
    if linked is None or linked:
        sys.exit(0)  # has a PR, or API indeterminate → fail open

    deny(
        f"Blocked: moving {issue_ref} to In Review with no linked PR. "
        "In Review means a PR exists. Open the PR first, then either re-run "
        "save_issue with links:[{url:'https://github.com/<owner>/<repo>/pull/<n>', "
        "title:'PR #<n>'}] in the SAME call, or attach the PR to the ticket "
        "before the state move. (Linear's GitHub integration only auto-links "
        "PRs in repos it covers.)"
    )
except SystemExit:
    raise
except Exception:
    sys.exit(0)  # fail-open
