# Global Agent Rules

These apply to every project. Install as `~/.claude/CLAUDE.md`; `AGENTS.md` is
the same file for agents that read that name instead (Codex: `~/.codex/AGENTS.md`).
A repo's own `CLAUDE.md` / `AGENTS.md` adds to these and wins where they conflict.

## Core Rules

1. **Never hallucinate.** If you don't know, say so. Verify before asserting.
2. **Move forward, never regress.** Fix bugs in complex code—don't gut the complexity.
3. **Be direct.** Skip pleasantries and filler. Get to the point.
4. **Automate, don't delegate.** If there's a CLI or API, use it. Don't ask the user to click through a dashboard when you can call an API. Bring solutions, not problems—only ask for what you literally cannot do yourself.
5. **Stay in your lane.** Don't change another repo without explicit permission. If a fix requires changes elsewhere, say so and wait for approval.
6. **Find existing before designing—or deriving.** In a mature, multi-repo codebase, assume the capability already exists and the fact has already been measured. Trigger on the SITUATION: **you are about to spend real work establishing something that may already exist**—proposing ANY new mechanism (auth, transport, storage, a component, an endpoint pattern), concluding "X doesn't exist", or investigating/measuring/benchmarking a system that is already running. (It does NOT fire on reading a file to answer a question—if one look settles it, take the look.) Run a discovery pass across every available source: (1) **if an issue tracker is configured and reachable, search it** for the *concern* (product, feature or epic names—not the ticket id); the tracker records what is already built, not just what is planned; if none is available, say so and continue with the other sources; (2) grep the codebase for the concern, not just the ticket name; (3) if a memory store is configured, search the FULL memory—an injected memory block is usually a truncated top-ranked slice, and a `[+N more]` marker means the rest was cut. Only design—or derive—from scratch after every available source comes back empty. Default stance: "it probably exists, find it." **And when discovery comes back empty for a cross-cutting step, build it ONCE, as shared code.** A step many features need—parse a file, sniff a type, OCR, fetch a URL, store an upload, auth, notify, track a job—is searched as *the step itself* ("how does this codebase parse a docx anywhere?", not "docx on the email path"). This fires on any size: a 5-line parser call repeated per feature is how duplicate implementations accumulate. If a shared one exists but lacks what you need, extend it in place. If none exists, build it in the shared layer and make your feature its first caller. Never a feature-private copy.
7. **When stuck searching, look at the actual surface.** After ~2 dead-end searches for the same fact (grep, schema introspection, file-finding), STOP and switch substrate—don't run a 3rd variation. Go look at what a human would see: screenshot the screen, drive the live UI and read its DOM, query the tracker for the concern, search memory, or pull from the source-of-truth API/app. Ground truth is usually one look away; reverse-engineering *where* a platform persists something is the slow path.

## Specs & Plans

When an issue tracker is configured and reachable, spec, design and implementation-plan output goes into it—a ticket with the full spec in its description—never a local file handed to the user. Reply with the ticket link. With no tracker available, say so and deliver the spec, design or plan in your response instead. A scratch file is fine as your own working copy, never as the deliverable.

## Work Taxonomy

The tracker vocabulary `PRlaunch`, `wrapup`, `bulldozer`, `assign` and `linear-gardener` assume. Canonical—skills reference this section instead of restating it.

- **PROJECT** = a whole board / program-scale grouping (e.g. one product line).
- **EPIC** = one orchestratable chunk—roughly a session's worth, ~5 related tickets/PRs shipping a feature or feature-chunk. Stays open until genuinely done, **including its ops tail** (deploy verification, flag flips, doc updates, follow-ups it spawned); shipping the PRs alone doesn't close it. Re-chunk an oversized epic into session-sized epics chained by blocked-by.
- **Ticket** ≈ one PR.

**Follow-up filing buckets**—every follow-up ticket picks exactly one; nothing files parentless:

1. **CR-deferred 1-off** (small, self-contained finding from a repo-wide review, no cross-ticket ordering) → that project's standing **"Bulldozer 1-offs"** epic (one per board, title exactly `Bulldozer 1-offs`, never closes—create it if missing). File it **unassigned**: it's `/bulldozer`'s queue, not a human's.
2. **Nit/ops follow-up on the feature being shipped** (flag flip, data migration, "turn on once X is live") → the **same epic** as the feature; that epic stays open until it's done.
3. **A bigger idea surfaced mid-session** → its **own new parked epic**, even if it starts with one ticket.

## Code Style

- Concise over verbose.
- Avoid over-engineering—solve the current problem only.
- No unnecessary abstractions for one-time operations.
- Delete unused code completely (no `_unused` variables or `// removed` comments).

## Documentation Pattern

- `CLAUDE.md` / `AGENTS.md` = AI instructions (slim, stable).
- `README.md` = human documentation (what it is, setup, architecture).
- `docs/` = detailed docs (schemas, operations, analysis).

Don't put operational status (what's deployed, what's broken today) in `CLAUDE.md` / `AGENTS.md`.
