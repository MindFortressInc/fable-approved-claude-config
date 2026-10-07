---
name: scorecard
description: Weekly automation quality report for the fleet (bulldozer, babysit-prs, PRlaunch, wrapup). Reads the durable automation ledger and grades shipping, sweeping, and gate integrity window-over-window, then posts the report as a comment on the standing Linear ticket. Use for "/scorecard", "automation scorecard", "how is the fleet doing", "weekly automation quality report".
argument-hint: "[--days N] (default 7) [--no-gh] [--gh-owner OWNER]"
allowed-tools:
  - Bash
  - Read
  - Write
  - mcp__linear__list_issues
  - mcp__linear__list_projects
  - mcp__linear__save_issue
  - mcp__linear__save_comment
  - mcp__linear__list_comments
---

<objective>
Grade how the automation fleet is doing. `scorecard.py` reads the durable ledger
(`~/.claude/automation-ledger.jsonl`, appended by `hooks/ledger-append.sh` at every
bulldozer/babysit/PRlaunch/wrapup call site) and computes, over a `--days` window vs
the prior equal window: bulldozer shipped/resolved/failed + merged-vs-open-vs-closed
outcomes for shipped PRs; babysit sweeps/bumps/fixes/red_ci; PRlaunch gate integrity
(cr_cli skips, outcome_eval N/A, PRLAUNCH_SKIP uses); cleanup-queue depth; and stale
prlaunch-ok orphans. It flags any metric that regressed >20% window-over-window.

This exists because the fleet's run state otherwise lives only in `/tmp` and is lost
— and when the driver model changes, you need a standing measurement loop to tell
whether quality held.
</objective>

<process>

## Step 1 — Run the scorecard

```bash
python3 ~/.claude/skills/scorecard/scorecard.py ${ARGUMENTS:-}
```

- Default window is 7 days; pass `--days N` (forwarded via `$ARGUMENTS`) to widen.
- The script shells out to `gh` (read-only) to resolve each shipped PR's current
  state. Ledger rows that record a bare repo name (no `owner/`) need an owner to
  resolve against: pass `--gh-owner OWNER` or set `$SCORECARD_GH_OWNER`; without
  one, those PRs count as `unknown` rather than guessing. If `gh` is
  unavailable/slow this session, re-run with `--no-gh` (PR-state enrichment is
  skipped, everything else still computes).
- It degrades gracefully: an absent/partial ledger prints "no data yet — ledger
  empty" per section, never a traceback.

Capture the full markdown output — that IS the report you post in Step 3.

## Step 2 — Find (or create once) the standing scorecard ticket

The report is posted as a COMMENT on ONE standing ticket so the weekly cadence
threads in one place. Where it lives is config, not code:
`~/.claude/skills/scorecard/state.json` (gitignored runtime state — copy
`state.example.json` next to it to start). Resolve the ticket id in this order:

1. **Local cache first:** if `state.json` has a non-empty `issue_id`, use that id —
   skip the search.
2. **Otherwise search Linear:** `mcp__linear__list_issues` with
   `query: "Automation scorecard — standing"`, scoped to `state.json`'s `project`
   (resolve the project id via `mcp__linear__list_projects` if needed). If a match
   comes back, that's the ticket — cache its id (Step 4).
3. **Only if BOTH come back empty, create it once:** `mcp__linear__save_issue`
   with `state.json`'s `team` and `project`, title
   **"Automation scorecard — standing (weekly reports as comments)"**, and a one-line
   description ("Standing home for weekly `/scorecard` reports — each run posts a
   comment; the ticket itself stays open."). Cache the returned id (Step 4).

If `state.json` has no `team`/`project` and there is no cached `issue_id`, ask the
owner which team and project the standing ticket belongs to — don't guess one.
Never create a second scorecard ticket — the cache + search exist precisely to keep
it singular. If unsure whether one exists, search before creating.

## Step 3 — Post the report as a comment

`mcp__linear__save_comment` on the standing ticket's id, body = the exact markdown
from Step 1. Prepend the run date as an H3 (e.g. `### 2026-07-06`) so the thread reads
chronologically. Do NOT edit the ticket description — always a fresh comment.

## Step 4 — Remember the ticket id + suggest scheduling

Write the resolved id back into `state.json` (keep its `team`/`project` keys) so
future runs skip the search:

```bash
python3 - <<'EOF'
import json, os
p = os.path.expanduser("~/.claude/skills/scorecard/state.json")
try:
    s = json.load(open(p))
except Exception:
    s = {}
s["issue_id"] = "<the-issue-id>"
json.dump(s, open(p, "w"))
EOF
```

Then close your reply with this suggestion line (do NOT create the schedule yourself):

> _Tip: to run this weekly, `/schedule` a Monday-morning cloud agent with prompt
> `/scorecard`, or fire it headless from launchd with
> `launchd/headless-skill.sh scorecard 1800 /scorecard` (see `launchd/examples/`) —
> the owner's call._

</process>

<report>
Reply with: the markdown table + verdict from Step 1, the Linear comment URL
you posted, and the scheduling suggestion line. If you had to fall back to `--no-gh`,
say so (PR-state outcomes were skipped this run).
</report>

<notes>
- The ledger is runtime state (gitignored); it is fine for it to be absent on a fresh
  machine — the script says "no data yet" rather than erroring.
- `scorecard.py`'s ledger parsing + aggregation are pure functions (unit-tested in
  `tests/test_scorecard.py`); only the gh/cleanup/orphan enrichment touches the
  outside world, and `--no-gh` turns the network part off.
- Regression flag = a tracked metric moved >20% the wrong way vs the prior window
  (shipped/fixes down, or failed/red_ci/cr_cli-skips/PRLAUNCH_SKIP up). A metric with
  no prior baseline is never flagged.
</notes>
