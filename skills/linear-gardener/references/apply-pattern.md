# Single-writer apply pattern (stage 5)

Only the orchestrator ever calls this pattern — never a classifier or any other agent (see SKILL.md's Common Mistakes: "Letting the classifier fleet write").

## Pattern: batched aliased GraphQL `issueUpdate`

Batch every move from the merged, tree-coherent plan into groups of `apply.batch_size` (default 20 — the proven safe batch size; larger batches have produced partial-batch failures). Each batch is ONE GraphQL request with one aliased mutation per ticket, so a single round-trip moves up to `apply.batch_size` tickets and returns one result per alias — a partial failure is attributable to a specific ticket, not an opaque batch-level error.

### Worked example (batch of 3 — generalizes to N up to `apply.batch_size`)

```graphql
mutation BatchMove {
  m0: issueUpdate(id: "<ticket-id-0>", input: { projectId: "<dest-project-id>", parentId: "<dest-parent-id-or-null>" }) {
    success
    issue { id identifier }
  }
  m1: issueUpdate(id: "<ticket-id-1>", input: { projectId: "<dest-project-id>", parentId: "<dest-parent-id-or-null>" }) {
    success
    issue { id identifier }
  }
  m2: issueUpdate(id: "<ticket-id-2>", input: { projectId: "<dest-project-id>", parentId: "<dest-parent-id-or-null>" }) {
    success
    issue { id identifier }
  }
}
```

Send it with the API key resolved per SKILL.md's Auth section (`$LINEAR_API_KEY` / `$LINEAR_KEY_FILE`, or your config's `auth.api_key_env` / `auth.api_key_file_env` overrides):

```bash
# bash (uses ${!name} indirection). Env var first, then the JSON key file.
KEY_ENV=$(jq -r '.auth.api_key_env // "LINEAR_API_KEY"' gardener.config.json)
FILE_ENV=$(jq -r '.auth.api_key_file_env // "LINEAR_KEY_FILE"' gardener.config.json)
LINEAR_KEY="${!KEY_ENV:-}"
if [ -z "$LINEAR_KEY" ] && [ -n "${!FILE_ENV:-}" ]; then
  LINEAR_KEY=$(jq -r '.env.LINEAR_API_KEY // empty' "${!FILE_ENV}")
fi
[ -n "$LINEAR_KEY" ] || { echo "no Linear API key resolved" >&2; exit 1; }

curl -s https://api.linear.app/graphql \
  --config <(printf 'header = "Authorization: %s"\n' "$LINEAR_KEY") \
  -H "Content-Type: application/json" \
  --data-binary @batch-0.json
```

`batch-0.json` is `{"query": "<the mutation string above>"}` — generate one file per batch from the merged plan programmatically (don't hand-write dozens of these); the alias names (`m0`, `m1`, ...) just need to be unique within a request.

### Gotchas specific to this stage

- **Malformed GraphQL (a missing closing brace) returns HTTP 500, not 400** — a brace-counting bug looks exactly like a transient server error and survives naive retries. Brace-count the generated mutation string before blaming the API or retrying blind.
- **20 mutations per request is the proven safe batch size** — arrived at empirically; don't scale it up without re-validating, and don't scale it down without a reason (more requests = more chances to lose atomicity mid-run).
- **Alias every mutation** (`m0`, `m1`, ...) so a partial failure is attributable to a specific ticket in the response, not an opaque batch-level error.
- **Re-pull and assert after every batch, not just at the end** (stage 6 does the final assert) — catching a partial-batch failure at batch 3 of 40 is far cheaper than diagnosing it after the whole plan believes it's applied.

## Precondition: tree-coherence

Never run this pattern against a plan that still has unresolved tree-coherence conflicts (stage 4b) — a child moving to a different bucket than its parent breaks the board, and this stage has no rollback beyond the reverse-apply check in stage 7.
