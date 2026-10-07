---
description: Wrap up the session — sync tracker, sync GitHub, clean dirty branches, persist memory, report
---

# Session Wrapup

End-of-session checklist. Work through each step in order, then deliver one consolidated report at the end. Do NOT skip steps. If a step has nothing to do, say so explicitly in the report ("Tracker: nothing to update").

Use TodoWrite to track the 7 steps. Mark each done as you go.

---

## 1. Tracker tickets

Goal: every ticket touched this session reflects current reality.

- Identify tickets referenced or worked on this session (scan conversation for ticket IDs, branch names, PR titles, commits).
- For each ticket: check current state. Compare against what actually happened.
- Update state when reality has moved past the ticket:
  - Code shipped to main → "Deployed"
  - PR open and ready → "In Review"
  - Work paused mid-stream → leave a comment with current state + next step
  - Scope changed → update description or add comment
- Add a comment summarizing this session's progress on the ticket if not obvious from PR links.
- **Verify PR↔ticket links landed.** For each ticket with a PR this session, fetch the issue and confirm the PR URL is actually in its `attachments`/links. The branch-token / `Closes <TICKET-ID>` auto-link usually fires, but tickets are systematically under-linked — if it's missing, attach it explicitly (`save_issue` with `links: [{url, title}]`). (Work-START state — In Progress + assignment — is handled automatically by the `linear-startwork.sh` hook on branch creation; you're only reconciling the *end* state here.)
- **Auto-reconcile multi-PR status drift.** Only after the link-verification above confirms **every** PR for the ticket is attached, run `~/.claude/hooks/reconcile-ticket.sh <TICKET-ID> [<TICKET-ID> …]` for the session's tickets. It advances a ticket to **Deployed** only when *every* linked PR is merged into its repo's default branch, and only from the states you list in `LINEAR_ADVANCE_FROM_STATE_IDS` (typically In Progress / In Review — never an Epics-style container) — fixing the multi-PR race where the tracker leaves a ticket stuck In Progress/In Review after just one of several cross-repo PRs merges (the no-op cases are silent). ⚠️ The reconciler trusts the tracker's attachment set as complete (it's the branch-name gate that keeps links complete) — so an *under-linked* ticket, where a still-open PR was never attached, could advance early; this is why link-verification must run first. Advance-only; never sets Done (Done stays a manual, prod-verified promotion).
- If you're unsure whether a state change is warranted, ask the owner before flipping it.
- **Follow-up work surfaced this session → file a ticket, don't just note it.** Out-of-scope review findings, deferred fixes, known gaps, "we should also…" items — if it's legitimate and won't ship this session, create an issue (batch related ones; link the source PR + `file:line` where relevant). The "Open follow-ups" report section is a summary of filed tickets, not a substitute for filing them. Out-of-scope ≠ discard.
  - **Route every follow-up into one of the three filing buckets — never file it parentless, ownership per bucket:**
    1. Routine nit/ops follow-up on the feature being shipped → same feature epic as the ticket being shipped (epic stays open until done); assign the owner.
    2. CR-deferred small self-contained 1-off (LLM-doable) → that project's standing "Bulldozer 1-offs" epic, unassigned (find by title; create it there if missing).
    3. Big idea surfaced mid-session → a new parked epic of its own; assign the owner.

    Canonical bucket definitions: see the README's "Work taxonomy (Linear conventions)" section.

## 2. GitHub

Goal: every PR opened/touched this session is in the right state and visible.

- `gh pr list --author "@me" --state open` — quick scan of your open PRs.
- For PRs touched this session:
  - Draft that's actually ready? → flip to ready (`gh pr ready <num>`).
  - Description stale vs. final commits? → update body.
  - Automated-reviewer findings open? → don't auto-fix unless trivially safe, but every legit finding gets a disposition: in-scope+trivial → fix; in-scope non-trivial or out-of-scope (many reviewers scan the whole repo, not just your diff) → **file a ticket**; junk → waive-with-reason in the report. Noting it in the report is not a disposition on its own.
  - Stacked PR with base merged? → rebase or note for follow-up.
- Respect your team's merge policy (in our shop: the agent never merges).

## 3. Stale / dirty branches

Goal: no uncommitted work, no unpushed commits, no branches without PRs that should have them.

For each repo touched this session:

```bash
cd <repo>
git status --short              # uncommitted changes?
git log @{u}..HEAD --oneline    # unpushed commits? (skip if no upstream)
git branch --show-current       # branch name
gh pr view --json number 2>/dev/null  # has a PR?
```

Decision tree per branch:

| State | Action |
|-------|--------|
| Clean, pushed, has PR | nothing to do |
| Uncommitted changes | show `git diff --stat` to the owner, ask: commit + push, stash, or discard? |
| Committed but unpushed | ask: push to existing PR / open new PR / leave for later? |
| Pushed but no PR, and changes look ship-worthy | ask: open PR or leave as work-in-progress branch? |
| Worktree with no changes | safe to leave; mention in report |

