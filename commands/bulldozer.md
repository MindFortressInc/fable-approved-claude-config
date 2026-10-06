---
name: bulldozer
description: Self-arming hourly heartbeat that clears EASY backlog tickets by DRAINING the eligible queue back-to-back each wake — one FRESH SUBAGENT per ticket (so the orchestrator context stays lean and each ticket gets isolated context) that confirms it still outstands, fixes it in an isolated worktree, runs a full ship/self-review flow, updates the tracker, and saves a memory. Keeps going ticket-after-ticket (a single ticket never stalls the chain for an hour); the hourly cron only re-wakes to catch newly-filed tickets. Runs until a target count of PRs ships, or the simple queue is dry.
argument-hint: "[N target PRs, default 10] [days, default 7] or 'no-loop' / 'stop'"
allowed-tools:
  - Bash
  - Read
  - Edit
  - Write
  - Glob
  - Grep
  - Agent
  - Workflow
  - SendMessage
  - AskUserQuestion
  - Skill
  - TaskCreate
  - TaskUpdate
  - TaskList
  - ToolSearch
  - CronList
  - CronCreate
  - CronDelete
---

<!--
ADAPT BEFORE USE — five things are yours to configure:
1. ISSUE TRACKER: written for Linear via its MCP (list_issues / get_issue / save_issue /
   save_comment). Swap in your tracker. "DEV-NNNN" is just Linear's issue-key shape.
2. CHECKOUT ROOT: replace `~/code/` with where your repo clones live. The skill creates
   worktrees under `~/code/<repo>.<id>` off each repo's canonical clone (never the
   system temp dir — see the fix recipe).
3. ASSIGNEE: "assign yourself" = the tracker user the loop may take tickets for. It NEVER
   takes a ticket assigned to someone else.
4. SHIP FLOW: each subagent invokes a `PRlaunch` skill (deep-review → reviewer CLI →
   outcome eval → open PR). Swap in your own; no ship skill → inline test + lint + open PR.
5. MEMORY (optional): the subagent saves a memory via a persistent-memory MCP (e.g. a
   `memory_write` tool). No memory tool → drop that step; the state file still dedups.
-->

<objective>
Drain a backlog of EASY tickets. Each wake (an hourly cron fire, or a manual `/bulldozer`) runs a **drain loop**: keep picking the next eligible simple/unblocked/well-spec'd/recent/un-stolen ticket and clearing it **back-to-back** until `target` PRs have shipped or the eligible queue is dry — DON'T stop after one and DON'T wait an hour between tickets.

**Each ticket runs in a FRESH SUBAGENT** (the Agent tool), which does the whole thing end-to-end — confirm-still-outstands → fix in an isolated worktree → ship flow → update the tracker → save a memory → return a validated JSON result object. The orchestrator (you) only accumulates the compact JSON results + the state file, so your context stays lean no matter how many tickets clear; the ~30k of per-ticket detail lives in the disposable subagent and is discarded when it returns. This is what gives all three properties at once: **per-ticket context isolation**, **back-to-back progress** (no hourly idle between tickets), and **resilience** (a subagent that fails returns an error → you record it and move to the next ticket; one bad ticket never kills the chain).

The hourly cron is a **heartbeat**, not the per-ticket driver: it re-wakes the loop to catch tickets filed since the last drain (and to resume if a wake was interrupted). It runs while the terminal is open and dies when you close it.

Hard scope: NEVER merge. NEVER start a ticket assigned to someone else. NEVER pick blocked / epic / research / needs-design / drifted / stacked-on-unmerged tickets. One ticket = one subagent = one branch = one worktree = one PR.
</objective>

