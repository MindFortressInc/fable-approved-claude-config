# Claude → Codex ticket/epic review (optional, off by default)

Codex mode sends a ticket or epic review to a second model family. It runs only when the user asks for `--via codex` **and** `CODEX_REVIEW_ENABLED=1` is set. With the flag unset, report that Codex mode is disabled and stop; never substitute native agents for a requested Codex review.

```text
/ticket-review epic EX-12345 --via native
/ticket-review epic EX-12345 --via codex
/ticket-review tickets EX-12346,EX-12347 --via codex
```

If Claude just authored the epic, use the IDs it actually created and fetch their latest tracker text; do not ask the user to repeat it.

## Freeze the scope and carry the spec

Before dispatch, store `handoff.json` in the durable run directory:

- `requested_ticket_ids`: the exact selected review targets, with no duplicates.
- `epic`: id/URL and the complete description containing the goal, architecture decisions and acceptance. Null for a standalone ticket list with no shared epic.
- `tickets`: each target's id/URL, full unedited description, status, priority, parent, dependencies, comments and linked PR states, plus the snapshot time.
- `repos`: absolute paths and full pinned SHAs; the relevant repo instructions and review invariants.
- `discovery`: pertinent tracker, memory and prior-art findings with their sources, including ratified decisions that constrain the design.
- `runtime_evidence`: timestamped observations and explicit unknowns, kept separate from code defaults.

For epic mode, resolve the open leaf descendants once and include the exact resulting IDs. For ticket-list mode, use exactly the supplied IDs. Priority and the default batch size do not drop newly created Normal/Low tickets. A missing ticket or an empty description is an explicit unresolved item; never silently replace it with another ticket or invent a spec.

Write one six-section **briefs** prompt per lane, with disjoint target IDs. Each prompt carries the epic goal and the full text of its own tickets. Carry every named epic and ticket description verbatim between fixed markers, and name all carried IDs in the prompt's own CONTEXT:

```text
CONTEXT
Review targets: EX-12346, EX-12347. Context-only parent: EX-12345.
Requested outcome: review this epic's proposed tickets against code and the epic goal.

<<<TICKET EX-12345 VERBATIM>>>
[paste the complete parent description unchanged]
<<<END TICKET>>>
<<<TICKET EX-12346 VERBATIM>>>
[paste the complete first target description unchanged]
<<<END TICKET>>>
<<<TICKET EX-12347 VERBATIM>>>
[paste the complete second target description unchanged]
<<<END TICKET>>>
```

Complete TASK, CONSTRAINTS, RETURN CONTRACT, VERIFICATION REQUIREMENT and STOP CONDITIONS from [the reviewer contract](reviewer-contract.md). Restrict output to the assigned targets; parent/context IDs get no per-ticket verdict. Paste the literal repo invariants and checked references: Codex does not inherit Claude's context. The receiver must not infer new scope from the project name or invoke this skill recursively.

Use the **briefs** strictly read-only Git constraint, verbatim. Tell Codex to use `python3`, never plain `python`, for anything it runs itself.

## Dispatch

Codex runs with no tracker or memory access of its own, so Claude does all discovery and carries the snapshots in the brief. Context the brief lacks comes back as uncertainty, not invented facts.

1. Put lane prompts at `<run>/briefs/r1_brief.txt`, and so on. Check exact ID and body coverage yourself: every requested ID carried once, every body identical to the tracker snapshot.
2. Dispatch plain `codex exec` per lane — read-only is not a knob, and the `review` subcommand ignores `--output-schema`:

   ```bash
   MODEL_FLAG=()   # an array: zsh does not word-split, so a string "-m x" would arrive as one argument
   [ -n "${CODEX_REVIEW_MODEL:-}" ] && MODEL_FLAG=(-m "$CODEX_REVIEW_MODEL")
   codex exec -s read-only -C /absolute/repo "${MODEL_FLAG[@]}" \
     --output-schema "$HOME/.claude/skills/ticket-review/references/codex-ticket-result.schema.json" \
     -o "$REVIEW_RUN/codex/r1_result.json" \
     - < "$REVIEW_RUN/briefs/r1_brief.txt" > "$REVIEW_RUN/codex/r1_stream.log" 2>&1
   echo "exit=$?"
   ```

   Use real resolved paths and IDs. Keep each lane's exit status; do not mask it with a pipe. Record the model that actually ran next to the result. The checkout Codex reads can differ from the handoff's pinned refs: keep the pins and the refs Codex actually cited as review evidence.
3. Parse each `r1_result.json` against [codex-ticket-result.schema.json](codex-ticket-result.schema.json).
4. Compare each lane's result IDs with its expected ID set: exact equality, no extras, no missing IDs, no duplicates. Check the verdict vocabulary (`agree`, `revise`, `superseded`, `uncertain`) and nonempty literal evidence behind every supported conclusion. The schema uses strings for optional prose: normalize an empty `proposed_comment`, `uncertainty` or `memory_handoff` to null in the review records. A missing result, an invalid payload or a failed lane is missing coverage, never a clean review. Inspect failure or refusal output; never reword a brief to get past a refusal, and never silently substitute a reviewer.

Codex returns verdicts, evidence and proposed comments only. Claude independently checks the sources and decisions, refutes weak claims, and publishes any surviving material disagreement through the single-writer path. Codex makes no tracker writes, edits, builds or new issues in this mode. The final report names the reviewer mode and accounts for every requested ticket, plus any epic-wide coherence findings.
