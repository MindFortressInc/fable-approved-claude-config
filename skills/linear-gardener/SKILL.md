---
name: linear-gardener
description: Use when a tracker project/board has accumulated stray tickets, an unweeded label cohort, or oversized epics and needs a standing board-hygiene pass — not a one-off manual reorg. Triggers "/linear-gardener <project>", "garden the board", "run a sweep on X", "reorg <project>'s epics into projects", "sweep the strays back onto their real boards". Config-driven 8-stage pipeline: inventory+histogram → promote oversized epics to projects → re-chunk to session epics → classifier-fleet stray sweep → single-writer apply → verify counts → house conventions → persist a session record.
---

# linear-gardener — standing Linear board-hygiene pipeline

Boards drift: epics balloon past what one session can orchestrate, tickets get filed under the wrong parent, labels sprawl past what the board's overview page describes. This skill is a **standing, repeatable** pipeline for that drift — every run (a new sweep, a different board, a different workspace) is a fresh invocation over new config, never a new skill. The pipeline is read-heavy and classifier-assisted, but writes are deliberately narrow: **one writer, batched, verified, reversible.**

**Everything workspace-specific — team, target project(s), cohort labels, thresholds, initiative map, board-convention toggles, batch size — lives in `gardener.config.json`, never in this file.** Copy `gardener.config.example.json` (next to this file) to `gardener.config.json` and edit; it's the only thing that changes between workspaces or runs.

## Entry point: `/linear-gardener <target> [--config path] [--cohort-labels ...] [--promote-threshold N] [--initiative-map ...]`