<arguments>
- A number = target **shipped-PR** count (default **10**). A second number = recency window in days (default **7**).
- **`target` counts shipped PRs only.** A resolve-no-PR (a ticket the subagent finds already-fixed/drifted) advances the queue + resets the stall streak but does NOT increment `shipped` — the loop keeps draining toward the Nth *shippable* PR. The stall guard stops it when the queue is genuinely dry.
- `no-loop` = run ONE drain pass, don't arm the heartbeat cron. `stop` = CronDelete the bulldozer job + delete the state file + report + exit.
- Bare `/bulldozer` = arm the heartbeat + drain toward 10. **Re-invoking after a finished (or >6h stale) batch starts a FRESH batch**: the per-batch `shipped` counter resets to 0 while the `actioned` dedup ledger carries over (already-shipped tickets are skipped, never re-PR'd).
</arguments>

<process>

## Step 0 — Load state + self-arm the hourly heartbeat

**0a. Load `/tmp/bulldozer-state.json`:**
```json
{ "target": 10, "shipped": 3, "resolved_no_pr": 2, "days": 7,
  "actioned": { "EX-101": "api-service#41", "EX-118": "resolved:already-fixed-Done" },
  "results": { "EX-101": {"status":"shipped","ticket":"EX-101","repo":"api-service","pr":41,"branch":"me/dev-<n>-…","evidence":{"verify_cmd":"…","verify_result":"…"},"reason":null,"memory_id":"bulldozer-ex-101"} },
  "no_progress_streak": 0, "last_iter_at": "2026-06-17T08:00:00Z" }
```
```bash
jq -c . /tmp/bulldozer-state.json 2>/dev/null || echo '{"target":10,"shipped":0,"resolved_no_pr":0,"days":7,"actioned":{},"results":{},"no_progress_streak":0}'
```
- No file → fresh state from `$ARGUMENTS` (target/days) or defaults (`shipped=0`, `actioned={}`).
- `$ARGUMENTS == "stop"` → CronDelete the bulldozer job, `rm -f /tmp/bulldozer-state.json`, report totals, **exit**.
- **`actioned` is the dedup ledger** (every ticket ref ever shipped/resolved/failed); it carries across batches and is ONLY wiped by `stop`. `shipped` / `resolved_no_pr` / `no_progress_streak` are **per-batch** counters. **`results`** is the parallel audit map — the full validated result object per ticket (evidence/pr/branch/memory_id); like `actioned` it carries across batches and is only wiped by `stop`. **Ledger value conventions** (these govern whether a ticket is re-pickable): `<repo>#<PR>` = shipped → permanent skip; `resolved:<reason>` = resolved → permanent skip; `failed:<reason>` = failed ONCE → **retry-eligible** (gets exactly one more attempt on a later wake); `failedx2:<reason>` = failed twice → permanent skip, never retried again.
- **NEW-BATCH RESET — re-invoking `/bulldozer` after a finished batch starts a FRESH drain (the common case).** If the loaded state has `shipped >= target` (a prior batch already met its target → Step 3 AUTO-STOP deleted its heartbeat, so reaching Step 0a in this state can ONLY be a fresh manual re-invocation) **OR** `last_iter_at` is > 6h old (prior batch stale/abandoned): treat this as a NEW batch → set `shipped=0`, `resolved_no_pr=0`, `no_progress_streak=0`, **KEEP `actioned` + `results` as-is** (so already-done tickets are never re-picked → no duplicate PRs), apply any new `target`/`days` from `$ARGUMENTS`, then proceed to drain. **Do NOT auto-stop at load time** — the only AUTO-STOP is in Step 3, after a *live* drain hits target.
- **Otherwise** (recent `last_iter_at`, `shipped < target`) → RESUME the in-flight batch: keep all counters and keep draining toward `target`.

**0b. Self-arm the heartbeat cron (skip if `no-loop`/`stop`).** `CronList`; if no job has `prompt == "/bulldozer"`, `CronCreate(cron: "13 * * * *", prompt: "/bulldozer", recurring: true, durable: false)`. `durable: false` = session-only → runs hourly **while the terminal is open**, dies on close (7-day auto-expire). Note the cron id in the report.

**0c. Arm careful-hook loop-mode** (auto-proceed routine worktree/`.venv`/`test_*.db` teardown instead of wedging on a confirm prompt; self-expires 90 min, re-armed each wake):
```bash
~/.claude/hooks/loop-mode-arm.sh 90 2>/dev/null || true
```

## Step 1 — Build the dedup set + the candidate shortlist (once per wake)

Dedup set of in-flight ticket refs (skip these — already being worked; `prlaunch-ok` markers are written by the ship flow, `~/code/` is your checkout root). Three sources: ship-flow markers, local worktrees/branches (any ticket ref in a worktree path OR checked-out branch across every clone), and **open PRs across every owner you ship to** (catches a ticket someone PR'd with no surviving local worktree and no tracker state change):
```bash
# List EVERY owner you ship to (`--owner` repeats into one query): an org-only
# scan contributes zero in-flight PRs from repos under your personal account.
# --limit MUST stay above the true open-PR count: `gh search prs` sorts
# newest-first and SILENTLY truncates at the cap, hiding the OLDEST in-flight
# PRs — exactly the long-running ones most likely to have lost their worktree.
(ls ~/.claude/prlaunch-ok/ 2>/dev/null; \
 for r in ~/code/*/; do git -C "$r" worktree list 2>/dev/null; done; \
 gh search prs --owner <your-org> --owner <your-user> --author "@me" --state open --json title -q '.[].title' --limit 300 2>/dev/null) \
  | grep -oiE "dev-?[0-9]+" | tr 'A-Z' 'a-z' | sed 's/dev-*/dev-/' | sort -u
```
The `gh search prs` scan (adapt `<your-org>`/`<your-user>`; PR titles must carry the ticket ref — the ship flow's convention) runs ONCE per wake (rate-limit friendly); if it errors/returns nothing, proceed on the other two sources — it's a belt-and-suspenders layer, not a gate.

Query your tracker for backlog tickets created in the last `days` (for Linear: `list_issues` state:backlog, createdAt:-P<days>D, orderBy:createdAt, generous limit). **Second source — each project's standing "Bulldozer 1-offs" epic:** find the epic per project by exact title `Bulldozer 1-offs` (`list_issues` title match, state Epics — see the README's "Work taxonomy (Linear conventions)" section), then list its open children (`list_issues` parentId:<epic id>, state not Done/Cancelled). These are deliberately-parked CR-deferred 1-offs — not fresh backlog, so the `days` window is the wrong filter for them; pull them regardless of age. The combined dump is large — **delegate the parse to a subagent**: have it EXCLUDE every identifier in `state.actioned` + the dedup set — **EXCEPT** entries whose `actioned` value starts with `failed:` (a single prior failure → retry-eligible; include these as lower-ranked retry candidates). `failedx2:` / shipped / `resolved:` entries stay excluded. Apply `<selection-criteria>` (tagging epic-sourced candidates so the Recent exemption applies to them), and return a RANKED shortlist as a **strict JSON array**, top-ranked first, nothing else — each element `{"identifier":"DEV-NNNN","repo":"<repo>","one_line_fix":"<the concrete fix>","assignee":"unassigned|you","rank":<int, 1=best>,"source":"backlog|bulldozer-1offs"}`. **Validate the returned array before using it:** `jq -e 'type=="array" and (length==0 or (.[0]|has("identifier") and has("repo") and has("one_line_fix") and has("rank")))'`. On validation failure, re-ask the parse-subagent ONCE for JSON-only; if it still doesn't parse, treat it as an empty shortlist for this wake. Keep that validated shortlist in hand for the drain loop.

## Step 2 — DRAIN LOOP (the core — one fresh subagent per ticket, back-to-back)

Loop over the shortlist, top-ranked first:

1. **Stop conditions (check before each ticket):** `shipped >= target` → done (Step 3 auto-stop, target met). No more shortlist candidates → done (queue dry this wake). A per-wake safety cap of `2 × target` subagents spawned → done (anti-runaway; the heartbeat resumes next hour).
2. **Skip** any candidate now in the dedup set, or in `state.actioned` UNLESS its value is a single `failed:` (retry-eligible — let it through for its one retry). State may have advanced mid-wake — so **re-run the LOCAL half of the dedup scan** (ship-flow markers + the `~/code/` worktree/branch loop; cheap, no API) right before each spawn, catching a ticket another terminal started mid-drain. The `gh` PR scan stays once-per-wake.
3. **Spawn ONE fresh subagent** with the `<ticket-subagent-prompt>` below, parameterized for this ticket — it does the entire ticket in its own context and returns the validated JSON result object (`RESULT_SCHEMA` = the RETURN CONTRACT object in that prompt).
   - **PREFERRED — Workflow tool.** Spawn via the Workflow tool as `agent(prompt, {schema: RESULT_SCHEMA})` so JSON validation + retry happen at the tool layer. Gotchas: workflow `args` may arrive as a **string** → coerce/JSON-parse before use; `export const meta` MUST be the first statement in the workflow module. Check Workflow availability **once per wake** and note in the report which path you're on.
   - **FALLBACK — Agent tool** (`subagent_type: general-purpose`) if Workflow is unavailable this session. Require the worker's fenced JSON result object and validate it with `jq -e` on the required keys: `jq -e '.status and .ticket and (.status!="shipped" or (.evidence.verify_cmd and .evidence.verify_result)) and (.status!="resolved" or .evidence.still_outstands_proof)'`. On validation failure, do **ONE** re-ask via **SendMessage** ("return only the JSON object, no prose") and re-validate; if it still fails, record `failed:malformed` (stays retry-eligible under the existing `failed:` → one-retry escalation) and continue to the next ticket.
4. **Validate, THEN record** into `/tmp/bulldozer-state.json` immediately (so a crash mid-drain doesn't lose progress and the next wake dedups correctly). The result MUST have passed validation first (Workflow validates at the tool layer; the Agent fallback uses the `jq -e` gate above) — an unvalidated/invalid result is treated as `failed:malformed`, never recorded as shipped/resolved. Then:
   - **Store the full validated result object** under the top-level `results` map, keyed by ticket: `results[<id>] = <the JSON object>` (preserves evidence/pr/branch/memory_id for audit + the ledger).
   - **Update `actioned`** as the quick-skip dedup index (its string conventions UNCHANGED): `status==shipped` → `actioned[<id>]="<repo>#<PR>"`, `shipped += 1`; `status==resolved` → `actioned[<id>]="resolved:<reason>"`, `resolved_no_pr += 1`; `status==failed` (incl. `failed:malformed`, or the subagent returned null/errored) → **escalate by prior state:** if `actioned[<id>]` already starts with `failed:` (this WAS its one retry) → set `actioned[<id>]="failedx2:<reason>"` (now permanent, never retried again); otherwise → set `actioned[<id>]="failed:<reason>"` (first failure → eligible for exactly one retry on a later wake). Log failures for the report. **Do NOT abort the loop — continue to the next ticket.** One bad ticket never kills the chain.
   - **Append one line per result to the automation ledger IF the helper exists** (degrade silently if absent): `if [ -x ~/.claude/hooks/ledger-append.sh ]; then ~/.claude/hooks/ledger-append.sh "$RESULT_JSON" || echo "WARN: bulldozer result row REJECTED — <ticket> is missing from the durable record" >&2; fi` — appends one compact JSON line per result to `~/.claude/automation-ledger.jsonl`. The `[ -x … ]` guard keeps this a silent no-op while `ledger-append.sh` is absent, but the `||` hangs off the INVOCATION so a REJECTED payload (the helper exits 1 and writes nothing) is noise rather than a silent gap in the scorecard — say the WARN out loud in the wake report. The emit never gates the drain: continue to the next ticket either way.
5. Print a one-line progress note (`<id> → <result> | shipped <s>/<target>`) and **continue to the next ticket**.

Run subagents **sequentially** (one ticket at a time) — they push branches, share the reviewer-CLI's hourly quota, and create worktrees; parallel would collide and blow the quota. (If you ever parallelize, cap at 2 and only on different repos.)

<ticket-subagent-prompt>
Spawn with this instruction (fill in the ticket id, repo, the one-line fix, the ticket's branch-name hint, and the canonical clone path). It follows the six-section worker-brief contract in `skills/briefs` — CONTEXT / TASK / CONSTRAINTS / RETURN CONTRACT / VERIFICATION REQUIREMENT / STOP CONDITIONS — filled below. **If any instruction here conflicts with what you infer, follow the instruction.**

"You are a bulldozer worker; this brief obeys the `skills/briefs` six-section contract.

CONTEXT: You are ONE isolated, disposable subagent in an orchestrator's back-to-back drain loop. The orchestrator will NOT see your working context — only the JSON result object you return — so everything it must record or audit has to live in that object. Ticket: <id> — <title/one-line fix>. Repo: <repo>; canonical clone at ~/code/<repo>. Trust the repo's DEFAULT branch as ground truth (`gh repo view <owner>/<repo> --json defaultBranchRef` — the default is NOT always `main`, it's often `develop`); do not trust any pre-existing local working tree. Recent tickets drift fast (spun off in-flight reviews), so a meaningful fraction are already fixed/drifted and should resolve WITHOUT a PR — that is the still-outstands gate working, not a failure.

TASK: Take EXACTLY this one ticket end-to-end to an OPENED PR (never merged) against the repo's default branch — or, if it no longer outstands, to a precise `resolved` diagnostic — then return the JSON result object. One ticket = one branch = one worktree = one PR; do NOT widen into a second ticket or an epic.

CONSTRAINTS:
- **Confirm-still-outstands FIRST (the gate).** Pull the full ticket (`get_issue`, relations; capture its branch-name hint), then read the REAL code on the repo's DEFAULT branch. If it's already fixed on the default branch, OR the named symbols/files don't exist (drifted), OR the target code only lives on an unmerged PR branch (stacked-on-unmerged) → do NOT fix it: post a precise diagnostic ticket comment, set state if warranted (already-fixed → Done), and RETURN status `resolved` with the required `still_outstands_proof` evidence.
- **Fix recipe (only if it still outstands).** Refresh the local base ref (`git -C ~/code/<repo> fetch origin <DEF>:<DEF>`), create a worktree off origin/<DEF> with the ticket's branch-name hint (`git worktree add -b <branch> ~/code/<repo>.<id> origin/<DEF>`), and make the SMALLEST correct change matching existing precedent (one-brain: fix the whole class, no lane-fix).
  - **Worktrees live beside the canonical clones, never in the system temp dir.** macOS reaps that directory, and anything the worktree shares with the primary clone dies with it — we once had a pnpm virtual store left pointing at a reaped temp worktree, which left two repos unable to run a single test **on `main`, in the primary clone**, long after the run that caused it had finished. `<checkout-root>/<repo>.<slug>` is the convention the `check-no-edit-on-main` hook already directs you to, and it is not reaped.
- **Ship via the PRlaunch skill** for that worktree: deep-review → reviewer CLI with `--base <DEF>` (skip-and-record if rate-limited/slow) → outcome eval grading what the USER receives (N/A for pure internal plumbing) → re-gate → push → open a READY PR with `--base <DEF>`, a past-tense Testing section, and a ticket link (`Closes <id>`). **NEVER merge.**
- **Update the tracker:** In Progress + assign yourself on branch-create (ONLY if unassigned or already yours — never steal or reassign someone else's ticket), In Review on PR open, then `get_issue` to verify the PR attachment landed (attach explicitly if missing).
- **Save a memory** via your persistent-memory MCP if you have one (item_id `bulldozer-<id>`, title `Bulldozer: <id> <short> → <repo>#<PR>`): the ticket, repo+PR#+branch, the actual fix (file:line + what changed), how it was VERIFIED (the empirical check), and any non-obvious gotcha. 3-6 tight sentences. (Skip if you have no memory tool; set memory_id null.)
- **Gotchas (verbatim — a paraphrased gotcha is re-learned the hard way):** default branch ≠ main (often `develop`) — branch off + PR into the actual default; refresh the local base ref before the reviewer CLI or it sees phantom 'Too many files'; the reviewer CLI is ~3/hr + slow → one try then skip-and-record; FE verify by symlinking the canonical clone's node_modules into the /tmp worktree then eslint/vitest/tsc; Python verify via the canonical `.venv/bin/python` with dummy DATABASE_URL/secrets; pre-existing red tests / env-only failures are out of scope — verify only YOUR change; grade outcomes not transport.

RETURN CONTRACT: return ONLY this JSON result object, no prose around it (under the preferred Workflow transport it is schema-validated at the tool layer; under the Agent fallback the orchestrator validates it with `jq -e`):
```json
{"status":"shipped|resolved|failed","ticket":"<id>","repo":"<repo>","pr":1234,"branch":"<branch>","evidence":{"still_outstands_proof":"<git show origin/<DEF>:file:line → what it shows>","verify_cmd":"<command you ran>","verify_result":"<observed summary>"},"reason":"<for resolved/failed>","memory_id":"bulldozer-<id>"}
```
Key meanings: `status` = shipped (PR opened) | resolved (no PR needed) | failed (blocked/unverifiable). `pr` (int) + `branch` are REQUIRED for `shipped`, null for `resolved`/`failed`. `reason` = one line for `resolved`/`failed` (what you found / what blocked you); null for `shipped`. `memory_id` = the item_id you wrote, or null if none. The `evidence` sub-fields are gated by status — see VERIFICATION REQUIREMENT.

VERIFICATION REQUIREMENT (evidence before assertion — you may not claim an outcome you didn't observe):
- **`resolved` REQUIRES `evidence.still_outstands_proof`** quoting ACTUAL default-branch code by `file:line` (e.g. `git show origin/<DEF>:path/file.py` around the line) showing the premise is gone / already fixed / drifted. No such proof → you may NOT return `resolved`.
- **`shipped` REQUIRES `evidence.verify_cmd` + `evidence.verify_result`** from a check you ACTUALLY ran and observed, AND a genuinely-open PR. A syntax-only check (e.g. `ast.parse`) does NOT qualify — a behavioral change must run the affected suite (FE: eslint/vitest/tsc against the symlinked node_modules; Python: the canonical `.venv/bin/python` running the affected tests with dummy env). Grade what the USER receives, not that the pipeline ran. For pure internal plumbing where the outcome eval is N/A, record that N/A + the structural check you ran in `verify_result` and say so in the PR's Testing section.
- **If you cannot produce the required evidence, status MUST be `failed`** with `reason` naming what blocked verification — never assert a fix or resolution you didn't watch work.

STOP CONDITIONS (bail instead of thrashing; ALWAYS return the JSON so the orchestrator can act):
- Premise contradicted (already fixed / drifted / stacked-on-unmerged) → RETURN `resolved` with `still_outstands_proof`; do NOT fix.
- After ~2 honest attempts to make the fix verify — or the reviewer CLI/env blocks you, or a pre-existing red suite you can't attribute to your change — STOP and RETURN `failed` with `reason` = the blocker + what you tried. Do not widen scope hunting for a workaround.
- Never merge; never reassign/steal a ticket; never stack a PR on an unmerged base (resolve-no-PR instead); never widen into a second ticket or an epic."
</ticket-subagent-prompt>

## Step 3 — After the drain: update state, report, decide

Set `no_progress_streak = 0` if this wake shipped or resolved ≥1 ticket, else `+= 1`. Write `last_iter_at = now`, persist state. Print a compact batch report:
```
Bulldozer wake — armed cron <id>
| <id> | <repo>#<PR> / resolved:… / failed:… | <one-line> |
...
shipped <s>/<target> · resolved-no-pr <r> · this wake: <k> shipped, <m> resolved, <f> failed
```
Then:
- **`shipped >= target`** → AUTO-STOP (target met): CronDelete the `/bulldozer` job, final summary, exit.
- **`no_progress_streak >= 3`** → AUTO-STOP (stalled): 3 consecutive wakes cleared nothing (queue dry or all-drifted). CronDelete, `AUTO-STOPPED (stalled)`, exit.
- **otherwise** (queue dry this wake but target not met, or work remains) → leave the heartbeat armed; the next hourly fire re-drains with newly-filed tickets. Report `WAITING (heartbeat armed)` and exit.

**Context reset between wakes (compact-per-fire).** Each wake re-enters the SAME session (the heartbeat re-injects `/bulldozer`). Per-ticket detail is already isolated in disposable subagents, but the orchestrator's own context (per-wake reports + one-liners) still grows wake-over-wake. So **only in the WAITING branch** (heartbeat staying armed → another wake is coming), arm a ONE-SHOT `/compact` ~2 min out to bound this session. **Skip it on either AUTO-STOP** (target met / stalled — no next wake), so the one-shot never orphans. The scheduler honors an injected `/compact` (verified), and the next wake re-derives everything from `/tmp/bulldozer-state.json` + the tracker, so an aggressive summary is safe:
```bash
COMPACT_CRON=$(date -v+2M +'%M %H %d %m *' 2>/dev/null || date -d '2 minutes' +'%M %H %d %m *')
```
Then arm it: `CronCreate(cron: "$COMPACT_CRON", recurring: false, durable: false, prompt: "/compact Context-reset between bulldozer wakes. Drop ALL prior wake reports, ticket detail, and command transcripts — the next wake re-derives everything from disk. KEEP ONLY: (1) this is the /bulldozer hourly heartbeat drain loop; (2) state file /tmp/bulldozer-state.json holds target/shipped/actioned/streak; (3) the heartbeat cron is armed and the drain is mid-flight — the next wake fires on schedule.")`. It fires once the wake goes idle (never interrupts a running wake), compacts, self-deletes. Add `compact armed for <HH:MM>` to the report. NOTE: bounds growth, not a full reset — a manual session restart every few days is still healthy.

</process>

<selection-criteria>
ELIGIBLE only if ALL hold (AND-ed — **any single failed criterion disqualifies, no matter how strong the others are**):
1. **Recent** — created within `days` (default 7). Lower drift. **Exemption:** a ticket sourced from a project's "Bulldozer 1-offs" epic (Step 1's second query) is EXEMPT from this criterion — it was deliberately parked (possibly weeks old), and staleness there doesn't imply the drift an aging plain-backlog ticket would. Criteria 2-6 still apply to it in full.
2. **Unblocked** — no open blockers.
3. **Assignee-safe** — UNASSIGNED or already yours. Anyone else → SKIP, never reassign/steal.
4. **Well-specified** — concrete description with a clear fix or acceptance criteria. Vague one-liner is NOT eligible.
5. **Small scope** — bug fix, config/one-liner, single-function change, guard, missing schedule/registry/rate-card entry, a small endpoint mirroring a sibling. NOT: epics, parent issues, spikes/research, "needs design", prod-ops/migrations/CI-workflow/branch-protection changes, or anything whose target code isn't on the default branch.
6. **Not already actioned** (`state.actioned`) and **not in-flight** (ship-flow markers / worktrees dedup set).

Prefer the SMALLER ticket when unsure.
</selection-criteria>

<gotchas>
- **Default branch ≠ main** (often `develop`) — the subagent branches off + PRs into the repo's actual default.
- **Refresh the local base ref before the reviewer CLI** or it reports phantom "Too many files".
- **The reviewer CLI is ~3/hr and slow** — one attempt, skip-and-record (cloud review on the pushed PR + /babysit-prs backstop). Because subagents run sequentially and each may spend one CLI slot, a long drain exhausts the hourly CLI quota — expected; later tickets skip-and-record and rely on cloud review.
- **FE verify:** symlink the canonical clone's node_modules into the /tmp worktree; **Python verify:** canonical `.venv/bin/python` + dummy env. **Pre-existing failures are out of scope.**
- **Grade outcomes, not transport.**
- **The careful hook defers destructive deletes** in unattended runs — don't depend on `rm`.
- **Recent tickets drift fast** (spun off in-flight reviews) — the subagent's still-stands gate resolves a meaningful fraction without a PR; that's the gate working.
</gotchas>

<guardrails>
- The DRAIN LOOP keeps going ticket-after-ticket within a wake — a single ticket (slow, failed, or resolved-no-PR) NEVER stalls the chain or makes it wait an hour. Only `shipped >= target`, a dry queue + 3-wake stall, or the per-wake safety cap stops it.
- One ticket per SUBAGENT (fresh isolated context); the orchestrator only holds the compact JSON results + state, so it stays lean across an arbitrarily long drain.
- A subagent that errors / returns null / returns unvalidatable JSON → record `failed` (`failed:malformed` for a bad or absent JSON result), continue. Never abort the loop on one ticket.
- NEVER merge. NEVER reassign/start someone else's ticket. NEVER widen into an epic. NEVER stack on an unmerged base (resolve-no-PR instead).
- NEVER re-arm the cron after AUTO-STOP — re-invoke `/bulldozer` yourself (which starts a FRESH batch: `shipped` resets to 0, `actioned` carries over so nothing is re-PR'd).
- Run subagents sequentially (shared reviewer-CLI quota + worktree/push collisions).
</guardrails>

<context>
Args (target / days / no-loop / stop): $ARGUMENTS
</context>
