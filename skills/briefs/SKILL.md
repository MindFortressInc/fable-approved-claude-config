---
name: briefs
description: Use when writing ANY subagent/worker prompt from an orchestrator skill (bulldozer ticket workers, deep-review finder/refuter agents, babysit fixers, assign discovery agents, skillify authors) — the six-section brief contract every worker prompt must contain.
---

# briefs — the worker-brief contract for subagent prompts

**Worker quality is downstream of brief quality.** A fresh subagent knows nothing but its prompt: no session history, no working-tree state, no sense of what "the checkout" means. An ad-hoc brief varies run-to-run — one worker gets the branch, the next assumes `main`; one gets a return schema, the next free-writes prose the orchestrator can't parse. This skill fixes the shape: **every worker prompt an orchestrator emits carries the same six sections, in order, filled.** The structure converts judgment a strong model applies implicitly into fill-in slots a weaker worker can't silently skip.

Use this any time an orchestrator skill spawns a worker: `bulldozer` ticket workers, `deep-review` finder/refuter agents, `babysit-prs` fixers, `assign` discovery agents, `skillify` authors — or any new orchestrator that fans out.

## The six required sections

Every brief MUST contain all six, each labeled, in this order. A section that "doesn't apply" is a smell — say why explicitly rather than dropping it.

### 1. CONTEXT
State the repo, the **exact branch or commit ref the worker must trust** — never "the checkout", never "the current tree" (the worker has no shared filesystem view and the tree may be mid-edit by someone else). Give 2–3 sentences of situation: what's already been done, why this worker exists, what surrounds the task. When the worker must NOT trust working-tree state — because another agent owns those files, or the ref is a moving target — inline the actual file contents or a diff into the brief so the worker reads from the prompt, not from disk.

### 2. TASK
One outcome, stated as **the artifact to produce** — "return a JSON verdict on whether EX-3803 still reproduces", "open a PR fixing the null-guard in `api/x.py`". Not a topic ("look into the bug"), not a bundle of three things. If you're tempted to write "and also", it's two briefs.

### 3. CONSTRAINTS
The scope fences that keep the worker in its lane: never merge, never reassign a ticket, read-only, path allowlist, don't touch files another agent owns. Then paste the relevant **gotchas verbatim** — the zsh word-split trap, the "list_issues blows the token cap" warning, whatever bit the last run. Verbatim, because a paraphrased gotcha is a gotcha the worker will re-learn the hard way.

Include the discovery obligation **written out**, never as a pointer to a rule the worker has not read — a fresh subagent sees only this prompt, and a Codex delegate reads `AGENTS.md`, never `CLAUDE.md`, so "follow rule #6" arrives as a dead reference. The operative text to paste: *when you are about to spend real work establishing something that may already exist or have already been measured — proposing a new mechanism, concluding "X doesn't exist", or measuring/benchmarking a system that is already running — first run all three of: (1) search the tracker for the* concern *(product, SKU or epic names — not the ticket id); the tracker records what is already built, not just what is planned. (2) grep the ecosystem for the concern. (3)* `memory_search` *the FULL memory: an injected memory block is usually a truncated top-ranked slice, and a* `[+N more]` *marker means the rest was cut. Build or derive from scratch only after all three come back empty.* A question one file read settles does not trigger this. If a retrieval tool ranks hits, inspect several and verify each cited `file:line` against the source; the top-ranked hit is not ground truth.

### 4. RETURN CONTRACT
The exact schema/format of the reply, **with a filled example**. Prefer JSON with named keys — the orchestrator parses it, so ambiguity here becomes a parse failure or a wrong write. Say what each key means and what an empty/failure value looks like. If the reply is prose, give the exact headings expected.

When persistence applies, specify where its identifier or failure belongs within the declared return schema (for example, an existing `notes` or `evidence` field); workers must not invent undeclared keys.

When the unit learns a meaningful, reusable factual gotcha, have it persist the finding with `memory_write(kind="fact")` before returning and include the returned memory identifier in its report. If the finding proposes a policy/rule change, return the proposal and evidence for operator review without writing it. Claims labeled `verified`, `confirmed`, or `validated` require evidence; carry the actual observation and supporting source or command output. For a read-only worker, return the finding and evidence for the orchestrator to persist instead. Both of those hand-offs need somewhere to land, so the declared schema must carry a `finding` field and an `evidence` field alongside the identifier — see the worked example's `notes` block. If persistence fails or the tool is unavailable, report that explicitly, with the finding and evidence still populated so the orchestrator can finish it; do not claim it was saved. No new learning means no required write.

