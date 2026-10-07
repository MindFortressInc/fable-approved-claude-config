---
name: epicbuilder
description: Use when the owner names project(s) whose loose Todo/Backlog tickets need structuring into epics before work can be assigned or orchestrated — "/epicbuilder <fuzzy project(s)>", "group the backlog into epics", "build epics for X", "chunk the backlog", "get the 1-offs under bulldozer". Feeds /orchestrate (which runs this first when a project's backlog is unstructured), /assign, and /bulldozer. A separate cross-board hygiene pass owns cross-board tidying (stray sweeps, oversized-epic splitting) — this skill never moves tickets between boards; /orchestrate to actually execute an epic.
---

# epicbuilder — loose backlog → session epics + bulldozer intake

A project whose Todo/Backlog is a flat pile can't be orchestrated, assigned, or bulldozed — those consumers all eat **epics**. This skill structures the pile: group related loose tickets into **session epics** (one orchestratable session, ~3–8 tickets targeting ~5, blocked-by chains when real ordering exists), and park the true 1-offs under the project's standing **Bulldozer 1-offs** epic so `/bulldozer` has a queue. It is `/orchestrate`'s front half: orchestrate picks FROM epics; epicbuilder makes them exist.

**Lane boundaries (defer, don't duplicate):** within-project structuring ONLY. Cross-board strays → flag for a separate cross-board hygiene pass (never move them here). Oversized existing epics → that same hygiene pass. Choosing which epic to run → `/orchestrate`. Worker prompts → `/briefs`. Writes follow single-writer discipline: one writer applies the announced map, nothing writes ahead of it.

## Entry point: `/epicbuilder <fuzzy project name(s)>`

One or more project names, space/comma separated, each fuzzy-resolved via `mcp__linear__list_projects` (a URL wins outright; two plausible matches for one token → the single allowed immediate ask). Run the pipeline per project — epics never span projects.

## Pipeline (per project)

### 1. Inventory the loose tickets
In scope: open Todo/Backlog tickets that are **unparented**, or whose parent is Done/Canceled (orphans). Out of scope: anything In Progress/In Review/Deployed (in-flight lanes), tickets already under an open epic, and epics themselves. Bulk inventory via raw GraphQL or cursor-paged `list_issues` — never one giant `list_issues` call (token cap).

### 2. Classify — read-only opus-worker fleet, orchestrator judges
≲40 loose tickets → one classifier; more → chunk ~120/ticket-chunk, parallel read-only classifiers (`Agent`, `subagent_type: "opus-worker"`) with six-section briefs (`/briefs`), each given title/labels/parent/300-char snippet **plus a whole-inventory `title_index.json`** for cross-chunk parent lookups. One bucket per ticket:

| Bucket | Test | Action at apply |
|---|---|---|
| `group:<concern>` | shares a shippable concern with ≥2 other loose tickets | member of a proposed session epic |
| `bulldozer` | small, self-contained, no cross-ticket ordering, machine-clearable | parent under the project's **Bulldozer 1-offs** epic |
| `existing-epic:<id>` | an OPEN epic on this project already covers it (incl. feature follow-ups → their feature's epic) | parent into that epic |
| `stray` | belongs on a different project/board | flag for the cross-board hygiene pass — do NOT move |
| `unclear` | none of the above confidently | leave untouched, list in report |

Classifiers propose; they never write.

### 3. Propose the epic map
- Group-bucket tickets → session epics of **~3–8 members (target ~5)**; chain epics via blocked-by when one genuinely gates another; note same-file/stacked expectations for the future builder.
- **Existing-epic-first** (rule #6 at project level): before minting an epic, sweep the project's open Epics-state tickets — a concern that's already an epic gets tickets parented INTO it, not a duplicate shell.
- **Bulldozer 1-offs epic**: locate by exact title "Bulldozer 1-offs" on this project; if absent, CREATE it — that exact title, state Epics, never closes, description covering what lands here, why it's an epic instead of loose tickets, and the rules of the road for what qualifies. 1-off tickets file under it **unassigned** — it's `/bulldozer`'s queue, not for humans.
- Announce the full map (epics + members + chains + bulldozer adds + strays/unclears) with a one-line rationale each, then GO. Ask only a true fork (two groupings with materially different product consequences you can't derive) — batched, once.

### 4. Apply — single writer
The orchestrator alone writes, from the announced map only: create epic shells (state **Epics**, priority set, description = the group's concern + member rationale + a breadcrumb naming this epicbuilder run), then re-parent members (`save_issue` parentId; batched aliased GraphQL `issueUpdate`, 20/request, for bulk). Ticket-level blocked-by relations survive re-parenting untouched — tracker relations are independent of parentId; don't recreate or drop them. Never delegate writes to the fleet.

### 5. Verify counts
Re-pull each touched epic and **assert children-count == map-count**. Any mismatch is stop-the-line: diagnose before reporting.

### 6. Report + persist
Per-project table: `epic → members / chain / bulldozer adds / strays flagged / unclear left`. Name the handoff explicitly — each epic is now one `/orchestrate` group, one `/assign` package, or (1-offs) bulldozer food. Which of orchestrate/assign a given epic goes to is the owner's call at consumption time — epicbuilder's job ends at the map. If you maintain a session-memory store, `memory_write` the run (epics created, counts, notable calls) so the next run starts from an accurate picture.

## Gotchas (keep verbatim — each fixes a real past mistake)

- `list_issues` with a generous limit blows the token cap — page with cursor, or use raw GraphQL for bulk inventory.
- `list_issue_labels` with no team SILENTLY OMITS team-scoped labels — always pass the team explicitly (`list_issue_labels(team:"<team-key>")`).
- Some tracker project names contain a double space — exact name-eq filters miss them; fuzzy-match, don't string-match exactly.
- Linear issue creation needs a priority (1–4) at filing time — scripted epic creation should set it explicitly rather than leaving an ambiguous default.
- Malformed GraphQL (missing closing brace) makes Linear return HTTP 500, not 400 — brace-count before blaming the API.

## Common mistakes

- **Minting a duplicate epic** when an open one already covers the concern — sweep existing Epics-state tickets first; parent INTO, don't shadow.
- **Moving strays to their right project.** That's the cross-board hygiene pass's job with its own coherence checks — here you only flag.
- **Feature follow-ups into Bulldozer 1-offs.** They ride their feature's epic; the bulldozer queue is for orderless self-contained 1-offs only.
- **Assigning the 1-offs.** The Bulldozer 1-offs epic files unassigned — it's a machine queue.
- **Letting classifiers write, or writing beyond the announced map.** Propose → announce → single-writer apply → verify.
- **Mega-epics.** If a group wants 10+ tickets it's never one balloon epic: still fits ~2 sessions (≲16 tickets) → split into two chained session epics; genuinely project-scale beyond that → flag for the hygiene pass's promote stage.
