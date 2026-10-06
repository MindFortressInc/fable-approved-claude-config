# Classifier worker brief — template (stage 4: stray sweep)

Fill every `<...>` slot before dispatching. One brief per chunk (`classifier.chunk_size` tickets, default 120). This is the `briefs` skill's six-section contract, pre-filled for a linear-gardener classification chunk — follow it exactly, don't drop a section.

```
CONTEXT: Tracker workspace is <workspace/team name>. This is chunk <N> of <TOTAL>
from the <target project/board> stray-sweep inventory pulled at <ISO timestamp,
or the file/commit the inventory is saved as>. You are ONE of <TOTAL> parallel
READ-ONLY classifiers; the orchestrator merges all chunk verdicts afterward and
applies moves itself — you never write anything. Destination buckets available
this run: <bucket A>, <bucket B>, <bucket C>, ... (derived from cohort scope /
initiative_map in gardener.config.json).

TASK: Classify each of the <N_TICKETS> tickets in the attached chunk into exactly
one destination bucket, purpose-over-layer — what the ticket is FOR, not which
repo/label/layer it happens to touch. Return one verdict per ticket.

CONSTRAINTS:
- READ-ONLY. Do not call any write/mutate tool (no issueUpdate, no save_issue, no
  state changes) — you propose, the orchestrator alone applies (stage 5).
- Classify by purpose, not surface label/repo. A <label>-labeled ticket whose
  actual purpose is <other bucket> belongs in <other bucket> because of what it's
  FOR, not because of the label sitting on it.
- Use `title_index.json` (attached, covers the WHOLE inventory, not just this
  chunk) to resolve a ticket's parent when the parent lives in a different chunk.
- Gotcha (verbatim): tree-coherence requires a child's destination bucket to
  equal its parent's destination bucket — if you can see the parent's verdict
  (in this chunk or via title_index.json), your child verdict MUST match it;
  flag (don't silently resolve) any case where you can't determine the parent's
  bucket.

RETURN CONTRACT: reply with exactly this JSON, no prose around it:
{
  "chunk": <N>,
  "verdicts": [
    {"id": "<ticket id>", "title": "<ticket title>", "parent_id": "<id or null>",
     "bucket": "<destination bucket>", "confidence": "high|medium|low",
     "reason": "<one line: why this bucket, purpose not layer>"}
  ],
  "unresolved": [
    {"id": "<ticket id>", "reason": "<why you could not classify -- e.g. parent
     bucket unknown, ambiguous purpose>"}
  ],
  "notes": {
    "finding": "<reusable fact for the orchestrator to persist, or null if
     nothing new was learned>",
    "evidence": "<the source excerpt or command output backing `finding`, or null>"
  }
}

VERIFICATION REQUIREMENT: before returning, re-read every verdict where you could
see the ticket's parent's bucket (in-chunk or via title_index.json) and confirm
child bucket == parent bucket; move any mismatch you can't resolve into
`unresolved` rather than guessing.

STOP CONDITIONS: if a ticket's title/body/labels are too sparse to infer purpose
with even low confidence after reading its parent context, put it in
`unresolved` with the reason -- do not force a guess into `verdicts`. If
`title_index.json` is missing or unreadable, stop immediately and return an
empty `verdicts` array with `unresolved` containing one entry explaining the
missing dependency -- do not classify blind.
```

## Notes for the orchestrator building this brief

- **Per-ticket data to attach**: title, labels, parent id/title, a short body snippet (a few hundred characters is enough — full bodies blow the chunk's context for no classification benefit).
- **`title_index.json`** must cover the full inventory (every ticket's id + title + parent id), not just the current chunk — it's the only way a classifier resolves a parent that landed in someone else's chunk.
- **Bucket list** should come straight from the cohort scope for this run (the destination projects/boards in play), not be re-derived per chunk — every classifier in the fleet must see the identical bucket list or their verdicts won't merge cleanly.
- **Chunk size** is `classifier.chunk_size` from `gardener.config.json` (default 120) — sized to stay well under a worker's context/token cap while keeping the fleet small enough to merge by hand.