### 5. VERIFICATION REQUIREMENT
What the worker must **RUN** before claiming success — the test command, the build, the `git show` that proves the change landed, the reproduction that now passes. Evidence before assertion (global rule #1). Make explicit: **if the outcome can't be verified, the worker reports failure honestly** rather than asserting success it didn't observe.

For a worker that runs in parallel with siblings, the test gate is the **targeted blast radius** — the new tests failing on base and passing on the branch, plus the grepped affected tests and the repo's ratchet tests — at **at most 2 xdist workers**. Never write "run the full suite" or `-n auto` into a parallel worker's requirement: the full suite is CI's job, and every fleet shares one machine (`hooks/test-admission.py` denies both).

### 6. STOP CONDITIONS
When to bail instead of thrashing, and **what to return when it bails** so the orchestrator can act. Mirror the global 3-strikes / two-dead-ends rule: after ~2 failed attempts at the same fact or fix, stop and report the blocker with what was tried — don't burn the budget grinding. Name the specific dead-ends for this task (premise contradicted, ref missing, tests already red on checkout) and the shape of the "I stopped" return.

## Worked example — a bulldozer-style ticket worker

```
CONTEXT: Repo is acme-api (a fictional repo for this example). Trust ONLY
origin/main at commit a1b2c3d (fetched this session) — do NOT read the live
working tree; another worker is editing it. The ticket EX-0000 claims
`send_reminder()` in api/notify.py double-sends when a user has two active
bookings. You are one isolated worker; the orchestrator is draining a queue
and will not see your context, only your returned JSON.

TASK: Produce an opened PR that makes `send_reminder()` send exactly one
reminder per user per booking-window, with a regression test that fails on
a1b2c3d and passes on your fix.

CONSTRAINTS:
- Work in an isolated worktree on branch me/ex-0000-dedupe-reminders. Never
  commit to main, never merge, never touch the ticket's Linear state yourself.
- Path allowlist: api/notify.py and tests/test_notify.py only.
- Gotcha (verbatim): pytest here needs `ENV=test` or it hits prod Postgres —
  `ENV=test uv run pytest tests/test_notify.py`.

RETURN CONTRACT: reply with exactly this JSON, no prose around it:
{
  "ticket": "EX-2991",
  "status": "pr_opened" | "blocked",
  "pr_url": "<url or null>",
  "test_cmd": "<exact command you ran>",
  "test_result": "<pass/fail summary you observed>",
  "notes": {
    "blocker": "<one line describing the blocker, or null if status=pr_opened>",
    "finding": "<the reusable fact to persist, or null if nothing was learned>",
    "evidence": "<the source excerpt or command output backing `finding`, or null>",
    "memory_id": "<memory_write identifier when persistence applies, or null>",
    "persistence_error": "<one line if memory_write failed or was unavailable, or null>"
  }
}

VERIFICATION REQUIREMENT: before returning status "pr_opened" you MUST run the
regression test and observe it fail on a1b2c3d, then pass on your branch. Paste
the observed pass line into test_result. If you cannot make it fail-then-pass,
return status "blocked" — do not claim a fix you didn't watch work.

STOP CONDITIONS: if the double-send does NOT reproduce on a1b2c3d after 2
honest attempts (the premise is wrong), or the allowlisted files don't contain
`send_reminder`, stop and return status "blocked" with notes naming what you
found instead. Do not widen the path allowlist to go hunting.
```

## The classifier-fleet pipeline

Some orchestrators don't fan out to *build* — they fan out to *classify/validate at scale*, reconciling a large set of claims against ground truth. This shape recurs enough to name: **deterministic pre-pass → read-only classifier/validator fleet → single-writer applies → verify counts → memory_write.**

1. **Deterministic pre-pass.** Establish ground truth once, cheaply, with plain commands — not agents. Pull the decisive facts up front (e.g. each repo's `git log`, which commits are ancestors of a live box's HEAD) and inject the result as a routing hint into each worker's brief. Agents shouldn't spend budget re-deriving what a `git log`/`grep` already answered.
2. **Read-only classifier/validator fleet.** One six-section brief per item, fanned out in parallel. Each worker classifies its item and returns a structured verdict — it proposes, it never writes. Read-only is what makes wide fan-out safe: a bad classification is a wrong string, not a wrong write.
3. **Single-writer applies.** One writer turns verdicts into writes — either the orchestrator itself, or one narrow, mechanical apply-brief per item. Route any write that can fork a record (one verdict becoming two tickets, a split) through the orchestrator by hand, never an agent: independent workers racing on the same write path is exactly how a split mis-applies to the wrong record.
4. **Verify counts.** After applying, re-pull the target collection and assert the count matches the expected kept-set. Don't trust a worker's self-reported "what I did" field — assert the actual post-state, because a worker can silently do more than it was told and misreport it.
5. **memory_write.** Close the sweep by persisting what was found/decided so the next run doesn't re-derive it from scratch (skip this step if you don't run a persistent-memory tool).

