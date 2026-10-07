---
name: bulldozer-reconcile
description: Board-parameterized reconciler for a "Bulldozer 1-offs" epic — walks its open children, best-effort-parses each ticket's recorded file:line + defect pattern from its prose, checks that pattern against the repo's current default branch, and closes ONLY tickets whose premise is confirmed gone (dry-run by default). Use when a board's Bulldozer 1-offs epic has accumulated dead tickets whose findings were already fixed by a later, unrelated PR. Triggers "/bulldozer-reconcile <epic> <repo>", "reconcile the 1-offs epic", "sweep dead bulldozer tickets", "which 1-offs are already fixed".
---

# bulldozer-reconcile — close dead Bulldozer 1-offs automatically

Scope: the read-the-prose reconciler only. Recording a structured "resolution
signature" at filing time, and closing 1-offs on merge from the ship flow
(wrapup/PRlaunch/babysit), are separate follow-ups and deliberately NOT built
here.

## The gap this closes

A CR pass (CR CLI or deep-review) on PR #N files a small out-of-scope
finding as a **CR-deferred 1-off** under that board's standing `Bulldozer
1-offs` epic (see the README's work-taxonomy section). A *later* CR pass on
**that same PR #N**, before it merges, fixes the finding. The PR merges.
**Nothing closes the ticket** — `hooks/reconcile-ticket.sh` only advances a
PR's OWN linked ticket, never a finding it spawned. `/bulldozer` eventually
rediscovers the dead ticket manually, at the cost of a full worker subagent
(~70k tokens) per ticket. Measured hit rate on one wake: **62%** of examined
1-offs were already dead.

This skill automates exactly the check `/bulldozer` was doing by hand: read
the ticket's own prose, check the claim against real code, close it if
(and only if) the claim is provably false now.

## Entry point

```
python3 ~/.claude/skills/bulldozer-reconcile/reconcile.py \
  --epic ENG-123 --repo ~/code/my-repo
```

Add `--live` to actually write to Linear (close + comment). Without it,
**zero writes happen, ever** — dry-run is the default, not an opt-in.

| flag | default | meaning |
|---|---|---|
| `--epic` | required | the board's Bulldozer 1-offs epic — a `TEAM-NUMBER` identifier (e.g. `ENG-123`) or a raw Linear issue UUID |
| `--repo` | required | local path to a clone of the repo the findings live in |
| `--default-branch` | `origin/main` | the git ref treated as "current default branch" |
| `--owner-email` | `$BULLDOZER_OWNER_EMAIL` | the assignee whose tickets (besides unassigned ones) may be closed; unset → unassigned tickets only |
| `--live` | off | perform Linear writes for confirmed-dead tickets; omit for a pure dry-run |
| `--linear-key-file` | `$LINEAR_KEY_FILE` | JSON file holding `.env.LINEAR_API_KEY` — same convention as `skills/assign/assign.py` / `hooks/reconcile-ticket.sh`; `$LINEAR_API_KEY` wins if set |
| `--done-state` | `Done` | the workflow state name to close into |

## How it decides

For each **open** child of the epic:

1. **Assignee filter first.** Only unassigned tickets — or ones assigned to
   the configured owner — are touched. Anyone else's ticket is left
   completely alone (`SKIPPED_ASSIGNEE`) — no read of its content, no
   comment, no status change. Assignee is never stolen.
2. **Parse.** Best-effort extraction of `{file, line_hint, pattern}` from
   the ticket's own description, targeting the 1-offs' "Files:" line
   convention (e.g. `` **Files: **`hooks/pr-gate.sh` (trigger match,
   ~lines 15-21) ``). The defect pattern itself is taken as the FIRST
   sufficiently-long, code-shaped backtick-quoted span in the description —
   these tickets are authored "the bug: ... (`<pattern>`) ..." followed
   *later* by "Demonstrated live" repro quotes / "Suggested fix" examples,
   so first-occurrence (not longest-span) is what actually lands on the
   defect, not a later illustrative quote. If no "Files:" line or no
   pattern-shaped span is found → `UNPARSEABLE`, left open, flagged. **Many
   historical 1-offs will come back UNPARSEABLE** — they predate this
   convention or use a different shape (e.g. a `## Finding` narrative with
   no "Files:" line at all). That is the correct, safe failure mode: a
   missed close costs a stale ticket; a wrong close costs trust in the
   whole mechanism.