`<target>` is fuzzy — resolve via `mcp__linear__list_projects` / `mcp__linear__list_teams` (or your tracker's equivalent list-projects/list-teams call), falling back to `gardener.config.json`'s `target.default_project` if set. No target and no default → ask which project/board. If cohort labels aren't given on the CLI or in config, ask what the reorg's scope actually is — running the full pipeline against an entire team unscoped is how you get a several-hundred-ticket classifier run by accident.

## Config (`gardener.config.json`)

Copy `gardener.config.example.json` → `gardener.config.json` (same directory) and fill in your workspace. Treat your real config the way `skills/assign/roster.json` is treated: keep it out of version control if it names real projects/teams you don't want public.

| Config key | What it is | Default |
|---|---|---|
| `team.key` / `team.id` | Tracker team key or id the target project(s) belong to. | required |
| `target.default_project` | Fuzzy project/board name to garden when `<target>` isn't passed on the CLI. | none — require `<target>` |
| `cohort_labels` | The label set defining which tickets are in scope for this pass. Narrows inventory so you classify the cohort, not the whole board. | required |
| `promote_threshold` | Open-children count above which an epic is really a PROJECT, not a session chunk (see Work Taxonomy below). | `5` |
| `session_epic_size` | Target ticket count per re-chunked session epic. | `5` |
| `classifier.chunk_size` | Tickets per read-only classifier chunk in stage 4. | `120` |
| `classifier.model` | Model override for the read-only classifier workers in stage 4 (passed as the `Agent` tool's `model:` param; the worker is always `subagent_type: "opus-worker"`, so `null` keeps that agent's own model). | `null` |
| `initiative_map` | Destination project → initiative name, for stage 7's initiative wiring. Omit an entry to skip wiring for that project. | `{}` |
| `board_conventions.rewrite_summary_page` | Whether stage 7 rewrites each destination board's summary/description to match its new contents. | `true` |
| `board_conventions.overview_pointers` | Whether stage 7 adds the 3-bullets + table + run-note overview convention. | `true` |
| `apply.batch_size` | Mutations per batched `issueUpdate` request in stage 5. | `20` |
| `auth.api_key_env` / `auth.api_key_file_env` | Env var **names** holding the tracker API key (see Auth below) — never a key or a path to one. | `LINEAR_API_KEY` / `LINEAR_KEY_FILE` |

## Work Taxonomy

**PROJECT** = a program/board-scale body of work. **EPIC** = one orchestratable session, roughly `session_epic_size` tickets, chained via blocked-by when there's real ordering. **ticket** ≈ one PR. Stage 2 promotes mis-typed epics (too big) up to projects; stage 3 re-chunks mis-typed epics down to session epics. Every ticket this pipeline touches ends up parented under something that matches its actual scale. (If your workspace already has its own written taxonomy doc, defer to it — this is the fallback definition the pipeline uses when none exists.)

## The 8-stage pipeline

Track all 8 stages with TodoWrite (or your equivalent task list). Stages 1-3 are sizing (deterministic), stage 4 classifies (LLM judgment, read-only), stages 5-6 are the single controlled write + its proof, stage 7 is board polish, stage 8 is the durable record.

### 1. Inventory + histogram — deterministic pre-pass, no LLM judgment
Pull the **full** issue dump of the target scope via raw GraphQL — **all states, including Done/Canceled** (a ticket's history moves WITH its bucket; you cannot reorg the open tickets and strand their closed siblings under the old parent). Then build a label/epic histogram (count of open children per epic, count per label) to size the actual problem before touching anything. This stage never makes a judgment call — it's counting, not classifying.

### 2. Promote oversized epics to projects
Any epic whose open-children count exceeds `promote_threshold` is really a PROJECT wearing an epic's clothes. Propose the promotion: a new project shell, the epic's children re-parented under it, the epic itself closed/converted. This is a proposal to review before stage 5 applies it, not an automatic action.

### 3. Re-chunk to session epics
Whatever's left oversized after stage 2 (still too big to be one epic, but not big enough to be its own project) gets split into `session_epic_size`-ticket session epics chained via blocked-by. Chain them so the sequence is explicit; don't leave the split tickets as siblings with no ordering when real ordering exists.

### 4. Stray sweep via classifier fleet — read-only, purpose-over-layer
Chunk the inventory into `classifier.chunk_size`-ticket chunks. Spawn parallel READ-ONLY classifiers (`Agent`, `subagent_type: "opus-worker"`), one per chunk, briefed with the six-section brief contract from the `briefs` skill (CONTEXT/TASK/CONSTRAINTS/RETURN CONTRACT/VERIFICATION REQUIREMENT/STOP CONDITIONS — see `references/classifier-brief-template.md` for a filled-in template). Give each classifier: per-ticket title, labels, parent, a short body snippet, plus a `title_index.json` covering the WHOLE inventory (not just its chunk) so it can resolve cross-chunk parent lookups when a ticket's parent lives in a different chunk. Buckets are decided **purpose-over-layer** — what the ticket is FOR, not which repo/layer/label surface it happens to touch.

Merge all chunk verdicts, then run the parent/child-mismatch check: **tree-coherence requires a child's destination bucket to equal its parent's destination bucket.** Whole trees move together — a child classified into a different bucket than its parent's verdict is a conflict to resolve before stage 5, never something stage 5 applies as-is.

### 5. Single-writer apply — the ONLY stage that writes
One writer — the orchestrator itself — executes every move from the merged, tree-coherent plan. All writes are batched, aliased GraphQL `issueUpdate` mutations, `apply.batch_size` per request (default 20 — the proven safe batch size). Never hand writes to the classifier fleet or to any other agent; classifiers propose, the orchestrator alone applies. See `references/apply-pattern.md` for the worked mutation pattern.

### 6. Verify counts
Re-pull the destinations and ASSERT `moved-count == plan-count` per destination. Any mismatch is a stop-the-line error — do not proceed to stage 7 with an unreconciled count. Diagnose the gap (partial batch failure, a ticket that resolved to two different buckets, a retry that silently no-op'd) before moving on.

### 7. House conventions
Every destination board that received tickets this run gets brought up to house standard (toggle each via `board_conventions.*`):
- **Summary page/description scope rewrite** — the board's description reflects what it now actually contains (`board_conventions.rewrite_summary_page`).
- **Initiative wiring** — using `initiative_map`, wire the project to its initiative.
- **Overview pointers** (`board_conventions.overview_pointers`) — 3 bullets + table pointers + a run note (which run moved tickets in, so provenance is traceable from the board itself).
- **Matrix-integrity check**: REVERSE-APPLY the move manifest against the new state and confirm it reproduces the original state exactly (**reverse-apply-equals-original**). This is the pipeline's own regression test — if replaying the inverse of every move doesn't get you back to the stage-1 inventory, something in the manifest or the apply was wrong, and you stop-the-line the same as a stage-6 count mismatch.

### 8. Persist a session record
Write a durable record of this run's outcome — via your persistent-memory MCP if you have one (e.g. a `memory_write` tool), else a comment on the target project or a note in your own tracking system: boards created/promoted, counts moved per destination, and the notable decisions (tree-coherence conflicts resolved, threshold overrides). This is what makes the NEXT run start from an accurate picture instead of re-deriving history from scratch. Skip if you have no persistence mechanism — the pipeline still completes without it.

## Quick reference

| Stage | Action | Mechanism |
|---|---|---|
| 1 Inventory + histogram | Full dump (all states) + label/epic histogram | raw GraphQL, deterministic |
| 2 Promote | Oversized epic (open-children > `promote_threshold`) → project shell | proposal, reviewed before apply |
| 3 Re-chunk | Oversized remainder → `session_epic_size`-ticket session epics | blocked-by chains |
| 4 Classify | Chunked (`classifier.chunk_size`/chunk) read-only classifiers, purpose-over-layer | `briefs` six-section prompts + `title_index.json` |
| 4b Coherence check | Child destination must equal parent destination | tree-coherence merge pass |
| 5 Apply | ONE writer executes all moves | batched aliased `issueUpdate`, `apply.batch_size`/request |
| 6 Verify | moved-count == plan-count per destination | re-pull + assert; mismatch = stop-the-line |
| 7 House conventions | Scope rewrite + initiative wiring + overview pointers (toggled) | + reverse-apply-equals-original check |
| 8 Persist | Durable run-outcome record | persistent-memory MCP, or your own tracker |

## Auth

Stages 1, 2, 5, and 7's raw-GraphQL calls need a tracker API key. Resolve it the same way the rest of this config resolves it: `$<auth.api_key_env>` (default `LINEAR_API_KEY`), or a JSON file named by `$<auth.api_key_file_env>` (default `LINEAR_KEY_FILE`) holding `.env.LINEAR_API_KEY`. Never hardcode a key, or a path to one, in this file or in config — only the env var *names* are configured, not a value.

## Gotchas (battle-tested)

- **GraphQL 500s are usually a brace-counting bug, not the API being down:** malformed GraphQL (a missing closing brace) makes the API return HTTP 500 (not 400) — looks like a transient server error and survives retries; brace-count before blaming the API.
- **Workflow-state creation has no MCP equivalent:** creating workflow states needs raw GraphQL — there's no `workflowStateCreate` (or equivalent) MCP tool. Use the API key resolved per Auth above; state descriptions are commonly length-capped (Linear's limit is 255 chars) — check your tracker's limit before writing a longer one.
- **Apply batch size is fixed for a reason:** batched aliased `issueUpdate`, `apply.batch_size` (default 20) mutations per request is the proven safe batch size — pushing it higher is how you get partial-batch failures.
- **Inventory pagination:** a bulk list call with a generous limit blows the token cap — page with a cursor, or use raw GraphQL for bulk inventory (same as stage 1).

## Common mistakes

- **Skipping stage 1's Done/Canceled tickets.** History moves WITH its bucket — reorganizing only the open tickets strands their closed siblings under the old parent, and the next histogram will look wrong.
- **Classifying by layer instead of purpose.** A ticket's repo/label surface isn't its home; what it's FOR is. Purpose-over-layer is the whole point of the classifier brief.
- **Letting the classifier fleet write.** Classifiers propose; only the single orchestrator writer applies (stage 5). Handing writes to the fleet is a split-write hazard.
- **Applying a plan with unresolved tree-coherence conflicts.** A child whose verdict disagrees with its parent's is a conflict to resolve, not noise to average out — moving part of a tree and leaving the rest behind breaks the board.
- **Proceeding past a stage-6 count mismatch.** Any `moved-count != plan-count` is stop-the-line, not a rounding error — diagnose before touching stage 7.
- **Shipping house conventions without the reverse-apply check.** If replaying the inverse manifest doesn't reproduce the original stage-1 inventory, the manifest (or the apply) has a bug you haven't found yet.
- **Running the pipeline unscoped.** No cohort labels means classifying the entire board/team — always scope stage 1's inventory to the cohort actually being reorganized.
