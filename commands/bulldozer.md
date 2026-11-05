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
jq -c '{"freshened_batch":0,"freshen":{},"pending_close":{}} + .' /tmp/bulldozer-state.json 2>/dev/null || echo '{"target":10,"shipped":0,"resolved_no_pr":0,"days":7,"actioned":{},"results":{},"no_progress_streak":0,"freshened_batch":0,"freshen":{},"pending_close":{}}'
```
- No file → fresh state from `$ARGUMENTS` (target/days) or defaults (`shipped=0`, `actioned={}`). A file written before `freshened_batch`/`freshen`/`pending_close` existed gets them defaulted on load; the file's own values win.
- `$ARGUMENTS == "stop"` → CronDelete the bulldozer job, `rm -f /tmp/bulldozer-state.json`, report totals, **exit**.
- **`actioned` is the dedup ledger** (every ticket ref ever shipped/resolved/failed); it carries across batches and is ONLY wiped by `stop`. `shipped` / `resolved_no_pr` / `no_progress_streak` / `freshened_batch` are **per-batch** counters. **`results`** is the parallel audit map — the full validated result object per ticket (evidence/pr/branch/memory_id); like `actioned` it carries across batches and is only wiped by `stop`. **Ledger value conventions** (these govern whether a ticket is re-pickable): `<repo>#<PR>` = shipped → permanent skip; `resolved:<reason>` = resolved → permanent skip; `failed:<reason>` = failed ONCE → **retry-eligible** (gets exactly one more attempt on a later wake); `failedx2:<reason>` = failed twice → permanent skip, never retried again.
- **`freshen`** is the Step 1b review record, keyed by ticket: `freshen[DEV] = {"at":"<UTC ISO>","verdict":"…","build_ok":true|false,"correction":"…|null","evidence":"…","comment_url":"…|null"}`. Like `actioned` it carries across batches and is only wiped by `stop`. It is the ONLY source for a worker brief's TICKET-REVIEW VERDICT block, and the only way a Step 1b review makes a ticket fresh (criterion 1). It stays `{}` while freshen is off.
- **Freshen is opt-in and off by default.** It needs a second model family, so it runs only when `BULLDOZER_FRESHEN=1` AND `CODEX_REVIEW_ENABLED=1` are both set and the `ticket-review` skill is installed: `[ "${BULLDOZER_FRESHEN:-}" = 1 ] && [ "${CODEX_REVIEW_ENABLED:-}" = 1 ] && [ -f ~/.claude/skills/ticket-review/SKILL.md ] && echo on || echo off`, once per wake. Off → every eligible ticket is fresh (criterion 1), `stale` stays empty, and Step 1b only retries `pending_close`.
- **NEW-BATCH RESET — re-invoking `/bulldozer` after a finished batch starts a FRESH drain (the common case).** If the loaded state has `shipped >= target` with `pending_close` empty and no `/bulldozer` job in `CronList` (a prior batch already met its target → Step 3 AUTO-STOP deleted its heartbeat, so this is a fresh manual re-invocation) **OR** `stopped` is set (a Step 3 stalled auto-stop also deleted the heartbeat, so this too is a manual re-invocation) **OR** `last_iter_at` is > 6h old (prior batch stale/abandoned) — except when `shipped >= target` with `pending_close` nonempty and not `stopped`, which is always a CLOSE-RETRY WAKE (below) whatever its age, since a delayed heartbeat wake must still finish its closes rather than drain a new batch: treat this as a NEW batch → set `shipped=0`, `resolved_no_pr=0`, `no_progress_streak=0`, `freshened_batch=0`, clear `stopped`, **KEEP `actioned` + `results` as-is** (so already-done tickets are never re-picked → no duplicate PRs), apply any new `target`/`days` from `$ARGUMENTS`, then proceed to drain. **Do NOT auto-stop at load time** — the only AUTO-STOP is in Step 3, after a *live* drain hits target.
- **INTERRUPTED TARGET-MET STOP** — `shipped >= target` with `pending_close` empty, not `stopped`, recent `last_iter_at`, and a `/bulldozer` job still in `CronList`: Step 3 persisted a met target but never deleted the heartbeat, so this is a heartbeat wake, not a new batch. Do NOT reset: keep all counters, skip Steps 1-2, and go to Step 3, whose target-met AUTO-STOP deletes the heartbeat.
- **CLOSE-RETRY WAKE** — `shipped >= target` with `pending_close` nonempty, not `stopped`, at any `last_iter_at` age (checked before the 6h stale reset): Step 3 kept the heartbeat armed only to finish those closes. Keep all counters, run only Step 1b's `pending_close` retry (no review, no drain), then go to Step 3.
- **Otherwise** (recent `last_iter_at`, `shipped < target`) → RESUME the in-flight batch: keep all counters and keep draining toward `target`.
- **Persist the resolved state now**, before Step 1 (`target`, `days` and the reset counters included). Step 1 reads `days` from the file, and on a first wake, or the first after `stop`, there is no file until something writes it.

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
# --limit MUST stay above the true open-PR count: `gh search prs` defaults to
# best-match order, so the sort is pinned newest-first with --sort/--order, and it
# SILENTLY truncates at the cap, hiding the OLDEST in-flight
# PRs — exactly the long-running ones most likely to have lost their worktree.
(ls ~/.claude/prlaunch-ok/ 2>/dev/null; \
 for r in ~/code/*/; do git -C "$r" worktree list 2>/dev/null; done; \
 gh search prs --owner <your-org> --owner <your-user> --author "@me" --state open --sort created --order desc --json title -q '.[].title' --limit 300 2>/dev/null) \
  | grep -oiE "dev-?[0-9]+" | tr 'A-Z' 'a-z' | sed 's/dev-*/dev-/' | sort -u
```
The `gh search prs` scan (adapt `<your-org>`/`<your-user>`; PR titles must carry the ticket ref — the ship flow's convention) runs ONCE per wake (rate-limit friendly); if it errors/returns nothing, proceed on the other two sources — it's a belt-and-suspenders layer, not a gate.

Query your tracker for backlog tickets created in the last `days` (for Linear: `list_issues` state:backlog, createdAt:-P<days>D, orderBy:createdAt, generous limit). **Second source — each project's standing "Bulldozer 1-offs" epic:** find the epic per project by exact title `Bulldozer 1-offs` (`list_issues` title match, state Epics — see the README's "Work taxonomy (Linear conventions)" section), then list its open children (`list_issues` parentId:<epic id>, state not Done/Cancelled). These are deliberately-parked CR-deferred 1-offs — not fresh backlog, so the `days` window is the wrong filter for pulling them; pull them regardless of age, and let criterion 1 (Fresh) decide whether each one is buildable this wake or waits for the Step 1b freshen tax. The combined dump is large — **delegate the parse to a subagent**: have it EXCLUDE every identifier in `state.actioned` + the dedup set — **EXCEPT** entries whose `actioned` value starts with `failed:` (a single prior failure → retry-eligible; include these as lower-ranked retry candidates). `failedx2:` / shipped / `resolved:` entries stay excluded. Before spawning it, write the **reviewed-recently set** to a file it can read. A review makes a ticket fresh only when it said the ticket is buildable as written or as corrected: an `agree` verdict in the `ticket-review` skill's coverage ledger, or a `build_ok` Step 1b record in `state.freshen`. `revise`, `uncertain` and `superseded` verdicts from other reviews do NOT count, because nothing carries their correction to the worker. The run_id is a UTC timestamp; anything else is skipped. If the cutoff cannot be computed, write an empty file, so freshness fails closed:
```bash
SCRATCH="${SCRATCH:?set SCRATCH to your session scratchpad dir}"
DAYS=$(jq -r '.days // 7' /tmp/bulldozer-state.json)
C=$(date -u -v-"${DAYS}"d +%Y%m%dT%H%M%SZ 2>/dev/null || date -u -d "-${DAYS} days" +%Y%m%dT%H%M%SZ 2>/dev/null)
if [ -z "$C" ]; then : > "$SCRATCH/reviewed_recent.txt"; echo "WARN: freshness cutoff failed; no ticket is fresh by review this wake" >&2; else
  CI=$(date -u -v-"${DAYS}"d +%FT%TZ 2>/dev/null || date -u -d "-${DAYS} days" +%FT%TZ)
  { jq -r --arg c "$CI" '(.freshen//{}) | to_entries[] | select(.value.build_ok==true and .value.at>=$c) | .key' /tmp/bulldozer-state.json
    find "${TICKET_REVIEW_HOME:-$HOME/.claude/ticket-reviews}" -mindepth 2 -maxdepth 2 -name coverage.json 2>/dev/null | while read -r f; do
      jq -r --arg c "$C" '(.reviewed//{}) | to_entries[] | select(.value.verdict=="agree") | select((.value.run_id//"")|test("^[0-9]{8}T[0-9]{6}Z")) | select(.value.run_id>=$c) | .key' "$f" || echo "WARN: unreadable $f" >&2
    done
  } | sort -u > "$SCRATCH/reviewed_recent.txt"
fi
```
Put this wake's freshen value (`FRESHEN=on` or `FRESHEN=off`, from Step 0) in its brief, and have it apply `<selection-criteria>` criteria 2-6 to every pooled ticket, then split the passes by criterion 1: **fresh** (created within `days`, in `reviewed_recent.txt`, or any pass at all while freshen is off) goes to `shortlist`, ranked; **stale** (passes 2-6, not fresh) goes to `stale`, oldest `createdAt` first. It returns ONE **strict JSON object**, nothing else:
```json
{"shortlist":[{"identifier":"DEV-NNNN","repo":"<repo>","one_line_fix":"<the concrete fix>","assignee":"unassigned|you","source":"backlog|bulldozer-1offs","gitBranchName":"<the tracker's branch-name hint>","rank":1}],
 "stale":[{"identifier":"DEV-NNNN","repo":"<repo>","one_line_fix":"<the concrete fix>","assignee":"unassigned|you","source":"backlog|bulldozer-1offs","gitBranchName":"<the tracker's branch-name hint>","created":"YYYY-MM-DD","title":"…"}],
 "pool":{"total":0,"ineligible":0,"eligible_fresh":0,"eligible_stale":0}}
```
`pool` counts **Bulldozer 1-offs children only** (`source:"bulldozer-1offs"`, including a recent backlog ticket that is also one): it is the size of bulldozer's own queue. Plain recent backlog still drains through `shortlist` but is never counted in `pool`.
**Validate it before using it:** `jq -e '(.shortlist|type=="array") and (.stale|type=="array") and (.stale|all(has("identifier") and has("repo") and has("one_line_fix") and (.gitBranchName|type=="string"))) and (.pool.eligible_fresh|type=="number") and (.pool.eligible_stale|type=="number") and (.shortlist|all(has("identifier") and has("repo") and has("one_line_fix") and has("rank") and (.gitBranchName|type=="string"))) and ((.shortlist+.stale)|map(.identifier)|length==(unique|length))'`. On validation failure, re-ask the parse-subagent ONCE for JSON-only; if it still doesn't parse, treat it as an empty shortlist and empty stale list for this wake (report the pool counts as `?`). With freshen off, nothing will ever review a `stale` entry, so append any it returned to the end of `shortlist` and add `eligible_stale` into `eligible_fresh` before going on. Keep the validated object in hand for Step 1b, the drain loop and the report. For every shortlist ticket whose `state.freshen` entry has `build_ok: true` and an `at` inside the `days` window, attach that entry: its worker brief gets the TICKET-REVIEW VERDICT block from it. An entry that is `failed`, `uncertain`, blocked or out of the window is never attached; a ticket that is fresh for another reason is built from its ticket text.

## Step 1b — Freshen tax (per batch, before the drain; opt-in)

First, every wake: retry every `state.pending_close` entry (step 3, `superseded`) and remove each once it succeeds. Count failed retries in the entry's `attempts`; on the third failure drop it from `pending_close` and name it in the wake report as `close failed — finish by hand`, so one ticket the tracker will not close cannot hold the heartbeat, the stall streak and every later batch. With freshen off, that retry is all of Step 1b.

A stale ticket was written against code that has since moved, so a worker would build against a premise nobody re-checked. With freshen on, each batch pays a tax to fix that: it freshens **half as many stale tickets as its target**, `quota = ceil(target / 2)` (the default target of 10 shipped PRs → **up to 5** freshens for the whole batch, not per ticket and not per wake), **oldest first**, whether or not the fresh shortlist is empty. `state.freshened_batch` counts what this batch has spent. A wake takes `quota − freshened_batch` tickets, so the first wake usually pays the whole tax and later wakes in the batch skip the review. Skip it also when no stale ticket is left to take.

1. Walk `stale` in order and take the first `quota − freshened_batch` that are not in the dedup set and are not blocked by `state.freshen`. An entry blocks by its verdict: a `failed` entry blocks while its `at` is less than 24 hours old, so a Codex outage is retried the next day; every other verdict blocks while its `at` is inside the `days` window. That rule gives each ticket one review per window, so a ticket whose review keeps failing, or that came back `uncertain` or blocked, cannot hold the front of the queue every wake.
2. Spawn ONE review-orchestrator subagent with a six-section brief (`skills/briefs`). TASK: run the `ticket-review` skill as `tickets <ids> --via codex`, following that skill and its `references/codex-handoff.md` literally, with its run records under the scope directory `bulldozer-freshen`. CONSTRAINTS: the Codex lanes are read-only, as that handoff defines them; the subagent is the single writer of review comments and the coverage ledger, and writes nothing else: no ticket state, assignee or description edits, no new issues, no code, no merges, and never `git stash`. It verifies every non-agree verdict against pinned source and posts comments only on material disagreements. RETURN CONTRACT: ONLY a JSON array, one object per ticket: `{"ticket":"DEV-NNNN","verdict":"agree|revise|superseded|uncertain|failed","build_ok":true|false,"correction":"<what the builder must do, or null>","evidence":"<sha:path:line + what it shows>","comment_url":"<or null>"}`. `build_ok` is true only for `agree`, or for `revise` whose corrected plan is still one small PR in the same repo. STOP CONDITIONS: a Codex lane that fails twice → those tickets come back `failed`; Codex wholly unavailable → every ticket `failed`, reason in `correction`.
3. **Validate the array before it touches state or the tracker**, with the requested IDs in `REQ` (a JSON array): `jq -e --argjson req "$REQ" 'type=="array" and (map(.ticket)|sort)==($req|sort) and all((.build_ok|type=="boolean") and (.evidence|type=="string") and has("correction") and has("comment_url")) and all(if .verdict!="failed" then (.evidence|length>0) else true end) and all(.verdict|IN("agree","revise","superseded","uncertain","failed")) and all(if .build_ok then (.verdict|IN("agree","revise")) else true end) and all(if .build_ok and .verdict=="revise" then (.correction|type=="string" and length>0) else true end)'`. Exact equality means no unrequested ticket, no duplicate and no missing one. On failure, re-ask the subagent ONCE for JSON-only; if it still fails, treat every requested ticket as `failed` for this wake: write `state.freshen[DEV] = {"at":<now>,"verdict":"failed","build_ok":false,"correction":null,"evidence":"malformed review result","comment_url":null}` for each (so the one-review-per-window rule moves the next wake on to other tickets), write nothing to `actioned` or the tracker, and do not count them against the quota. Then re-check each `build_ok: true` corrected plan against `<selection-criteria>` criterion 5 yourself: a correction that steers into a migration, a CI workflow, prod-ops, another repo, or code not on the default branch flips it to `build_ok: false`. Then record every verdict in `state.freshen[DEV]` with `at` = now, write each `superseded` ticket's `state.pending_close[DEV]` (shape below) in that same update, and persist state once, then act on it:
   - `build_ok: true` → the ticket is now fresh. Insert its `stale` object, which carries every shortlist field, into this wake's shortlist after the fresh first attempts and ahead of every single-`failed:` retry, so the Rank rule's retry-last order holds (a promoted ticket that is itself a `failed:` retry goes with the retries). Its brief gets the TICKET-REVIEW VERDICT block from `state.freshen`, on this wake or any later one.
   - `superseded` → there is nothing for bulldozer to build. Its `state.pending_close[DEV]` (shape below) went out in the same persist as its `freshen` verdict, so a wake that dies mid-close still retries it. Then record `actioned[DEV]="resolved:superseded-<one line>"` and `results[DEV]` with the evidence as `still_outstands_proof`, `resolved_no_pr += 1`, and append the result to the automation ledger as Step 2.4 does. Then set the tracker state by what the evidence shows: already fixed on the default branch → Done, the same rule a worker follows; covered by another ticket (duplicate scope or superseding work) → mark it a duplicate of that ticket, or Canceled with a link to it, never Done. Comment with a link to the review comment either way. `state.pending_close[DEV] = {"state":"Done|Duplicate|Canceled","of":"<superseding ticket or null>","comment_url":"…"}`; delete it only after the tracker update and the ledger append both succeed, and otherwise name it in the wake report. Every wake retries it at the top of Step 1b; `actioned` already stops bulldozer from building the ticket.
   - `revise` with `build_ok: false` (it waits on an unmerged PR, or it outgrew bulldozer scope), `uncertain`, or `failed` → it stays out of `actioned` and out of the shortlist, and stays stale. The Codex comment on the ticket says why. It is not reviewed again until its `freshen` entry leaves the window.
4. Add the tickets that got a verdict other than `failed` to `state.freshened_batch` and persist state; a `failed` review does not use up the quota. Count for the report: `freshened` = this wake's such tickets; `promoted` = `build_ok: true` tickets; `superseded`.

The tax scales with the batch, one freshen per two shipped PRs targeted, because each Codex review lane costs a review's worth of time and tokens: it keeps the stale pool shrinking every batch without turning a batch into a review session.

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
   - **Append one line per result to the automation ledger IF the helper exists** (degrade silently if absent): `if [ -x ~/.claude/hooks/ledger-append.sh ]; then LEDGER_SKILL=bulldozer ~/.claude/hooks/ledger-append.sh "$RESULT_JSON" || echo "WARN: bulldozer result row REJECTED — <ticket> is missing from the durable record" >&2; fi` — appends one compact JSON line per result to `~/.claude/automation-ledger.jsonl`. The `[ -x … ]` guard keeps this a silent no-op while `ledger-append.sh` is absent, but the `||` hangs off the INVOCATION so a REJECTED payload (the helper exits 1 and writes nothing) is noise rather than a silent gap in the scorecard — say the WARN out loud in the wake report. The emit never gates the drain: continue to the next ticket either way. `LEDGER_SKILL=bulldozer` is load-bearing: `$RESULT_JSON` has no `skill` field of its own, and Step 3's throughput count selects on it.
5. Print a one-line progress note (`<id> → <result> | shipped <s>/<target>`) and **continue to the next ticket**.

Run subagents **sequentially** (one ticket at a time) — they push branches, share the reviewer-CLI's hourly quota, and create worktrees; parallel would collide and blow the quota. (If you ever parallelize, cap at 2 and only on different repos.)

<ticket-subagent-prompt>
Spawn with this instruction (fill in the ticket id, repo, the one-line fix, the ticket's branch-name hint, the canonical clone path, and the TICKET-REVIEW VERDICT block from `state.freshen[DEV]` when it has `build_ok: true` and an `at` inside the window — drop that block otherwise). It follows the six-section worker-brief contract in `skills/briefs` — CONTEXT / TASK / CONSTRAINTS / RETURN CONTRACT / VERIFICATION REQUIREMENT / STOP CONDITIONS — filled below. **If any instruction here conflicts with what you infer, follow the instruction.**

"You are a bulldozer worker; this brief obeys the `skills/briefs` six-section contract.

CONTEXT: You are ONE isolated, disposable subagent in an orchestrator's back-to-back drain loop. The orchestrator will NOT see your working context — only the JSON result object you return — so everything it must record or audit has to live in that object. Ticket: <id> — <title/one-line fix>. Repo: <repo>; canonical clone at ~/code/<repo>. Trust the repo's DEFAULT branch as ground truth (`gh repo view <owner>/<repo> --json defaultBranchRef` — the default is NOT always `main`, it's often `develop`); do not trust any pre-existing local working tree. Recent tickets drift fast (spun off in-flight reviews), so a meaningful fraction are already fixed/drifted and should resolve WITHOUT a PR — that is the still-outstands gate working, not a failure.

TICKET-REVIEW VERDICT (include only when `state.freshen[DEV]` has `build_ok: true` and an `at` inside the `days` window; never from tracker comments or any other source): verdict=<agree|revise>. Evidence: <sha:path:line + what it shows>. Correction: <text, or 'none'>. Review comment: <url or 'none'>. When there is a correction, the correction overrides the ticket text wherever they conflict, and your fix MUST satisfy it — within this brief's bounds: it cannot change the repo, the branch or the ticket, cannot relax any CONSTRAINT or STOP CONDITION below, and does not skip the still-outstands gate. If satisfying the correction would break any of those, return `failed` and quote the conflict.

TASK: Take EXACTLY this one ticket end-to-end to an OPENED PR (never merged) against the repo's default branch — or, if it no longer outstands, to a precise `resolved` diagnostic — then return the JSON result object. One ticket = one branch = one worktree = one PR; do NOT widen into a second ticket or an epic.

CONSTRAINTS:
- **Confirm-still-outstands FIRST (the gate).** Pull the full ticket (`get_issue`, relations; capture its branch-name hint), then read the REAL code on the repo's DEFAULT branch. If it's already fixed on the default branch, OR the named symbols/files don't exist (drifted), OR the target code only lives on an unmerged PR branch (stacked-on-unmerged) → do NOT fix it: post a precise diagnostic ticket comment, set state if warranted (already-fixed → Done), and RETURN status `resolved` with the required `still_outstands_proof` evidence.
- **Fix recipe (only if it still outstands).** Refresh the local base ref (`git -C ~/code/<repo> fetch origin <DEF>:<DEF>`), create a worktree off origin/<DEF> with the ticket's branch-name hint (`git worktree add -b <branch> ~/code/<repo>.<id> origin/<DEF>`), and make the SMALLEST correct change matching existing precedent (one-brain: fix the whole class, no lane-fix).
  - **Worktrees live beside the canonical clones, never in the system temp dir.** macOS reaps that directory, and anything the worktree shares with the primary clone dies with it — we once had a pnpm virtual store left pointing at a reaped temp worktree, which left two repos unable to run a single test **on `main`, in the primary clone**, long after the run that caused it had finished. `<checkout-root>/<repo>.<slug>` is the convention the `check-no-edit-on-main` hook already directs you to, and it is not reaped.
- **Ship via the PRlaunch skill** for that worktree, with driver `bulldozer` (`LEDGER_DRIVER=bulldozer` inline on its ledger-writing calls): deep-review → reviewer CLI with `--base <DEF>` (skip-and-record if rate-limited/slow) → outcome eval grading what the USER receives (N/A for pure internal plumbing) → re-gate → push → open a READY PR with `--base <DEF>`, a past-tense Testing section, and a ticket link (`Closes <id>`). **NEVER merge.**
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

Set `no_progress_streak = 0` if this wake shipped or resolved ≥1 ticket or Step 1b promoted ≥1 (a review that only came back `uncertain` or blocked is not progress), else `+= 1` — except leave it unchanged when Step 1b had quota left and skipped ≥1 stale ticket inside a `failed` entry's 24-hour cooldown (whatever else it skipped), or recorded ≥1 new `failed` verdict this wake, so the stall stop cannot fire before that retry comes due. Leave it unchanged too while `state.pending_close` is still nonempty after this wake's Step 1b retry: the stall stop deletes the heartbeat, and that heartbeat is the only thing that retries a pending close, so a closure that keeps failing must never count a wake toward the stop. Write `last_iter_at = now`, persist state. Compute the throughput counts from the automation ledger, counting distinct tickets, since one worker can append more than one row:
```bash
L=~/.claude/automation-ledger.jsonl
if [ -s "$L" ] && jq -e 'select(.skill=="bulldozer" or (.skill==null and .ticket!=null))' "$L" >/dev/null; then
  for d in 7 30; do
    C=$(date -u -v-${d}d +%FT%TZ 2>/dev/null || date -u -d "-${d} days" +%FT%TZ)
    jq -r --arg c "$C" 'select((.skill=="bulldozer" or (.skill==null and .ticket!=null)) and .status=="shipped" and .ts>=$c) | .ticket' "$L" | sort -u | wc -l
  done   # → X (7 days), Y (30 days)
else
  echo "unavailable"   # report "Bulldozed: unavailable (no ledger rows)", never zeros
fi
```
The remaining counts come from Step 1's `pool`, moved forward by this wake: `E` = `eligible_fresh` + Step 1b's `promoted` − `source:"bulldozer-1offs"` shortlist tickets this wake shipped, resolved or escalated to `failedx2:` (a first `failed:` is still retry-eligible, so it stays in `E`); `S` = `eligible_stale` − `promoted` − Step 1b's `superseded` (an `uncertain` or blocked ticket is still stale); `F` = `state.freshened_batch`, out of `quota`. When Step 1's pool counts are `?`, print `Remaining 1-offs: unavailable (pool parse failed)` instead of doing the arithmetic; when this wake skipped Step 1 (a close-retry wake or an interrupted target-met stop), there is no `pool`, so print `Remaining 1-offs: unavailable (Step 1 skipped this wake)`. Print a compact batch report:
```
Bulldozer wake — armed cron <id>
| <id> | <repo>#<PR> / resolved:… / failed:… | <one-line> |
...
shipped <s>/<target> · resolved-no-pr <r> · this wake: <k> shipped, <m> resolved, <f> failed
Bulldozed: <X> in last 7 days · <Y> in last 30 days
Remaining 1-offs: <E> eligible (fresh ≤7d) · <S> stale · freshened this batch <F>/<quota>
```
With a `days` argument other than 7, print the real window in the Remaining line (for example `fresh ≤14d`). With freshen off, `S` is 0 by definition, `E` counts every eligible 1-off whatever its age, and `<F>/<quota>` reads `off`.
Then:
- **`shipped >= target`** → AUTO-STOP (target met): CronDelete the `/bulldozer` job, final summary, exit — unless `state.pending_close` is still nonempty: the heartbeat is the only thing that retries a pending close, so leave it armed, report `TARGET MET, closes pending (heartbeat armed)`, and let Step 0's close-retry wake finish them before this stop fires.
- **`no_progress_streak >= 3`** → AUTO-STOP (stalled): 3 consecutive wakes cleared nothing (queue dry or all-drifted) — never reached while a `pending_close` entry is outstanding, since such a wake does not advance the streak. CronDelete first, then persist `stopped: "stalled"` to state, so an interrupted stop never leaves a live heartbeat that would reset the batch, then `AUTO-STOPPED (stalled)`, exit.
- **otherwise** (queue dry this wake but target not met, or work remains) → leave the heartbeat armed; the next hourly fire re-drains with newly-filed tickets. Report `WAITING (heartbeat armed)` and exit.

**Context reset between wakes (compact-per-fire).** Each wake re-enters the SAME session (the heartbeat re-injects `/bulldozer`). Per-ticket detail is already isolated in disposable subagents, but the orchestrator's own context (per-wake reports + one-liners) still grows wake-over-wake. So **only in the WAITING branch** (heartbeat staying armed → another wake is coming), arm a ONE-SHOT `/compact` ~2 min out to bound this session. **Skip it on either AUTO-STOP** (target met / stalled — no next wake), so the one-shot never orphans. The scheduler honors an injected `/compact` (verified), and the next wake re-derives everything from `/tmp/bulldozer-state.json` + the tracker, so an aggressive summary is safe:
```bash
COMPACT_CRON=$(date -v+2M +'%M %H %d %m *' 2>/dev/null || date -d '2 minutes' +'%M %H %d %m *')
```
Then arm it: `CronCreate(cron: "$COMPACT_CRON", recurring: false, durable: false, prompt: "/compact Context-reset between bulldozer wakes. Drop ALL prior wake reports, ticket detail, and command transcripts — the next wake re-derives everything from disk. KEEP ONLY: (1) this is the /bulldozer hourly heartbeat drain loop; (2) state file /tmp/bulldozer-state.json holds target/shipped/actioned/streak; (3) the heartbeat cron is armed and the drain is mid-flight — the next wake fires on schedule.")`. It fires once the wake goes idle (never interrupts a running wake), compacts, self-deletes. Add `compact armed for <HH:MM>` to the report. NOTE: bounds growth, not a full reset — a manual session restart every few days is still healthy.

</process>

<selection-criteria>
ELIGIBLE only if ALL hold (AND-ed — **any single failed criterion disqualifies, no matter how strong the others are**):
1. **Fresh** — created within `days` (default 7), OR within `days` a ticket-review found it buildable: an `agree` verdict in the `ticket-review` skill's coverage ledger (`${TICKET_REVIEW_HOME:-~/.claude/ticket-reviews}/*/coverage.json`), or a `build_ok` Step 1b record in `state.freshen`. Step 1 builds the set. Either one means somebody checked the ticket against current code recently and found nothing to correct, or recorded the correction where the worker brief reads it. With freshen on, a ticket that passes 2-6 but not this one is **stale**: it is not built this wake, it waits for the Step 1b freshen tax, and a `build_ok` review there makes it fresh. That applies to "Bulldozer 1-offs" children too — being parked deliberately does not stop the code under them from moving. **With freshen off (the default)** nothing would ever review a stale ticket, so every ticket that passes 2-6 counts as fresh — the pool already admits plain backlog only while created within `days`, and 1-offs children at any age.
2. **Unblocked** — no open blockers.
3. **Assignee-safe** — UNASSIGNED or already yours. Anyone else → SKIP, never reassign/steal.
4. **Well-specified** — concrete description with a clear fix or acceptance criteria. Vague one-liner is NOT eligible.
5. **Small scope** — bug fix, config/one-liner, single-function change, guard, missing schedule/registry/rate-card entry, a small endpoint mirroring a sibling. NOT: epics, parent issues, spikes/research, "needs design", prod-ops/migrations/CI-workflow/branch-protection changes, or anything whose target code isn't on the default branch.
6. **Not already actioned** (`state.actioned`) and **not in-flight** (ship-flow markers / worktrees dedup set). One exception: an `actioned` value that starts with `failed:` (a single prior failure) passes this criterion, because that ticket gets exactly one retry; `failedx2:`, shipped and `resolved:` values do not.

Prefer the SMALLER ticket when unsure.

**Rank** the shortlist by these keys, in order: tracker priority (Urgent, High, Medium, Low, then none); smaller scope first; newer `createdAt` first; identifier as the final tiebreak. A single-`failed:` retry ranks after every first attempt. The same pool must give the same order on every wake.
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
