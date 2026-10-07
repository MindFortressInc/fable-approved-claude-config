---
name: babysit-prs
description: Sweep ALL your open PRs across the org. A deterministic, tested script (skills/babysit/babysit_classify.py) does all classification + planning; this skill executes the planned actions (apply CR fixes, bump, rebase, CI-triage, CR-CLI), renders the script's greens/decision VERBATIM, and reports. Loop-safe — designed for hourly `/loop 1h /babysit-prs`.
argument-hint: "[repo1,repo2,...] or 'no-loop' (default: arm hourly cron + sweep all org repos)"
allowed-tools:
  - Bash
  - Read
  - Edit
  - Write
  - Glob
  - Grep
  - AskUserQuestion
  - CronList
  - CronCreate
  - CronDelete
  - ToolSearch
---

<!--
ADAPT BEFORE USE: set the TEAM CONFIG block at the top of
skills/babysit/babysit_classify.py (org owner, lane repos, cosmetic-check
allowlist, quiet-scan authors). "CR" below = CodeRabbit; substitute your
automated reviewer. Ticket ids use the `dev` prefix by default
(LINEAR_BRANCH_PREFIX to change it).
-->

<objective>
Drain your open-PR queue across the org. `babysit_classify.py` classifies every PR + plans the actions; you EXECUTE them (fixes, bumps, rebases, CI-triage, CR-CLI), then report. The script owns all classification, the greens block, the bump/rebase/triage plan, the stall/decision logic, and the state file — you never re-derive any of it. You own only the judgment work: applying mechanical fixes, resolving conflicts, interpreting CLI harvest, and writing NEEDS_HUMAN prose.

Hard scope: NEVER merge PRs, NEVER push --force without --force-with-lease, NEVER touch DRAFT PRs.

## EXECUTION MODE — this run is SINGLE-TURN. There is no notifier and no second turn.

**Read this before anything else. It is the constraint every other rule assumes.**

When this command is run headlessly (e.g. via `launchd/babysit-hourly-gate.sh`), it runs under `claude -p`: one non-interactive turn. **The moment you stop emitting tool calls, the turn ends and the process EXITS.** There is no re-invocation, no background-task notifier, and no second turn in which to "pick this up."

So all of these are unreachable by construction, and every one of them ends the sweep on the spot:

- *"I'll wait for the completion notification rather than poll."* — **there is no notifier here.**
- *"The background waiter will notify me when it finishes."* — **there is no waiter.**
- *"I'll pick this up as soon as the classifier lands."* / *"I'll resume automatically when it completes."* — **there is no later.**

**Never wait for a completion signal. Poll the child yourself, with repeated tool calls, until it exits.** A step is only "awaited" if you are still emitting tool calls while it runs — a blocking foreground call, or a loop that keeps calling `sleep`/`wait`/a status check until the child is gone. Silence is not waiting; silence is exiting.

**Why this rule exists, and why saying "don't stop" is not enough.** Backgrounding a long job and yielding until you are notified is *correct* in a normal interactive session — that's how an interactive supervisor learns its own sweep finished. The pattern is right in an interactive session and unreachable here, so a sweep that improvises it anyway — backgrounding the classifier and waiting for a notification headless mode never sends — exits `rc=0` well under a real sweep's runtime, having done nothing, while every observable signal (the exit code, the pipeline statuses, the fire/exit log pairing) reads healthy. Telling a sweep "do not stop" does not reach it, because **it does not believe it is stopping** — it believes it is awaiting a notification. The mechanism's absence is the load-bearing fact, so it is stated outright rather than implied.

**The residue is detected, not trusted to prose.** `hooks/babysit-fire-log.sh verdict <sweep_start> <rc> <duration_s>` flags the signature — `rc=0`, a short run, and no Step 3 ledger row written since the sweep started — and the headless launcher (`launchd/babysit-hourly-gate.sh`) relaunches once on `FORFEIT`. That is a backstop for a recurrence, not a reason to relax this rule.
</objective>

<process>

## Step 0 — Auto-arm cron + loop-mode (state file is owned by the script)

The queue-state file `/tmp/babysit-prs-state.json` is read AND written by `babysit_classify.py` (Step 1/4). Do NOT read or write it yourself.

**0·mutex — acquire the machine-wide sweep lock FIRST, before anything else.** Two sweeps overlapping (hourly cron + the launchd plist + a second terminal / CI-fix terminal) race on the state file, the shared `~/code/<repo>` git clones, the CR-CLI pid files, and CR bumps — this is a real corruption/clobber, not a theoretical one. Run:
```bash
~/.claude/hooks/babysit-lock.sh acquire
```
- **`LOCKED …` (exit 3)** → another live sweep owns the lock. **SKIP this entire sweep** — do NOT run the classifier, do NOT execute actions. Report one line: `_Skipped — another babysit sweep is active (lock held by <owner>, age <n>s). Cron stays armed; next fire retries._` and STOP. Do NOT release a lock you don't own.
- **`ACQUIRED …` (exit 0)** → you hold the lock for this whole sweep. You MUST call `~/.claude/hooks/babysit-lock.sh release` at the very end (Step 4, both the PROGRESSING path and the AUTO-STOP path). The stale TTL is 60 min — deliberately LONGER than every launcher's sweep cap, so a live sweep can never age out of its own lock; a crashed sweep's lock is instead reaped within seconds by its LAUNCHER (`babysit-lock.sh reap-since`, called on every exit path), and the TTL is only the backstop for a launcher that itself died. Always release explicitly.