3. **Check.** `git show <default-branch>:<file>` and look for the pattern:
   - **Present** → `STILL_PRESENT`, left untouched (no write, no noise).
   - **Absent, file unreadable, or no removal commit found** →
     `AMBIGUOUS`, left untouched. Absence alone is never enough — evidence
     before assertion.
   - **Absent now, but also absent when the ticket was filed** (the file at
     `git rev-list -1 --before=<createdAt> <default-branch>` lacks the
     pattern) → `AMBIGUOUS`. The parser most likely grabbed the wrong span,
     and an old unrelated removal of that string is not this ticket's fix.
   - **Candidate commit whose parent lacks the pattern** → `AMBIGUOUS`: a
     commit can only be cited as the fix if it actually removed the pattern.
   - **Absent, with a specific commit found (`git log <default-branch> -S
     <pattern> -- <file>`) that is confirmed via `git merge-base
     --is-ancestor <sha> <default-branch>`** → `CONFIRMED_GONE`. Restricting
     the pickaxe search to `<default-branch>`'s own history means a fix that
     exists only on an unmerged PR branch can never be mistaken for shipped
     — it just never shows up as a candidate.
4. **Act**, only under `--live` and only for `CONFIRMED_GONE`: post one
   comment citing the exact `file:line` and the fix commit sha, then move
   the ticket to `Done`. Every other verdict writes nothing.

## Validated against

The original dry-run target was a real 1-off whose premise (a naive
quote-stripper in `hooks/pr-gate.sh`) was being rewritten by a still-open PR.
The dry-run correctly reported it `STILL_PRESENT`, not closeable; it becomes
`CONFIRMED_GONE` only once that PR actually merges to `main`. That ticket's
full description shape is kept as a fixture in `tests/test_bulldozer_reconcile.py`.

## Gotchas (learned building this)

- **"Longest code span" is the wrong heuristic — use "first."** The real
  ticket text contains a *longer* backtick span in its "Demonstrated
  live" repro section (`# Scenario 2: shell comment mentioning gh pr
  create...`, 83 chars) than the actual defect snippet quoted earlier
  (`sed -E "s/'[^']*'//g; ..."`, ~40 chars). Picking "longest" silently
  grabbed the repro quote instead of the defect pattern — caught only by
  running the real dry-run against production Linear, not by a trimmed
  unit fixture. The test's `REAL_TICKET_DESCRIPTION` constant keeps the
  full text (not trimmed) for exactly this reason.
- **Bare file paths must be excluded from pattern candidates.** Without
  that filter, a second file mentioned in the same "Files:" line (e.g.
  `` `tests/test_pr_gate.py` ``) can outcompete the real defect pattern on
  length alone in some ticket shapes.
- **`git log -S<pattern>` must be scoped to `<default-branch>`, not "all
  refs."** That is what makes "a fix that exists only on an unmerged PR
  branch counts as still-outstanding" true by construction rather than by
  a separate check — a candidate commit can only ever be found if it is
  already in `<default-branch>`'s own history. `is_ancestor` is kept as an
  explicit, provably-redundant check anyway: it guards a future caller that
  widens the search.
- **`hooks/pr-gate.sh` can false-positive on quoting that ticket's own
  text.** Its "Files:" convention text literally contains the substring
  `` `gh pr create` `` — pasting the raw description into a Bash heredoc for
  local debugging can trip pr-gate's live block (that ticket's own bug,
  reproduced by accident while building its reconciler fixture); route
  around it with `Write` instead of a `Bash` heredoc when the ticket text
  itself must be reproduced verbatim.
- **Assignee-skip happens BEFORE parsing, not after.** Someone else's
  ticket gets zero attention of any kind, not just zero writes — parsing
  their description and computing a verdict nobody will ever see or act on
  is wasted work and a needless read of someone else's active ticket.
- **Never write on `STILL_PRESENT`, `AMBIGUOUS`, or `UNPARSEABLE`, even
  under `--live`.** A real bug left open must generate **zero** comment
  noise, not a "still checking on this" note. Only `CONFIRMED_GONE` ever
  writes.
- **No third-party deps** (urllib + json + subprocess only), matching
  `skills/assign/assign.py` and `hooks/reconcile-ticket.sh`'s conventions —
  same `LINEAR_API_KEY` / `LINEAR_KEY_FILE` credential lookup, same GraphQL
  endpoint.
