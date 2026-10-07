---
name: execute
description: Use when the owner names work to drive the whole way to opened PRs — one ticket, a pasted LIST of tickets, or a whole epic. Triggers "/execute", "/execute DEV-NNNN [DEV-NNNN …]", "execute this epic", "execute these", "take this/these to a PR", "build and ship this end to end". Multi-ticket input means ALL of them ship (one PR per ticket) — never a chosen subset. Use /bulldozer when the queue should pick its own tickets unattended; /PRlaunch alone when work is already built.
---

# execute — read → validate → scope → plan → build → test → ship

Drive the named work — one ticket, a pasted list of tickets, or an epic — from request to opened PR(s) in one disciplined pass, wiring together the substrate that already exists: validate-against-prod (the `/flushdeployed` mindset), `superpowers:writing-plans`, TDD, isolated worktrees, and `/PRlaunch`. **This skill is mostly orchestration — its job is to call the right existing skills in the right order and to know exactly when to stop and ask.**

**Core principle — autonomous when clear, ask only on a true fork.** Once the work is scoped and planned, run all the way to `/PRlaunch` without a check-in **as long as execution is clear**. Stop ONLY for a genuine fork (defined below) or when validation/scoping is *not* clear. "No gate needed if clear execution" — the owner's standing rule. Do not invent checkpoints; do not barrel past a real fork.

**Build always happens in an isolated git worktree** (`superpowers:using-git-worktrees`) on a fresh feature branch — never the live tree, never `main`.

## Entry point: `/execute [DEV-NNNN … | URL | epic | free-form task]`

The argument names the **work-list** — enumerate it before anything else:

- One `DEV-NNNN` / Linear URL → one unit. Fetch the issue; its body is the task.
- **Several tickets** (pasted list — space/comma/newline separated, or pasted bodies) → one unit PER ticket.
- **An epic / parent ticket** → `get_issue` the parent, then its sub-issues: every open child is a unit (blocked-by order decides sequence, not membership).
  - **Size epics to a one-session chunk (~5 tickets).** That's roughly what one `/execute` pass can validate, plan, and ship with the discipline this skill requires. An epic with materially more open children than that doesn't fit in one pass.
  - **Over that size, propose a re-chunk before building** — split the remaining children into a blocked-by chain of smaller batches (same "(stacked on #NNNN)"-style sequencing as dependent units) and confirm the chunking with the owner rather than silently grinding through all of them in one session.
  - **The epic isn't closed until its ops tail is done.** Every child ticket shipping a PR is not the same as the epic being finished — deploy, verify-in-prod, and any wrap-up/monitoring the epic implies are part of "done." Don't report or treat the epic as closed until those ops tails are confirmed, not just merged.
- Free-form ("execute: add X to Y") → that text is the task; if it contains several independent shippable units, decompose it — each unit gets its own ticket filed in phase 4.
- No argument → ask what to execute.

**The work-list is the owner's, verbatim.** N units in = N units executed = N branches = N PRs — every PR maps to exactly one ticket. Executing a subset the owner didn't pick is a scope violation, not a judgment call: never "pick the best one", never treat the rest as follow-ups, never collapse the list into one mega-PR.

## Batch runs (work-list of 2+ units)

1. **Validate first, across the whole list:** run phases 1–2 (read + premise table) for EVERY unit before building any — a contradicted premise on unit 3 shouldn't surface after units 1–2 already shipped around it.
2. **One batched ask:** collect ALL true forks found across the list into a single `AskUserQuestion`, then build straight through with the answers in hand.
3. **Then per unit, sequentially, in dependency order:** phases 3–8 with its own worktree, own `me/dev-NNNN-slug` branch, own PR. If a unit hard-depends on an earlier unit's still-unmerged PR, branch off that PR's branch and mark the title "(stacked on #NNNN)".
4. **A halted unit never kills the batch.** Contradicted premise / blocked / failed → record it, continue with the remaining units, and end with a per-unit report table: `unit → PR link / halted:<reason> / failed:<reason>`.
5. **Long list (≳4 units):** run each unit's build phases (6–8) in a fresh subagent per the `briefs` six-section contract (bulldozer's pattern) so orchestrator context stays lean — validation and forks stay in the main session where the owner can answer. Paste `/orchestrate`'s Worker gotcha pack into every build brief; its test bound (targeted + ratchet tests at `-n 2`, full suite left to CI) is what keeps N builders from exhausting the machine. Before spawning, check nothing else is already running a fleet here: `ps -axo command | grep -c '[c]laude -p --agent'` and `ps -axo command | grep -c '[-]m pytest'` (`hooks/test-admission.py` enforces the same bound mechanically).

