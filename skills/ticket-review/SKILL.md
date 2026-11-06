---
name: ticket-review
description: Use when reviewing existing tracker tickets or a newly created epic against code, architecture and goals — explicit ticket lists, epic-spec reviews, top Urgent/High batches — and commenting on material disagreements. Runs on the session's own agents; an optional, off-by-default Codex mode adds a second model family. Use deep-review for a PR diff.
---

# Ticket review

Run the review queue with fresh read-only agents, evidence-backed verdicts, and one writer for tracker comments. Review the ticket's premise and proposed solution, not just whether work remains.

## Invocation

`/ticket-review [scope | epic <ID> | tickets <ID>,<ID>] [next | recheck <ID> | new-sweep] [--via native|codex] [--limit N] [--report-only] [--drain]`

- A project/queue scope reviews 20 tickets, Urgent then High. Resolve a fuzzy project name to the tracker's project before selecting. With no scope, use `TICKET_REVIEW_DEFAULT_SCOPE` if it is set; otherwise ask which project to review. Never fall back to the whole workspace.
- `epic <ID>` resolves that epic's open descendants and carries the epic goal. `tickets <ID>,<ID>` reviews exactly those IDs. Explicit lists and epics bypass the queue's priority, coverage and default-20 filters: review every supplied ticket unless the user explicitly sets a limit. Do not substitute other tickets for missing children or inaccessible IDs.
- `--via native` (the default) uses the controlling runtime's own agents. `--via codex` makes Claude orchestrate an independent read-only Codex CLI review — see [Codex mode](#codex-mode-optional-off-by-default).
- `next` resumes coverage for the same scope. `new-sweep` starts fresh coverage while keeping history. `recheck` revisits named tickets.
- Normal output includes comments on material disagreements. `--report-only` suppresses every external write.
- `--drain` continues bounded batches through the invocation's eligible snapshot. Otherwise stop after the requested batch. Create a recurring task only when the user asks for recurrence.

## 1. Select and checkpoint

Use the tracker's existing connector or authenticated API path; do not build another credential or transport helper. Page the entire scoped inventory before ranking. Keep large payloads in files and inspect concise summaries.

For queue selection, outstanding means not completed, canceled or archived. Exclude epic containers, but keep blocked, assigned, In Progress, In Review and Deployed tickets: they can need review too. An explicitly named ticket stays in scope even if closed; report its current status. For an epic, traverse nested containers, review the open leaf tickets and assess the epic's overall coherence. Record the exact target IDs before dispatch.

For queue selection, sort by priority (Urgent, then High), the tracker's manual sort order, creation time, identifier. Preserve the supplied order for explicit ticket lists; freeze a stable descendant order for epics. Do not widen scope through an umbrella label. Report the selection rule and priority counts; a full Urgent batch does not mean any High tickets were reviewed.

Keep durable records under `${TICKET_REVIEW_HOME:-$HOME/.claude/ticket-reviews}/<scope>/`, outside any repository. Read the coverage ledger before selecting, and reconcile pending comment receipts before new writes (report-only still forbids writes). Record every terminal verdict, including agreement with no comment; otherwise `next` repeats the same tickets. Follow [the record contract](references/records.md). Save each result before advancing. A failed worker has no verdict: record the error, continue with independent tickets, and keep it for retry. Count uncertainty separately from completed, evidence-backed reviews.

## 2. Ground and delegate

Fetch each ticket's full description, parent, relations, existing comments and linked PRs. Before substantial reconstruction, run the discovery pass for the concern: the tracker (including completed and superseding work), relevant source across the codebase, and the full memory store if one is configured. Establish the intended end state: a consolidation or deletion decision can supersede current code conventions.

Fetch and pin the relevant remote SHAs. Read the repo's instructions and invariants, and inspect source with `git show <sha>:<path>` rather than trusting another session's checkout. Establish production state separately where it affects the verdict: merged code, a checkout SHA and a code default do not prove what is served, which image or worker is rolled out, which migrations ran, or which flags are live.

Use parallel read-only agents on small groups that share a concern or repo, with fresh contexts as the queue advances. Brief each one with the **briefs** six-section contract and [the reviewer contract](references/reviewer-contract.md): ownership, full ticket context, pinned sources and any known live evidence. Agents inherit the session model unless the user asks for another. If agents are unavailable, review sequentially and say so; never use that fallback to replace an explicitly requested Codex reviewer.

In a pre-build review of a just-authored epic, proposed functionality being absent from current code is expected: assess reuse, feasibility, architecture, sequencing and acceptance rather than treating every unimplemented requirement as a defect.

## 3. Decide, verify and comment

For each ticket assess premise, alignment with the architecture and goal, the proposed solution, dependencies, current implementation and remaining acceptance. Use `agree`, `revise`, `superseded` or `uncertain`. Preserve valid remaining work: a completed migration does not prove a live user flow, and a partial rollout does not invalidate its goal.

Treat agent output as candidates. Before publishing, independently open every cited source and related decision. Require literal source excerpts, exact lines and pins, and executed check output; a paraphrased evidence field is not enough. Try to refute each disagreement through upstream guards, sibling paths, later decisions and prior comments. Recheck current vendor claims against official sources when they matter.

The orchestrator alone posts. Re-fetch the ticket and its comments before each write; skip resolved objections and duplicate comments. Comment only on material disagreements: an obsolete premise, a wrong boundary or solution, unsafe sequencing, duplicate scope, or acceptance that cannot prove the goal. State the disagreement, link the evidence, and give the concrete correction. Agreement, and missing runtime access on its own, do not warrant a comment.

Review comments and local records are the only default side effects. Do not implement fixes, edit ticket descriptions, states, priorities or assignments, file new issues, merge, deploy, run migrations, flip flags, run paid generations or other production jobs, or message people as part of this review. Route any further action through an explicit user instruction. Check production read-only, without exposing secrets. After two dead-end searches for one fact, switch to its actual surface or record uncertainty.

Re-read every posted comment from the tracker and compare its body and id with the intended write. On an ambiguous write failure, inspect the existing comments before retrying. Report partial publication honestly.

## 4. Report and resume

Return the full ordered list with links and a concise verdict per ticket, totals reviewed / uncertain / commented, the most consequential conclusions and any runtime limits. Link commented entries to their comments. Record rejected disagreements internally rather than publishing them. If a memory store is configured and the run is not `--report-only`, persist a concise memory with the durable conclusions and evidence links; a report-only run keeps them in its local run record instead. Update the coverage ledger, so `next` advances without erasing history or repeating agreement-only reviews.

## Codex mode (optional, off by default)

`--via codex` sends the review to a second model family through the Codex CLI, read-only, while Claude stays the orchestrator and the only writer. It runs only when **both** the user asks for it **and** `CODEX_REVIEW_ENABLED=1` is set — the same switch as deep-review's Codex lane. With the flag unset, say Codex mode is disabled and stop; do not silently substitute native agents for a requested Codex review. The model is a config value: `CODEX_REVIEW_MODEL` selects it, and unset means the Codex account default (no `-m` flag). The handoff — frozen scope, per-lane briefs, dispatch and result checks — is in [the Codex handoff](references/codex-handoff.md).