**`flushdeployed` is the existing end-to-end exemplar of stages 1–4**: its per-repo `git log`, pre-computed live-vs-pending recon IS the deterministic pre-pass; `validate-workflow.js`'s one-agent-per-ticket, read-only (`apply=false`) verdicts ARE the classifier fleet; the orchestrator hand-doing every `PARTIAL` split plus the narrow `apply-done-workflow.js` for the mechanical Done/note writes ARE the single-writer stage; and its "re-pull the Deployed column, assert `count == expected-kept-set`" step IS verify-counts.

### Worked example — a read-only classifier/validator brief

```
CONTEXT: Repo is acme-services (fictionalized values below). Trust ONLY the
prod ground truth given here — do not re-derive it: origin/main HEAD is
a1b2c3d; the live EC2 box is at e4f5a6b (`git merge-base --is-ancestor` a
commit against e4f5a6b tells you if it's actually live). You are one of N
parallel read-only workers; a single orchestrator applies all verdicts after
every worker returns — you never write the tracker yourself.

TASK: Return a structured verdict on whether ticket EX-3217 ("Fix
double-charge on renewal") is genuinely live in prod, not just merged.

CONSTRAINTS:
- Read-only: propose an action, do not call any tracker write tool.
- Don't trust a merge commit alone — confirm the actual fixed code is present
  in origin/main (`git show origin/main:<path>`), not just a commit subject
  match; catches reverts and no-op merges.
- Gotcha (verbatim): a failed merge-search alone is NOT enough to call
  WRONG_NOT_DEPLOYED — when unsure between WRONG and UNCERTAIN, pick
  UNCERTAIN.

RETURN CONTRACT: reply with exactly this JSON:
{
  "id": "EX-3217",
  "verdict": "DEPLOYED_LIVE" | "MERGED_PENDING_DEPLOY" | "PARTIAL" | "WRONG_NOT_DEPLOYED" | "UNCERTAIN",
  "evidence": "<commit hash + file:line proving present/absent>",
  "proposed_action": "move_to_done" | "note_keep_deployed" | "split_then_done" | "note_move_to_todo" | "note_only_manual",
  "confidence": "high" | "medium" | "low",
  "memory_handoff": "<reusable fact + evidence for the orchestrator to persist via memory_write, or null if nothing new was learned>"
}

VERIFICATION REQUIREMENT: before returning a verdict other than UNCERTAIN, you
MUST have actually run the merge-search and the ancestor check this session
and quote their output in "evidence" — a verdict with no command behind it is
not a verdict.

STOP CONDITIONS: if you can't find the merge AND can't positively confirm the
fix is absent, return UNCERTAIN with confidence "low" and evidence naming what
you checked — do not guess WRONG_NOT_DEPLOYED off a failed search alone.
```

## Adopters

These orchestrator skills each spawn workers and must reference this template when they build a worker prompt: **bulldozer** (per-ticket workers), **deep-review** (finder/refuter agents), **babysit-prs** (CodeRabbit-fix workers), **assign** (discovery agents), **skillify** (skill-author workers), **flushdeployed** (deployed-ticket validator/apply fleet — the end-to-end exemplar of the classifier-fleet pipeline above). Their edits to point at this contract land separately — this file is the canonical source they cite, not a change to those skills.