**Always confirm with the owner before pushing, opening a PR, or discarding work.** Reading state is free; mutating it needs a green light.

## 4. Memory

Goal: capture anything from this session that a future session needs to know and can't easily re-derive from code or git.

Review the session for memory candidates. Strong candidates:

- New feedback the owner gave (corrections AND validated approaches)
- Project state that's not in git (decisions, blockers, who-owns-what, dated deadlines)
- External references the owner named (dashboards, tracker projects, docs)
- Surprising gotchas / footguns surfaced this session
- User-context shifts (new role, new project focus, new collaborator)

For each candidate:
- Check for an existing entry to UPDATE rather than duplicate.
- If updating an existing memory because facts changed, also update its description/index line.

Skip: code patterns, file paths, architectural facts derivable by reading the repo, ephemeral task state.

If nothing is worth saving, say so — don't invent entries to look productive.

(This works with Claude Code's native auto-memory. We run a retrieval-backed memory system instead — see the README's memory section — but the discipline is identical: dedupe before writing, update over duplicate, skip the derivable.)

## 5. Config repo

Goal: your `~/.claude` customizations stay tracked.

We keep `~/.claude` itself as a **private** git repo with an ignore-everything `.gitignore` that whitelists only deliberate config: `CLAUDE.md`, `settings.json`, `commands/`, `skills/`, `hooks/*.sh`, playbooks. Never tracked: credentials, tokens, `.env`, MCP definitions with secrets, session data.

```bash
cd ~/.claude
git status --short
```

- Dirty? Review the diff briefly (sanity-check no secrets — tokens, API keys, account emails in new files), then commit with a one-line message describing the config change and push.
- New customization files created this session (new command/skill/playbook/script)? Check they're covered by the `.gitignore` whitelist; extend it if not.
- Clean? Say so in the report.

(Skip this step if you don't track your config in git — though you should.)

## 6. Cleanup queue

Goal: clear delete-debt the careful hook deferred during unattended loops (it queues any unrecognized `rm -r` to `~/.claude/cleanup-needed.log` rather than wedging the loop).

```bash
DEPTH=$(python3 ~/.claude/hooks/cleanup-sweep.py --count); echo "$DEPTH"
# Durable record (guarded no-op if the helper is absent) — feeds a weekly scorecard:
if [ -x ~/.claude/hooks/ledger-append.sh ]; then
  ~/.claude/hooks/ledger-append.sh \
    "$(jq -nc --argjson n "${DEPTH:-0}" '{skill:"wrapup", event:"cleanup_depth", count:$n}')" \
    || echo "WARN: cleanup_depth row REJECTED — this wrapup is missing from the durable record" >&2
fi
```

**Say the WARN out loud in the report if it fires** (`hooks/ledger-append.sh` exits 1 and writes nothing on a bad payload). The `||` hangs off the INVOCATION, not off the `[ -x … ]` test — an absent helper stays a deliberate silent no-op; a REJECTED row must be noise, never a silent gap in the scorecard. The emit still never gates the wrapup.

- `0` → "Cleanup: nothing pending" in the report.
- `>0` → run the **`/cleanup`** sweep: show the queued deletes (`cleanup-sweep.py`), confirm which to run, then **`cleanup-sweep.py --run <i>`** each approved entry (or `--run-all`) — it parses the actual delete targets and removes them directly. Do NOT re-run the queued `cmd` (the hook re-defers it, and some entries recreate scratch). Use `--remove <i>` only for declined entries. Report how many were cleared vs. left.

## 7. Report

One consolidated message. Structure:

```
## Wrapup complete

**Tracker**
- XXX-111 → moved In Progress → In Review (PR #123)
- XXX-222 → comment added: blocked on team merge
- (or: nothing to update)

**GitHub**
- PR #123 flipped draft → ready
- PR #456 has 2 open reviewer findings — left for next session
- (or: nothing to update)

**Cleanup**
- 3 deferred deletes cleared, 1 left (declined)
- (or: nothing pending)

**Branches**
- repo-a/feat/foo — clean, pushed, PR #123
- repo-b/main — clean
- (any branches needing follow-up that the owner confirmed leaving)

**Memory**
- Saved: <slug> — <one-line why>
- Updated: <slug> — <what changed>
- (or: nothing new worth saving)

**Config repo**
- committed: <one-line summary> (or: clean)

**Tickets filed for follow-ups / out-of-scope findings**
- XXX-333 — <what> (out-of-scope reviewer finding from #123)
- waived: <finding> — <reason>
- (or: none)

**Open follow-ups for next session**
- <thing #1>
- <thing #2>
```

Keep it scannable. No filler. If everything was already clean, the report can be 3 lines.