**0·progress — load the durable judgment store** (survives sessions/reboot, unlike the `/tmp` state file). This is how a fresh session resumes continuity instead of re-doing work:
```bash
~/.claude/hooks/babysit-progress.sh load        # {cli_reviewed, known_fp, waived_findings, fix_attempts, merges}
```
Keep it in mind for Step 2: use `cli_reviewed[repo#pr].head` to decide CR-CLI re-launch (only when the head MOVED), and `known_fp` to skip re-chewing a classifier false-positive (e.g. a `fix` action whose only "actionable" comment is a CR ack-reply). Update it as you act (helpers below).

**0a. Silent auto-arm of the hourly cron.** Call `CronList`; if no recurring job has `prompt == "/babysit-prs"`, call `CronCreate` with `cron: "7 * * * *"`, `prompt: "/babysit-prs"`, `recurring: true`. Note in the report's opening line: "_Auto-armed hourly cron `<id>` — stays alive while the queue has pending work; auto-stops only when drained (or stalled)._"

Opt-out: if `$ARGUMENTS` is the literal `no-loop`, skip 0a/0b — the one-shot escape hatch. Any other arg is the repo filter (passed to the script as `--repos`). Plain `/babysit-prs` always arms.

**0b. Arm careful-hook loop-mode** (skip if `no-loop`). Self-expires in 90 min, re-armed each iteration:
```bash
~/.claude/hooks/loop-mode-arm.sh 90 2>/dev/null || true
```
Anything auto-proceeded lands in `~/.claude/cleanup-needed.log` — surface a "cleanup pending" note if non-empty at the end.

## Step 0.5 — Recording a human ruling (WAIVE)

A human ruling = a ruling made INSIDE an agent session, never a human typing into GitHub or clicking "Resolve conversation" by hand (those are not actions a session can invoke repeatably). Whenever the operator rules WAIVE on a finding — reviewing this sweep's report, or in any other session entirely — the session performs ONE ruling action, which is ALWAYS both of the following, never the store write alone:

1. **Write the store**: `~/.claude/hooks/babysit-progress.sh waive <repo>#<pr> "<finding-key>" "<reason>" <by>` — the durable local record.
2. **Post the same ruling to the PR**, rendered from the ONE source of truth so the format can never drift out of sync with its own parser:
   ```bash
   python3 ~/.claude/skills/babysit/babysit_classify.py ruling-comment "<finding-key>" "<reason>" <by> \
     | gh -R <your-org>/<repo> pr comment <pr> --body-file -
   ```
   **Set `BABYSIT_WAIVE_AUTHORIZED_BY=<human>` on BOTH commands, or neither.** It is the same variable step 1's hook already demands for anything beyond nit-grade findings, and the renderer reads it from the environment — there is no argv flag — so the comment can never claim more authority than the store write posted beside it. Without it the comment renders `authorized-by-human: false`, which still suppresses an ordinary finding but **cannot clear a CRITICAL**: `by:` is a caller-supplied string an agent can set to anything, so it is not authorization, and letting a comment clear a critical on `by:` alone would make the durability path the way around the "no critical waiving without human input" bar.

   This is the natural place, with the evidence, where reviewers can see the ruling — AND durability: `harvest_ruling_comments()` re-derives the identical waiver straight from this comment on every sweep, so a ruling made in a session that never touches this machine's store, or a store that gets lost/reset, still self-heals on the very next sweep. No re-adjudication needed.

`<finding-key>` must match the `<file>:<rule-id>` convention (never a line number or head SHA — both move on a push, and the point is the ruling survives a force-push/rebase). `<by>` is who ruled, never the session. Never do step 1 without step 2 or vice versa.

**Configure the trusted-author allowlist once**: set `RULING_AUTHORS_DEFAULT` in `babysit_classify.py` (or export `BABYSIT_RULING_AUTHORS`) to the GitHub login(s) your sessions comment as. Both marker formats are public, so on a public repo anyone could post a marker-shaped comment — the classifier only ever parses ruling comments AND CR-CLI harvest comments authored by these logins, and an unconfigured/empty list disables comment harvesting entirely (fail closed).

## Step 1 — Run the classifier/planner and read its JSON

ONE call classifies every open PR you authored and plans every action. Pass the repo filter through if `$ARGUMENTS` is a repo list:
```bash
python3 ~/.claude/skills/babysit/babysit_classify.py sweep ${ARGUMENTS:+--repos "$ARGUMENTS"}
```

**NEVER background this call.** Run it in the FOREGROUND and let the tool call block until it returns — this is the one long step in the sweep, and the step a forfeit dies on if the EXECUTION MODE rule above is not followed. The backgrounded `coderabbit review` launch under `cli_launch` below is a house pattern for that step specifically, because it harvests its children in a LATER sweep; **the classifier is not that shape** and must not inherit it. Do not detach it, do not wrap it in a completion-signalling shell, and do not "kick it off and wait" — there is nothing to wait with.

If the output is too large to hold in context, redirect it to a file **and still block**: `python3 … sweep > /tmp/babysit-sweep.json` in the foreground, then read the file. If a wrapper ever returns before the work is done, that is a bug in the invocation, not a cue to go idle: poll the pid with repeated tool calls (`kill -0 <pid>`) until it is gone, then read the output. **No classifier process may outlive its sweep.**

