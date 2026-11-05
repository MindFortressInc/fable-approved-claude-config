# Reviewer contract

Use **briefs** for the six prompt sections, in order: CONTEXT, TASK, CONSTRAINTS, RETURN CONTRACT, VERIFICATION REQUIREMENT, STOP CONDITIONS. Fill them with the assigned tickets, exact repo refs, relevant repo invariants, and the artifact to return. Do not send a worker to fetch a ticket specification the brief does not carry.

Workers read source and propose verdicts. They never write the tracker, memory, repositories or production. They are not alone in the workspace: they must not revert others' work or use `git stash`, because the stash is one stack shared by every worktree of a repo. Paste the **briefs** strictly read-only Git constraint verbatim. Scratch output belongs in the assigned run directory. Independent groups should not repeat discovery the orchestrator already did.

Write the discovery obligation into each brief: before substantial reconstruction, proposing a mechanism, concluding absence, or re-measuring a running system, search the tracker for the concern, grep the relevant source, and search the full memory store if one is configured. Read the relevant hits in full; an injected memory preview is not the corpus. Reuse existing mechanisms and measurements. A direct file read that settles a question needs no elaborate investigation.

Ask each reviewer to return one JSON object per assigned ticket:

```json
{
  "ticket": "EX-123",
  "verdict": "revise",
  "conclusion": "The migration is deployed; keep the remaining non-default-space acceptance.",
  "evidence": [
    {
      "repo": "owner/repo",
      "sha": "full-commit-sha",
      "path": "api/media.py",
      "line": 42,
      "excerpt": "space_id=render.space_id,",
      "command": "git -C /repo show full-commit-sha:api/media.py",
      "source_url": "https://github.com/owner/repo/blob/full-commit-sha/api/media.py#L42"
    }
  ],
  "decision_sources": [
    {
      "url": "https://tracker.example/issue/EX-122#comment-1",
      "observed_at": "2026-01-14T20:00:00Z",
      "excerpt": "Migration applied; live clip acceptance remains."
    }
  ],
  "runtime_evidence": [],
  "remaining_acceptance": "Observe one generated clip in a non-default space.",
  "proposed_comment": "Review: narrow remaining scope to the live clip check; migration completion is already recorded in the linked deployment evidence.",
  "uncertainty": "No paid generation was run.",
  "memory_handoff": null
}
```

Use `proposed_comment: null` for agreement or insufficient evidence. Source excerpts must be literal contiguous lines starting at `line`; explanations belong in `conclusion`, never disguised as quotations. Cite exact PR head refs when reviewing unmerged work and label them unmerged. A missing or inaccessible source yields `uncertain`, not agreement.

The review lenses:

- Does the reported problem still exist? Did another ticket or PR complete or supersede part of it?
- Does the proposed change move toward the ratified architecture and user goal? Is the component it touches being removed or consolidated?
- Are tenancy, credentials, scopes, money and data ownership bound at the right shared layer?
- Are rollout prerequisites still real? Does the ticket distinguish the API, worker, image, schema and runtime configuration?
- Can the acceptance test prove the complete goal? A dry run can omit persistence; an anonymous 401 proves the route exists, not an authenticated flow; an environment file can differ from the process environment.

Try to disprove a proposed disagreement before returning it. Do not duplicate objections already acknowledged with the same remaining action. Keep historical evidence labeled historical, and say what was actually observed this run. Do not re-run expensive measurements or production actions to avoid recording uncertainty.

Stop after two dead ends for one fact: switch to the tracker, memory or the actual app surface, or report the unresolved evidence. Return partial results for the other assigned tickets instead of widening scope.
