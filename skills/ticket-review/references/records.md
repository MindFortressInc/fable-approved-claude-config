# Coverage and run records

Use ordinary JSON files; no queue service is needed. The state root is `${TICKET_REVIEW_HOME:-$HOME/.claude/ticket-reviews}/`, outside any repository. Create the state root and each run directory with mode `0700`, and each JSON file with mode `0600`: ticket snapshots carry descriptions and comments. If you also run this skill from another runtime, point both at the same `TICKET_REVIEW_HOME`: shared coverage keeps the two from reviewing the same batch twice. Use collision-resistant run IDs (UTC timestamp plus a random suffix), not date-only names. Never make `/tmp` the only copy of results or comment receipts.

Each normalized scope has `coverage.json` and `runs/<run-id>/`. Scope identity includes the resolved project filters and priorities, or the explicit epic / target-ID set, so another pass cannot suppress this one's tickets. Store the reviewer mode and the exact expected target IDs in the run manifest. The coverage file points to the current sweep and its terminal per-ticket results:

```json
{
  "version": 1,
  "scope": {"project": "example-project", "priorities": [1, 2]},
  "sweep_id": "20260115T061100Z-a81c",
  "reviewed": {
    "EX-123": {"run_id": "20260115T061100Z-a81c", "verdict": "agree", "publication": "not_needed", "comment_id": null}
  },
  "uncertain": {},
  "failures": {}
}
```

The run directory holds the selected snapshot with its ordering and counts, pinned repo SHAs, full ticket context, reviewer results, orchestrator decisions (including refuted candidates), comment receipts, and read-back verification. Store outputs as they arrive. Write JSON atomically: a temporary sibling, then rename.

For a queue's `next`, refresh eligibility and rank again, then exclude this sweep's `reviewed` and `uncertain` IDs. Explicit ticket and epic reviews bypass that skip: a just-authored or revised spec needs a fresh review of the requested IDs. Never derive coverage only from comments: an `agree` verdict legitimately has none. Do not use a changing `updatedAt` as the only reviewed/unreviewed signal: the review's own comment changes that timestamp. Uncertain means examined but unresolved, and stays visible in cumulative reports; failures without a usable verdict stay retryable. Attempt a failure at most once per invocation so one ticket cannot stall `--drain`.

`new-sweep` resets the current coverage maps under a new sweep ID and keeps every prior run record. `recheck <ID>` revisits named IDs and replaces their current coverage entries after a new verdict; it keeps the old run. An explicit changed-scope request gets its own coverage file. Never suppress a ticket permanently across future sweeps.

Before posting, store the intended comment body with the result. Track publication as `not_needed`, `report_only`, `pending`, `verified` or `failed`, separately from the review verdict. After posting, record the comment's id or URL and read it back. If publication is uncertain, leave a pending receipt and inspect the tracker on resume before attempting another write. Reconcile pending and failed publication before selecting new tickets; report-only mode still forbids writes. A report-only run can record coverage; if later asked to publish its findings, re-validate the saved proposals rather than assuming skipped coverage means nothing needs publishing.

Only one orchestrator writes a scope ledger at a time. Before selecting, take the scope's lock atomically, so the lock never exists without its owner record: write the run ID, host, pid and start time to `runs/<run-id>/owner.json` first, then `ln runs/<run-id>/owner.json <scope>/lock.json`. A hard link fails if `lock.json` already exists, and it appears complete or not at all, so a crash leaves either no lock or a lock that names its owner. Hold it through selection, comment publication and every ledger update; remove `lock.json` when the run ends. Atomic JSON renames alone do not stop two runs from selecting the same tickets or one ledger update overwriting another. If the lock exists, another run is active: wait, or stop and report it. Treat it as stale only on positive evidence that its owner stopped: its pid is gone on its recorded host. Silence is not that evidence; a live run can wait on reviewers for longer than a batch's runtime without writing a record. If you cannot check the recorded host, stop and report the lock rather than take it.

Recover a stale lock under an exclusive recovery claim, so only one orchestrator reconciles it: `ln runs/<run-id>/owner.json <scope>/recovery.json`, which fails if another recovery holds it. If the link fails, stop and report; never reconcile without the claim. Holding it, re-check that `lock.json` still names the same stopped owner, then reconcile that run's pending receipts and coverage, including any publication retry, and never overwrite the other writer's state. Only after reconciliation completes, take over: atomically replace the lock with your own record (copy `owner.json` to a temporary sibling of `lock.json`, then rename it onto `lock.json`), then remove `recovery.json`. A run that finds `recovery.json` treats the scope as locked; if that claim's owner is gone, a recovery died mid-reconciliation: stop and report both files rather than take either.