(Omit `--repos` for `no-loop` / empty / plain invocations.) Parse the single JSON document. Its keys:
- `prs[]` — `{repo,number,branch,head_oid,state,mergeable,mss,failing_checks,tier,lane,last_cr_activity,blurb,cli_findings_open,ruled_via_pr_comment,green_via,parent_pr,parent_state}` per PR (`state` = CR state: CLEAN / HAS_ACTIONABLE / RATE_LIMITED / NO_REVIEW_YET / TRIGGERED_WAITING / STACKED_BLOCKED / FETCH_FAIL). `cli_findings_open` is the CR-CLI harvest's unapplied-findings signal after waiver subtraction (null when clean/absent); `ruled_via_pr_comment[]` lists finding-keys a structured ruling comment resolved this sweep. `parent_pr`/`parent_state` name a stacked PR's parent (from a `stacked on #N` title marker, or a base branch that is another open PR's head) and whether it is `OPEN`/`MERGED` (`null` when there is no parent or it could not be read).
- `greens{strict[],cosmetic_yellow[],red_ci[]}` — the authoritative merge-ready buckets. Each entry: `{repo, number, pr (same value as number), branch, base, lane, mss, failing_checks, blurb, green_via, red_failing (red_ci only)}` — the PR number is under BOTH `number` and `pr`; use either, never render a key that isn't there. **Render VERBATIM in Step 3 — do NOT reclassify.** `green_via` is `"cloud"` or `"cli"` — a clean CR-CLI review at the live head greens a PR on the same terms as a cloud review (it must postdate the head commit, the reviewed SHA must BE the head, it reports 0 open findings — 0 raised, or every one of them adjudicated — and it never overrules an outstanding cloud finding or a red check). **Report `green_via` on every merged green** — a `cli` green shipped on a local review, and a merge must never be silently attributed to the cloud reviewer.
- `actions[]` — the ordered work list: `{type: bump|fix|rebase|retarget|ci_triage|cli_launch, repo, pr, why, verify_open, mode?, comments?, branch?, base?, parent_pr?}`. `retarget` is the parent-merged rung inside the `rebase` ladder below — its own action outcome, never rendered as a `rebase`.
- `attempt_capped[]` — `{repo, pr, lane, blurb, path: fix|rebase, attempts, last, guard_exit}` for every PR the attempt cap took out of `fix`/`rebase` this sweep (≥ `BABYSIT_FIX_ATTEMPT_CAP`, default 2, failed attempts at the CURRENT head). These are NEEDS_HUMAN rows — render each with its `last` reason; never let one vanish silently. A push to the PR re-arms it automatically; `babysit-progress.sh reset-pr <repo>#<pr>` is the human override.
- `reconcile_tickets[]` — merged-PR-derived ticket ids for the reconciler.
- `ruled_via_pr_comment[]` — `{repo, pr, lane, blurb, findings[]}` per PR whose findings a structured ruling comment resolved this sweep (see Step 0.5). This is settled human debt, not open work — never render it as a needs-human row; report it as its own count so queue depth is never inflated by already-adjudicated findings.
- `quiet` — `yes:...` / `no:reason` (gates the CR-CLI step).
- `decision` (PROGRESSING|DRAINED|STALLED), `pending`, `fingerprint`, `streak` — the loop verdict. **Use as-is in Step 4.**

If the JSON has an `error` key (e.g. `search_fetch_fail`), report it and treat as PROGRESSING (do NOT auto-stop) — the next sweep retries.

Reconcile merged tickets (idempotent, conservative — advances only when EVERY linked PR is merged; over-including is a safe no-op, never sets Done):
```bash
~/.claude/hooks/reconcile-ticket.sh <all reconcile_tickets...>
```
Print `_Reconciled: <list> → Deployed (or: none)._` in Step 3.

## Step 2 — Execute `actions[]` in order

For EACH action: **re-confirm the PR is still OPEN first** (`gh -R <your-org>/<repo> pr view <pr> --json state -q .state` → must be `OPEN`; RETRY on empty ≥4× ~1.5s — an empty response is a transient throttle, NOT a closed PR; only a non-empty `MERGED`/`CLOSED` means dropped-off). Teammates merge in bursts mid-sweep. Run OPEN/bump loops inside an explicit `bash -c '...'` (zsh does NOT word-split unquoted `$var`). Then dispatch by `type`:

**GIT-SAFETY (applies to EVERY destructive git op below — fix/rebase/ci_triage/cli worktrees).** The mutex (Step 0) stops a second *babysit* sweep, but a human/CI-fix terminal doing git in the same repo is NOT locked out — so babysit must never clobber shared work:
- **Before any `git reset --hard` / `git clean -fd` on a worktree, check it's not in use:** `git -C "$wt" status --porcelain` — if it prints ANY line (uncommitted/untracked changes), a human or CI-fix terminal may own that worktree → **SKIP that worktree, do NOT reset/clean it**, and flag it in the report (`worktree <wt> dirty — skipped, may be in use`). Only reset a clean worktree.
- **Retry on `index.lock` contention:** any `git fetch` / index-touching op in a shared `~/code/<repo>` clone can collide with a concurrent terminal (`fatal: Unable to create '.git/index.lock': File exists`). Wrap in a retry: up to 3 attempts, ~2s backoff; if still locked after 3, SKIP that op this sweep (do NOT delete another process's `index.lock`) and note it — next sweep retries.
- Babysit's CR-CLI worktrees stay under `/private/tmp/*-cli` (babysit-private paths); never point a destructive op at a `~/code` primary clone's working tree.

**WORKTREE LEASE (fix, rebase/retarget, ci_triage, CR-CLI harvest).** The dirty-check above is read at one instant, so a worktree that is clean *at that instant* can still be mid-edit by another worker — a TOCTOU no amount of re-checking fixes. The worker USING a worktree says so explicitly, for as long as it uses it. BEFORE touching or creating the PR's worktree:
```bash
python3 ~/.claude/skills/babysit/worktree_lease.py acquire <repo>-<pr> --owner "babysit-<repo>-<pr>-$$" --worktree <path>
```
- Exit 0 prints `ACQUIRED key=... owner=<O> ...` — copy that exact `owner=` string; you need the literal to release (`$$` only makes it unique; that shell is gone after the call). Exit 3 prints `LEASED key=... holder=...` — another worker holds it: record the PR as declined (leased) for this sweep and do NOT touch the worktree, not even `git worktree add`.
- Never pass `--watch-pid $$` — that shell exits when the Bash call returns and the lease is reaped at once. The default already watches the nearest agent ancestor (`BABYSIT_LEASE_AGENT_NAMES`, default `claude`).
- Release with the exact owner string as an explicit final Bash call, on success AND failure paths: `python3 ~/.claude/skills/babysit/worktree_lease.py release <repo>-<pr> --owner "<O>"`. No `trap ... EXIT` in a per-call shell (it fires when that call ends, not when the work ends). Without `--owner`, release exits 2 and releases nothing.
- A crashed holder never needs manual cleanup: a dead watched pid, the TTL, or the 2h `BABYSIT_LEASE_MAX_AGE` ceiling each make the lease reapable on the next check.

### `bump` — post `@coderabbitai review`
`gh -R <your-org>/<repo> pr comment <pr> --body "@coderabbitai review"`. This spends the hour's CR credit refill; the script already capped it at 3 and rotated oldest-first. A bump is progress.

### `fix` — apply the CR's actionable inline findings (HAS_ACTIONABLE rules, VERBATIM)
0. **False-positive short-circuit (check FIRST — the classifier's HAS_ACTIONABLE can trip on a CR ack-reply).** If `~/.claude/hooks/babysit-progress.sh is-fp <repo>#<pr>` exits 0, this PR is a known FP — skip with that reason, no worktree. **Also check for an already-ruled finding before attempting anything**: if this finding's key already carries a `<!-- babysit:ruling ... -->` comment on the PR (see Step 0.5), or the classifier's `ruled_via_pr_comment[]`/the waiver store already cover it, do NOT re-derive a fix for it — it is settled, not open. Same shape as the FP short-circuit, different source. Otherwise inspect the CR comments newer than the last push: if the ONLY one(s) carry a `review_comment_addressed` marker / are a CR acknowledgement reply with **no "Prompt for AI Agents" block** (no real finding), it's a false-positive — skip it AND record it so future sweeps don't re-chew it: `~/.claude/hooks/babysit-progress.sh add-fp <repo>#<pr> "CR ack-reply only, no AI-prompt finding"`. (If a genuine new finding later lands, `clear-fp` it.)
1. **Take the WORKTREE LEASE first** (GIT-SAFETY above), then find/create the worktree: `git worktree list`; if the branch isn't checked out, create a sibling at `/tmp/<repo>-<branch-short>`. Symlink `node_modules` from the main checkout. (Obey GIT-SAFETY: never reset/clean a dirty worktree.) Release the lease when the fix is pushed or abandoned.
2. Apply the CR's suggested fix EXACTLY when it's **mechanical** (regex, min/max bound, missing validation). If it needs architectural judgment ("refactor X to Y", "rename Z") → skip, flag NEEDS_HUMAN.
3. **`ast.parse` is syntax-only; it does NOT catch a behavioral break.** If the fix changes runtime behavior of source-under-test you MUST run the affected suite:
   - TS: `npx tsc --noEmit` must pass.
   - Python: `python3 -c "import ast; ast.parse(open('<file>').read())"` is a syntax gate ONLY. If the fix touches a test file OR a source file with a sibling test module → RUN that module (map source→test by convention, e.g. `api/services/planning/date_triggers.py` → `tests/planning/test_date_due_soon.py`; if unsure run the nearest test dir). One PR shipped red because only `ast.parse` ran.
   - Worktrees have NO venv and bare `python` is not on PATH. Run pytest via the main checkout's interpreter: `~/code/<repo>/.venv/bin/python -m pytest <test_path> -q` (order: worktree `.venv` → sibling main `.venv` → `python3`). If NONE resolves you CANNOT validate → do NOT push; report "unvalidated, skipped".
   - ANY suite failure (including one your fix surfaced in a pre-existing test) → revert that file and skip. Never push a red suite.
4. Commit `fix(CR PR #<N>): <summary>`; `git push origin <branch>` (NOT --force; --force-with-lease only if rebase needed, documented in the message). CR auto-re-reviews on the new commit.
5. **Record the outcome so only REPEATED failure escalates.** On a validation failure: `~/.claude/hooks/babysit-progress.sh add-attempt <repo>#<pr> "<why>" <head-sha>` (head from `gh pr view <pr> --json headRefOid -q .headRefOid` — the planner re-arms the PR only when that head moves, so an omitted sha never lifts the cap on a push). On a successful push: `clear-attempts <repo>#<pr>`. The script stops planning `fix` for a PR at the cap and lists it in `attempt_capped[]` instead.

### `rebase` / `retarget` — bring a BEHIND/DIRTY branch current, or retarget a parent-merged stack child (Step 4.7 rules, VERBATIM)
Bringing a branch current with its base is mechanical — babysit OWNS it; only a genuinely *semantic* conflict needs the owner. The script already applied the attempt cap: a DIRTY/CONFLICTING PR whose merges failed `BABYSIT_FIX_ATTEMPT_CAP` times at the current head is in `attempt_capped[]`, not here (BEHIND is never capped — `update-branch` cannot ship wrong code).
1. **Cheap path (`mode: update-branch`):** `gh pr update-branch <pr>`. Succeeds for BEHIND / stale-but-clean DIRTY → done (CI re-runs). Errors "Cannot update PR branch due to conflicts" → real conflict, go to 3 (or 2 for a `retarget` action).
2. **Parent-merged retarget (`mode: retarget`) — ONLY for a `type: "retarget"` action, i.e. ONLY when `parent_state == "MERGED"`.** Do NOT resolve anything here; prove it is safe to point this branch straight at the default branch instead of touching a conflict that may already be moot. In the PR's worktree (lease held):
   ```bash
   python3 ~/.claude/skills/babysit/babysit_classify.py retarget-plan --worktree <wt> \
     --default-ref origin/<default> --parent-merge-sha "$(gh -R <your-org>/<repo> pr view <parent_pr> --json mergeCommit -q .mergeCommit.oid)"
   ```
   It runs, read-only: `git merge-base --is-ancestor <parent merge sha> origin/<default>` (the parent's work really landed); the **THREE-dot** `git diff --name-only origin/<default>...HEAD` (this branch's OWN file set — **never two-dot**: a tip-to-tip diff against a base that moved since the fork reports every file the base changed as if this branch touched it); and intersects that with the files the parent's merge brought in. **Non-empty overlap, a non-ancestor, or any git failure → `{"action":"fallthrough"}`: fall through to 3, unchanged. No cleverness on overlap** — retargeting anyway would silently drop the parent's changes from this branch's rendered diff. `{"action":"retarget"}` → `gh pr edit <pr> --base <default>`. **No merge, no push, no conflict resolution** — done; report it as its own outcome and skip 3–5. A parent branch GitHub already deleted (`fatal: couldn't find remote ref <branch>` on a fetch) is the NORMAL post-merge state, not an error — the merge sha comes from the PR metadata, which persists.
3. **Worktree merge:** sibling worktree at `origin/<head>` (lease held) → `git merge origin/<base> --no-edit`. On conflict, **do NOT strip markers by hand.** Run the guard, which decides the shape and does the strip itself:
   ```bash
   python3 ~/.claude/skills/babysit/babysit_merge_guard.py --worktree <worktree>
   ```
   - **exit 0** — union-strip was valid and applied; the resolved paths are already staged, so go to 4. A conflicted generated file listed in the guard's `GENERATED_FILES` is never stripped: the guard reruns its generator over the merged tree and stages the output. The registry ships empty; add `"path/in/repo": ["{python}", "scripts/generator.py"]` to opt a file in.
   - **exit 2** — REFUSED. `git merge --abort` + NEEDS_HUMAN, quoting the printed reasons verbatim, and `~/.claude/hooks/babysit-progress.sh add-attempt <repo>#<pr> "<the reasons it printed>" <head-sha> 2` (the trailing `2` records the guard's own exit code, so the row reads as a judgment call, not a mechanical retry). Do not re-derive, re-argue, or hand-resolve it.
   - **any other non-zero** — the guard could not ANSWER. Treat it exactly like a refusal: abort, NEEDS_HUMAN quoting stderr, record the attempt. A gate that failed to run is not a gate that passed.
   - The guard is all-or-nothing: if any file in the conflict set is refused, it writes nothing.

   *Why a script and not a rule:* union-strip keeps BOTH sides of every hunk, which is correct only for ADD/ADD (two router includes, two imports, a model registration). A re-indent colliding with an edit, or a replace/replace conflict, becomes two copies of the same code once the markers are stripped — measured: a billable API called twice for one request, in a file that still parsed clean. The guard checks each conflict hunk against the merge base before stripping and, after stripping, rejects new damage `ast.parse` accepts: duplicate kwargs, clobbered bindings, fewer guards than either parent had.
4. **HARD-VALIDATE before push:** `ast.parse` every touched `.py`; app import (e.g. `from api.main import app`) for service/router/wiring; run touched `tests/*` + the nearest test dir for a touched source module. **If a touched file is `.ts`/`.tsx`/`.mts`/`.cts`: `npx tsc --noEmit -p tsconfig.json` must pass** — a strip can leave a `.tsx` conflict `ast.parse` never looks at (e.g. one symbol imported twice from two modules). If no typechecker resolves you CANNOT validate → do NOT push; report "unvalidated, skipped". **ANY failure → `git merge --abort`, do NOT push, flag NEEDS_HUMAN "semantic rebase conflict in `<file>`" — and `add-attempt <repo>#<pr> "<the specific contradiction>" <head-sha>`.** Say WHAT contradicted ("both sides rewrote `rate_limit()` to different contracts") — that string is what the human queue shows next sweep.
5. Clean → commit the merge (`--no-edit`) + plain `git push` (no force) + `clear-attempts <repo>#<pr>`. CI re-runs; PR re-greens next sweep. Release the lease.

### `ci_triage` — otherwise-clean PR blocked only by a real failing check (Step 4.6 tree, VERBATIM)
NEVER guess-patch a logic failure to make it pass — wrong-but-green is worse than red.
1. `gh run list -R <your-org>/<repo> --branch <head> --workflow CI --limit 1 --json databaseId,status,conclusion`.
2. **Startup/infra signature** (job `failure` <30s, empty steps/log, OR annotation mentions billing/spend-limit): transient → re-run **once**, in the mode `python3 ~/.claude/skills/babysit/ci_rerun_mode.py --repo <owner>/<repo> --run <id> --pr <pr>` prints (run its `cmd` verbatim; never add or drop `--failed` yourself). Why: `gh run rerun --failed` reuses every successful job's outputs, including a CI router's, so a shard the router sent to GitHub-hosted stays there on every `--failed` re-run (measured: 4–5 attempts on `ubuntu-latest`; one full re-run re-routed it to the self-hosted pool and went green). The script picks a FULL re-run only when a failed job ran on GitHub-hosted, the run has a router job, and the PR lacks `ci:fast` (read live); anything else, including any read error, keeps `--failed`. Router job / caller names / fast label: `BABYSIT_CI_ROUTER_JOB` / `BABYSIT_CI_ROUTER_CALLERS` / `BABYSIT_CI_FAST_LABEL`. Billing → NEEDS_HUMAN ("GH Actions spend cap — owner's billing"). Never code-fix.
3. **Real failure** (`gh run view <id> --log-failed`): auto-fix ONLY these bounded patterns (fix in worktree → VALIDATE per the fix rules above → `fix(CI #<N>): <summary>` → push): collection/import error; stale snapshot/golden (regenerate via its own mechanism); clock/cron flake (freeze via monkeypatch); lint/format/codegen-parity gate (run the formatter/codegen, commit).
4. **Everything else → NEEDS_HUMAN** with the failing test name + one-line reason. Never patch a logic test to turn it green.

### `cli_launch` — CR-CLI on cloud-rejected stacked PRs (Step 4.5, only when `quiet` starts `yes:`)
The script only emits `cli_launch` actions when `quiet` is `yes:...` and the PR is a stacked base with 0 inline — you do NOT re-derive targets. The CLI is a SEPARATE ~3/hr quota from cloud.
- **HARVEST first** (every sweep, regardless of quiet): for each `/tmp/cli-*.pid` whose PID is dead and whose `/tmp/cli-*.out.json` has a `{"type":"complete"}` line — re-check OPEN, parse `jq -c 'select(.type=="finding")'`, apply mechanical fixes per the `fix` rules (sibling `/tmp/<repo>-<branch>-cli` worktree, WORKTREE LEASE held), commit `fix(CR CLI #<N>)`, push, and post a `## 🤖 CodeRabbit CLI review (local)` findings comment (never on a MERGED/CLOSED PR). Read `repo`/`pr`/`branch`/`base` from the `/tmp/cli-<id>.meta` sidecar. **On a completed harvest, record the reviewed head so the next sweep won't redundantly re-review it:** `~/.claude/hooks/babysit-progress.sh set-cli-head <repo>#<pr> <the-head-sha-the-review-covered>`. Clean up pid/out on done; kill+discard runs older than 15 min.
- **LAUNCH** each `cli_launch` action (up to the 3 already in the plan, minus in-flight): **first, re-launch ONLY when there's new code to review** — compare the current `origin/<head>` short-sha against `~/.claude/hooks/babysit-progress.sh cli-head <repo>#<pr>` (the head the last CLI review covered, durable across sessions). If they MATCH, skip the launch (re-reviewing byte-identical code just re-posts the same flagged findings and burns the ~3/hr quota) — note `CLI held — head unchanged since last review`. If they DIFFER (author pushed) or there's no record → proceed. **Skip any PR whose worktree lease is held** (`python3 ~/.claude/skills/babysit/worktree_lease.py is-leased <repo>-<pr>` exits 0 when leased) — a fixer may be mid-edit in that worktree, clean at this instant or not. Then confirm OPEN, reset the babysit-private worktree to the new head (obey GIT-SAFETY: skip if dirty), write a `.meta` sidecar (`repo=`/`pr=`/`branch=`/`base=`/`started=`), then background `( cd "$wt" && coderabbit review --agent --base-commit "$(git -C "$wt" merge-base HEAD origin/<base>)" > "$out" 2>&1 ) &`, record `$!` in `/tmp/cli-<id>.pid`, `disown`. A launch that errors `"errorType":"rate_limit"` → clean up silently and move on (do NOT record a head — nothing was reviewed; next sweep retries). `"errorType":"auth"` → note "run `coderabbit auth login`", skip CLI this sweep. Effects land in the NEXT sweep's harvest.

  **Backgrounding here is correct for THIS launcher step only.** It works because this same sweep (or the next one) comes back later in Step 2 to harvest the child via its `.pid`/`.out.json` files — there is a consumer. The Step 1 classifier has no such consumer and must NOT inherit this pattern (see EXECUTION MODE above): backgrounding it and moving on ends the sweep with nothing read.

**Budget note:** cloud bumps and CLI launches draw from separate buckets — a quiet sweep can advance up to 3+3 PRs. Watch the clock: each sweep should finish <10 min; if you hit walls, report and let the next hourly sweep continue.

## Step 3 — Report

Open with the auto-arm line, then `_CLI: quiet=<quiet> · harvested=<N> · launched=<N> · in-flight=<N>_` and (if any triaged) `_CI-triage: fixed=<N> · flagged-human=<N> · reran=<N>_` and `_Reconciled: <list>._`. If the GIT-SAFETY guard skipped any dirty worktree or a lock-contended git op this sweep, surface it: `_Git-safety: skipped <wt> (dirty/in-use) · <n> index.lock retries._`. (The mutex is held for the whole sweep — you acquired it at Step 0 and release it at Step 4.)

**LEAD with the GREENS block, rendered VERBATIM from `greens`** (do NOT reclassify — the script already ran the mandatory 🟡 gate and the RED-regex-wins check). Group each tier's entries by `lane`:
- 🟢 **`strict`** and 🟡 **`cosmetic_yellow`** (annotate each 🟡 with its `failing_checks`): `owner` → ✅ **Your lane (merge now)**; `team` → ⛔ **Team's lane** (ready, but theirs to merge); `secondary` → ◽ **Secondary product — your call** (feature→develop); `secondary_cohort` → 🔑 **Cohort unblockers** (stack roots gating a cohort). Annotate stack parents with the merge procedure (merge WITHOUT `--delete-branch` → retarget child → delete branch).
- 🔴 **`red_ci`** — surface EVERY entry with its `red_failing` checks; these are routed to `rebase`/`ci_triage`/NEEDS_HUMAN via `actions`, never folded into greens. If `red_ci` is empty, say so — the count is mandatory every sweep.
If all three tiers are empty: "no greens this sweep."

If `ruled_via_pr_comment` is non-empty, surface it on the summary line (`_Ruled this sweep: <N>_`) and name the PRs it cleared — that is the visible proof the ruling input path is working, not silence. Never render a ruled finding as a needs-human row.

Then the action-results table (`| Repo | PR # | Branch | State | Action Taken | Result |`), a **Clean-list** of each CLEAN PR's `blurb`, and any NEEDS_HUMAN items (with the failing test / architectural reason) — **including every `attempt_capped[]` row** with its `path`, `attempts` and `last` reason (a `guard_exit` of `2` means the merge guard REFUSED the conflict shape: a judgment call, not a retry). Surface cleanup debt: `python3 ~/.claude/hooks/cleanup-sweep.py --count` → if `>0`, `_🧹 Cleanup: N delete(s) pending — run /cleanup._` (never resolve it here).

End with the summary line matching `decision`: `ACTIVE FIXES` / `CONSUMING CREDITS` (bumped N to spend the refill; NOT a blocker) / `WAITING ON CR` / `NEEDS HUMAN` (terminal, escalate) — for PROGRESSING; or the Step 4 auto-stop line.

**Durable record (guarded no-op if the helper is absent).** Append one per-sweep summary event to `~/.claude/automation-ledger.jsonl` (a durable quality record a weekly scorecard can aggregate), with real values sliced from the Step 1 classifier JSON (capture it once as `$SWEEP`):
```bash
[ -x ~/.claude/hooks/ledger-append.sh ] && ~/.claude/hooks/ledger-append.sh "$(printf '%s' "$SWEEP" | jq -c '{
  skill:"babysit", event:"sweep",
  pending:(.pending // 0),
  bumps:([.actions[]? | select(.type=="bump")] | length),
  fixes:([.actions[]? | select(.type=="fix")]  | length),
  red_ci:((.greens.red_ci // []) | length),
  decision:(.decision // "UNKNOWN")}')"
```

**Catch-up digest (optional, read-only).** Any interactive session can ask "what happened since I last looked" without re-running a sweep: `python3 ~/.claude/skills/babysit/babysit_digest.py [--since 2026-01-01T00:00:00Z]`. It reads `~/.claude/automation-ledger.jsonl` and `~/.claude/logs/headless-babysit.log`, makes no `gh`/API calls, and prints one JSON document; without `--since` the window starts at this session's last digest (2h back on a first run, `cold_start: true`). `queue` summarises the sweep rows (pending first → last, bumps, fixes, latest decision); `gap.cadence_holes` lists gaps longer than `BABYSIT_CADENCE_HOLE_MIN` (default 120) between sweeps and `stall` a `fire:` with no `exit=`; read both with `coverage` — when it says the log had no data, an empty list means "unknown", not "clean". `gate.suppressed_backstop: true` means the hourly backstop stood down while nothing was actually sweeping.

## Step 4 — Decision (from the script — NEVER recompute stall logic)

**0·release the mutex — ALWAYS, on every exit path of a sweep you actually ran** (both PROGRESSING and AUTO-STOP; do this before/around the decision handling so it can't be skipped): `~/.claude/hooks/babysit-lock.sh release`. (It's a no-op if you don't own the lock; a missed release is reaped by the launcher's `reap-since` on exit — the 60-min TTL is only the last-resort backstop — but release explicitly.) If you SKIPPED the sweep at Step 0 because it was `LOCKED`, do NOT release — you never held it.

Use `decision` verbatim:
- **PROGRESSING** → leave the cron armed; the loop fires again next hour. This is the default while any PR is pending — do NOT stop just because a sweep pushed no fix (a bump-only sweep is the loop working). A credit-blocked/rate-limited queue is ALWAYS PROGRESSING (the script forces `streak=0`); NEVER auto-stop on credit exhaustion and NEVER report it as needing the owner's billing action.
- **DRAINED** or **STALLED** → AUTO-STOP: `CronList` → `CronDelete` the `/babysit-prs` job (or note "manual invocation"); `rm -f /tmp/babysit-prs-state.json`; report:
  - DRAINED → `AUTO-STOPPED (queue drained)` — every non-draft PR is CLEAN or NEEDS_HUMAN. List NEEDS_HUMAN items. Re-arm with `/babysit-prs` when new PRs/CR feedback land.
  - STALLED → `AUTO-STOPPED (stalled)` — `streak` sweeps frozen with zero credit-blocked PRs to bump (CR genuinely not responding). List pending PRs. Re-arm once CR is back.

Do NOT re-arm the cron yourself after AUTO-STOPPED. NO per-sweep auto-compact (it kills the backgrounded CR-CLI procs before harvest — learned the hard way).

</process>

<hard_rules>
- NEVER `gh pr merge` — the owner's call. NEVER `git push --force` (use --force-with-lease when rebasing). NEVER skip pre-commit hooks (`--no-verify`) unless the owner authorized this session.
- NEVER apply a CR fix you don't understand — note + skip. NEVER cross repos for a single fix — file a note and stop.
- A behavioral source change (new guard/branch/return-contract) MUST be validated by RUNNING the affected suite — `ast.parse` does NOT catch it. If no interpreter resolves, do NOT push; report "unvalidated, skipped". If pytest/tsc fails on your fix — or your fix surfaces a pre-existing failure — revert that file and skip.
- If a worktree doesn't exist, create a sibling at `/tmp/<repo>-<branch-short>`; symlink `node_modules` from main; run pytest via the main checkout's `.venv/bin/python`.
- rebase: union-strip is shape-gated by `babysit_merge_guard.py`, not by judgement — ANY refusal (exit 2), any other guard failure, or any hard-validate failure → `git merge --abort` + NEEDS_HUMAN + `add-attempt`, never ship a wrong merge.
- The attempt cap governs `fix` AND `rebase` (one shared bucket, `BABYSIT_FIX_ATTEMPT_CAP`, default 2) — enforced by babysit_classify.py, re-armed only by a push to the PR (head move) or `reset-pr`. A semantic conflict never resolves on retry.
- CR-CLI only when `quiet` starts `yes:` (the script gates this) — it burns the owner's separate quota. NEVER launch on a PR with a live `/tmp/cli-<repo>-<pr>.pid`. NEVER post CLI findings on a MERGED/CLOSED PR (re-check state immediately before). NEVER CLI-review a PR cloud already covers (base main/develop).
- NEVER re-arm the cron after AUTO-STOPPED — the owner must re-invoke.
- Bump cap (≤3/sweep, RATE_LIMITED >50min oldest-first, rotate), CLI/rebase/ci_triage caps, and "re-trigger CR at most once per PR per sweep" — enforced by babysit_classify.py (test: test_b_all_rate_limited_bumps).
- "UNSTABLE + a failing pytest/CI/build/lint/codegen/tsc/mypy check is RED, never cosmetic-yellow; the RED regex always wins the cosmetic allowlist" — enforced by babysit_classify.py (tests: test_a_pytest_unstable_is_red_ci, test_f_cosmetic_only_is_yellow). Render `greens` verbatim; never reclassify.
- "Every sweep surfaces a 🔴 RED-CI count" — enforced by babysit_classify.py (`greens.red_ci`; test: test_a_pytest_unstable_is_red_ci).
- "CR credit exhaustion == RATE_LIMITED, the hourly-refill grind — never a wall, never a stop condition; a rate-limited/bumped queue forces streak=0 and stays armed" — enforced by babysit_classify.py (tests: test_b_all_rate_limited_bumps, test_e_stall_math).
- "Per-PR classify retries on empty; empty-after-retries → FETCH_FAIL, never NO_CR/green; greens come from the authoritative low-concurrency recompute" — enforced by babysit_classify.py (test: test_c_empty_is_fetch_fail).
- "Stall math (STALL_LIMIT 12; drained when zero pending) + fingerprint + state-file read/write" — enforced by babysit_classify.py (test: test_e_stall_math). The skill NEVER recomputes it.
(The regression tests named above live in `tests/test_babysit_classify.py`; the merge guard, worktree lease, CI re-run mode and digest each have their own `tests/test_babysit_*.py`.)
</hard_rules>

<loop_safety>
This command self-arms an hourly cron on first invocation (Step 0a) — `/babysit-prs` alone starts the recurring sweep. Each invocation is stateless re: PR state (the script re-derives everything from `gh`) and persists the tiny queue-state file the script reads/writes.

**Convergence (queue-drain, not quiet-count):** the loop stays armed while ≥1 non-draft PR is PENDING. It auto-stops only when the script returns `decision: DRAINED` (every PR CLEAN/NEEDS_HUMAN) or `decision: STALLED` (pending PRs, NONE credit-blocked, fingerprint frozen for 12 sweeps — CR's API genuinely dead). **Credit/rate-limit exhaustion is NEVER a stop condition** — it refills hourly and consuming it is the whole job; the script forces PROGRESSING while any PR is RATE_LIMITED. A bump-only sweep counts as progress. When you see NEEDS_HUMAN: flag prominently but know it's terminal — it doesn't keep the loop alive on its own. Opt out of auto-arming with `/babysit-prs no-loop`.
</loop_safety>

<context>
Repo override (if any): $ARGUMENTS

Standing notes:
- The reviewer's Pro tier hits per-hour ceilings; pace re-triggers (the script caps at 3/sweep).
- Leave PRs open 5-10 min for inline review before declaring "no findings".
- Specs live in the tracker, not local docs.
</context>
