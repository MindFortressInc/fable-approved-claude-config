---
name: done-without-evidence
description: Recurring sweep that flags Linear tickets closed with no started state, no PR attachment, and no commit — "Done is not evidence" made a standing check. Reports findings as a comment on a standing ticket; never mutates any ticket's state. Use for "/done-without-evidence", "run the done-without-evidence sweep", "check for tickets closed with no work behind them", "is anything marked Done that was never actually built".
argument-hint: "--team NAME [--project NAME] [--limit N] [--dry-run]"
allowed-tools:
  - Bash
---

<objective>
A tracker's "Done" is a claim, not evidence. This skill exists because a bulk
close once marked a batch of tickets Done that had no work behind them, and it
took a one-time audit to find the unbuilt ones. This is the RECURRING TOOL, so
the same failure mode — a ticket marked Done with no work behind it — gets
caught going forward, not just once.

`done_without_evidence.py` flags tickets on a team in state type "completed"
(Done) that have none of the three evidence signals Linear itself carries:

  - `startedAt` is set     — the issue ever entered a started-type state
  - a GitHub PR attachment — `Attachment.url` matches `.../pull/<n>`
  - a GitHub commit attachment — `Attachment.url` matches `.../commit/<sha>`

and are not whitelisted via the `no-code-expected` label (ops/manual tasks,
decisions, docs — legitimately "done" with no code). Canceled tickets are never
flagged: canceling doesn't claim work was done, only completing does.

REPORT-ONLY. It never changes a ticket's state, assignee, or labels. Its only
writes (outside `--dry-run`) are: lazily creating the `no-code-expected` LABEL
DEFINITION if the team doesn't have it yet (never applying it to anything —
a human decides which tickets earn the whitelist), find-or-create ONE standing
report ticket, and posting the sweep result as a comment on it.
</objective>

<process>

## Step 1 — Run the sweep

```bash
python3 ~/.claude/skills/done-without-evidence/done_without_evidence.py sweep ${ARGUMENTS:-}
```

- `--team NAME` (required) is the Linear team to audit. If the owner didn't name
  one, ask — there is no built-in default team.
- `--project NAME` is the project the standing report ticket is filed under.
  It's only needed on the first live run (when no standing ticket exists yet);
  after that the ticket id is cached.
- `--limit N` caps how many closed issues are scanned (paginates 100 at a time
  otherwise, oldest pages last since the query orders by `updatedAt`).
- `--dry-run` fetches and prints the report only — no label lookup/creation, no
  standing-ticket search/creation, no comment. Use this to preview before the
  first real run, or any time you want the read without the write.
- Credentials: `$LINEAR_API_KEY`, or a JSON file named by `$LINEAR_KEY_FILE`
  holding `.env.LINEAR_API_KEY` (same lookup as `skills/assign/assign.py`). No
  key → the script errors loudly rather than silently reporting "clean".

Without `--dry-run`, the script itself performs steps 2–3 (label find-or-create,
standing-ticket find-or-create, comment post) — there is nothing further for you
to do by hand. The sections below describe what it does, for when you need to
debug or reason about the output.

## Step 2 — (script does this) Lazily ensure the whitelist label exists

`no-code-expected` is looked up on the team by name; if absent, the script
creates the label definition once (never applies it to any ticket). Apply it to
a specific ticket yourself, by hand, when a flagged ticket really was done with
no code — ops task, decision record, research spike, docs.

## Step 3 — (script does this) Find-or-create the standing report ticket, post the comment

Same pattern as `skills/scorecard`: a local `state.json` next to the script
(gitignored — runtime state, never committed) caches the standing ticket's id
so repeat runs skip the search. On a cache miss it searches Linear for a ticket
titled "Done-without-evidence sweep — standing (reports as comments)" on the
team; if none exists, it creates one — with the `--project` you passed and a
**priority** (3/Medium), since a ticket filed without a project or priority is
a ticket nobody triages. The report is always a fresh COMMENT on that ticket,
never an edit to its description, so the thread reads chronologically.

## Step 4 — Read the result

The report is the same markdown printed to stdout and posted as the comment: a
clean sweep says so in one line; violations render as a table (ticket link,
state, title) followed by the whitelist/reopen instruction. Nothing needs
reformatting before relaying it.

</process>

<report>
Reply with: the markdown report from Step 1, and (unless `--dry-run`) the
standing ticket's identifier + the comment URL the script printed. If you ran
`--dry-run`, say so plainly — nothing was written to Linear this run.
</report>

<notes>
- Classification is scoped to state type **"completed"** only, not "canceled" —
  canceling a ticket doesn't claim work was done, so it isn't audited the same
  way. If that scope ever needs to widen, it's a one-line change to
  `is_closed()` plus a test, not a redesign.
- `has_pr_evidence`/`has_commit_evidence` match on the attachment **URL shape**
  (`github.com/.../pull/<n>` or `.../commit/<sha>`), not `Attachment.sourceType`
  — checked live against real closed tickets when this was built:
  `sourceType`/`source.type` come back generically `"api"` for both PR and
  commit links regardless of owner (personal and org repos alike), so the URL
  pattern is the only reliable cross-repo signal.
- `startedAt` alone can be false while `has_pr_evidence` is true (a ticket can
  move straight from Backlog to Done with a PR attached, skipping a
  started-type state) — a real shape seen live, so `classify_issue` treats
  PR/commit evidence as sufficient on its own; all three absent is what's
  flagged, not `startedAt` alone.
- The pure classification/rendering functions (`classify_issue`, `sweep_issues`,
  `render_report`, evidence/whitelist/closed predicates) are unit-tested with
  zero network calls in `tests/test_done_without_evidence.py`. The live Linear
  calls (`ensure_label`, `find_or_create_standing_ticket`, `post_comment`,
  `run_sweep`'s dry-run-never-writes contract) are tested against a stubbed
  `gql_fn` — no test reaches the network, and none require a Linear API key.
</notes>