## The pipeline

Track all 8 phases with TodoWrite. Each phase's output feeds the next; don't skip ahead.

### 1. Read
Load the full task. If a `DEV-NNNN`/URL was given, `mcp__linear__get_issue` for the real description, comments, project, and labels — don't work from the title alone. Restate the task in one or two sentences so the target is unambiguous.

### 2. Validate against prod code  ⟵ *the phase that makes /execute different*
**The ticket's premise is a claim to verify, not trust** (same stance as `/flushdeployed`). Before planning a single line, confirm against ACTUAL production code that the work is real and described correctly:
- **Does it already exist?** Run the global rule #6 discovery across all three: (a) **Linear** for the *concern* (product/SKU/epic, not the ticket name), (b) `grep` the ecosystem for the concern, (c) `memory_search` your FULL memory store, if you run one (an injected memory preview is truncated). Default stance: "it probably exists, find it."
- **Is the described state accurate?** Read the prod code the ticket assumes. Is the bug reproducible? Is the architecture as described? Is the file/route/table actually where the ticket says?
- **Two dead-ends → switch substrate** (rule #7). After ~2 failed searches for the same fact, stop varying the query and go look at the real surface (live UI via Playwright, `screencapture` + Read, Linear, the source-of-truth API), don't run a third grep.

**Log the premise check as an evidence table — don't just eyeball it.** Every *load-bearing* premise (something the plan will build on) gets at least one row before you plan a line: a claimed existence/absence, a file/route/table location, a reproducible bug, an architecture-as-described. The table IS the proof that phase 2 actually happened:

| # | Ticket claim (load-bearing premise) | Check run | Observed | Verdict |
|---|---|---|---|---|
| 1 | Bug lives in `api/x.py`'s `handle()` — returns 200 on the error branch | `git show origin/main:api/x.py` | `return JSONResponse(status_code=200)` on the except branch at L88 — matches the report | HOLDS |
| 2 | No existing staged-intake engine — build one | rule #6 discovery: `mcp__linear__list_issues {query:"intake"}` + `grep -ri "stages" api/services/chat/` | ticket **EX-2757** already shipped the guided-intake substrate (`api/services/chat/intake.py`, `Mode.stages`) | CONTRADICTED |

Rules for the table:
- **≥1 row per load-bearing premise** — existence/absence, location, reproducibility, architecture-as-described. If the plan leans on it, it gets a row.
- **Every "Check run" is a command/query you actually executed THIS session** — not "would run", not a result carried over from a past session. No row without a real invocation behind it.
- **"Observed" quotes the real output** — the actual line, record, or verdict returned, never a paraphrase of what you expected to see.
- **Any `CONTRADICTED` verdict → the HALT-and-report path below.** One contradicted load-bearing premise is enough to stop; don't quietly plan around it.
- **The completed table ships with the work** — paste it into the eventual PR body / tracker plan comment so the evidence trail persists and a reviewer can see exactly what was validated.

**If the premise is wrong** — the capability already exists, the bug doesn't reproduce, the ticket assumes an architecture that isn't there — **HALT and report.** Do not build the wrong thing. This counts as a stop condition (treat like a fork): tell the owner what prod actually shows and what you'd do instead, and wait.

### 3. Scope
Define the shippable unit: what's IN, what's explicitly OUT, and the success criteria ("done when…"). YAGNI ruthlessly. One **unit** = one branch = one PR = one ticket — and one `/execute` runs EVERY unit on the work-list. If a free-form request turns out to be several independent subsystems, that's a batch, not a fork: decompose into tickets (phase 4 files them) and execute them all — don't stop to propose, and don't shrink the list. **Stay in your lane** (rule #5): if delivering a unit requires changing a *different* repo, name it and stop for approval — don't hop repos.

### 4. Plan
Invoke `superpowers:writing-plans` to produce the implementation plan against the validated scope.
- **Spec/plan output goes to Linear, never a local file handed to the owner** (the owner's "Specs & Brainstorming Output" rule — this overrides writing-plans' default `docs/superpowers/specs/` location). Put the plan in the **ticket description**; if no ticket exists yet, file one now (right project + labels) so it exists before build — and so `/PRlaunch`'s pr-gate (which blocks PRs lacking a `DEV-NNN` link) is satisfied. An internal scratch copy of the plan is fine as your own working notes; it is never the deliverable.

### 5. Decide: ask on true forks only
A **true fork** is a decision where ALL of these hold: (a) 2+ viable paths exist, (b) they produce materially different outcomes, and (c) the choice depends on the owner's intent / product / business context that you **cannot** derive from the ticket, the code, or a sensible default. Ask these with `AskUserQuestion` — lead with your recommendation.

Everything else you decide yourself and keep moving:

| Decide yourself (NOT a fork) | Ask the owner (true fork) |
|---|---|
| Naming, file placement, formatting | Two designs with different product/UX consequences |
| Which existing util/substrate to reuse | A scope cut that drops something the owner may want |
| Test framework already in the repo | Touching another repo / shared substrate one-brain decision |
| Obvious fix for a clearly-understood bug | Premise contradicted by prod (from phase 2) |
| Anything verifiable in the codebase | A migration/destructive step that's hard to reverse |

**If there are no true forks and execution is clear, do NOT pause for approval — go straight to build.** Bundle any forks into a single batched ask; don't drip them one message at a time.

### 6. Reuse existing substrate
Build ON what exists. The phase-2 discovery already found the relevant substrate — use it (extend the service, the component, the endpoint pattern) rather than inventing a parallel mechanism. Only build from scratch after discovery genuinely came back empty (rule #6). Match the surrounding code's style, naming, and idioms.

### 7. Build
- Create the **isolated worktree + feature branch** (`superpowers:using-git-worktrees`); branch name `me/dev-NNNN-slug` so the `linear-startwork` hook flips the ticket to In Progress.
- **Run `~/.claude/hooks/collision-check.sh <TICKET>` immediately before creating the worktree** (`--repo <name>` for one half of a paired-PR ticket). Exit 1 = another agent is building it: STOP and report. Exit 3 = a probe could not run, so the answer is UNKNOWN: report it, do not build. Hits that resolve to your own worktree print as `· self` and do not count. Phases 2–6 take real time, so an earlier check can be stale by now; this re-check closes that window.
- **If `git worktree add` fails with "branch already exists", or the path is taken — STOP.** That is another agent's live workspace. Report it. Clearing the way by force is how a sibling agent's unpushed commit once ended up as an unreferenced object; there is no version of "it's only a local branch" that makes deletion safe.
- Implement with `superpowers:test-driven-development` (test → red → minimal code → green → refactor).
- For a plan with independent tasks, use `superpowers:subagent-driven-development` / `superpowers:executing-plans`.
- Hit a bug? `superpowers:systematic-debugging` — find root cause before patching. Never gut complexity to dodge a bug (rule #2: move forward, never regress).

### 8. Test, then ship
- Run the REAL tests/build and read the output. `superpowers:verification-before-completion` — evidence before any "it works" claim (rule #1: never hallucinate; verify before asserting).
- Then invoke **`/PRlaunch`**, which runs the three local quality gates (deep-review → CR CLI → outcome eval), opens the PR, and wraps up. `/execute` ends where `/PRlaunch` begins — don't duplicate its gates here.

## When to stop vs. keep going

```dot
digraph execute_stop {
  rankdir=LR;
  "Phase done" [shape=box];
  "Premise contradicted by prod?" [shape=diamond];
  "A true fork? (all 3 tests)" [shape=diamond];
  "Scope irreducibly ambiguous?" [shape=diamond];
  "HALT + report / AskUserQuestion" [shape=box];
  "Proceed to next phase" [shape=doublecircle];

  "Phase done" -> "Premise contradicted by prod?";
  "Premise contradicted by prod?" -> "HALT + report / AskUserQuestion" [label="yes"];
  "Premise contradicted by prod?" -> "A true fork? (all 3 tests)" [label="no"];
  "A true fork? (all 3 tests)" -> "HALT + report / AskUserQuestion" [label="yes"];
  "A true fork? (all 3 tests)" -> "Scope irreducibly ambiguous?" [label="no"];
  "Scope irreducibly ambiguous?" -> "HALT + report / AskUserQuestion" [label="yes"];
  "Scope irreducibly ambiguous?" -> "Proceed to next phase" [label="no — clear"];
}
```

## Quick reference

| Phase | Action | Existing skill / tool |
|---|---|---|
| 0 Enumerate | Build the work-list (ticket / pasted list / epic children); ALL units run | entry point + `mcp__linear__get_issue` |
| 1 Read | Fetch ticket / capture task (per unit) | `mcp__linear__get_issue` |
| 2 Validate | Verify premise vs prod; log the premise-evidence table; find existing | rule #6 (Linear + grep + `memory_search`), `/flushdeployed` stance |
| 3 Scope | In/out + success criteria; stay in lane | rule #5 |
| 4 Plan | Plan → **Linear ticket** (not local file) | `superpowers:writing-plans` + the owner's Linear-spec rule |
| 5 Forks | Ask only true forks, batched | `AskUserQuestion` |
| 6 Reuse | Build on existing substrate | rule #6 |
| 7 Build | Isolated worktree, TDD | `using-git-worktrees`, `test-driven-development`, `systematic-debugging` |
| 8 Ship | Verify, then PR + wrap | `verification-before-completion`, `/PRlaunch` |

## Common mistakes

- **Narrowing a batch.** The owner pastes 3 tickets or an epic → every unit ships. Picking a "preferred" ticket, deferring the rest as follow-ups, or asking which one they meant is the #1 failure of this skill — the work-list is the answer.
- **One mega-PR for a batch.** N units = N PRs; every PR maps to exactly one ticket.
- **Clearing a collision by force.** Removing another agent's worktree or deleting its branch because "it has no PR yet" is the destructive move, not the safe one — unpushed is the *most* fragile state. Report it; never remove a worktree you did not create, never `git worktree remove --force`, never `git branch -D` someone else's branch.
- **Skipping phase 2.** Planning straight from the ticket builds whatever the ticket assumed — including things that already exist or bugs that aren't real. Validate first.
- **Skipping the evidence table, or back-filling it after building.** Filling the premise table retroactively to match what you already built defeats phase 2 — the check has to precede the plan, and every row needs a command you actually ran this session.
- **Over-asking.** Pausing for naming, file placement, or "is this OK?" on decisions with an obvious default. That's not a fork — decide and move.
- **Under-asking.** Silently picking one path on a real product/architecture fork because asking felt slower. If the choice needs the owner's intent, ask.
- **Writing the plan to a local `.md` and handing it over.** Specs live in Linear. The local copy is scratch, never the deliverable.
- **Building in the live tree or on `main`.** Always an isolated worktree + feature branch.
- **Re-running PRlaunch's gates inside execute.** Build + test here; the three quality gates belong to `/PRlaunch`. Don't duplicate.
- **Hopping repos without permission.** If the fix needs another repo, name it and stop (rule #5).
