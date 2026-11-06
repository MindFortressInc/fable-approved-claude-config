# PRlaunch — why the rules are shaped this way

Reference only. Every rule here runs from `~/.claude/commands/PRlaunch.md`; this file holds the incidents and measurements behind them, so the command stays under the one-plain-read budget (`tests/test_deep_review_layout.py`, `PRLAUNCH_MAX_BYTES`). Read it when you are changing one of these rules, not to run a launch.

## CR CLI

**Why the wrapper (`hooks/cr-review.sh`).** With nothing configured it runs the CLI once under your own login, exactly like the bare command. What it adds is one exit-code contract every caller can trust. CodeRabbit rate-limits *per developer* (a few CLI reviews per hour per seat, on a rolling window), so parallel agent workers on one login all burn one bucket. With the optional seat pool, the wrapper picks the least-recently-used seat under cap, holds a per-seat lock so two workers never share a seat, rests a seat for 15 min when it reports a limit, and gives up (exit 75) only when every seat is spent.

**Why never `| tail -N`.** `tail -N` buffers until EOF, hiding progress on a long run. In a background command only the final tail reaches the output file: one run surfaced 5 findings, `| tail` kept 1, and the review had to be redone.

**Exit 71.** CodeRabbit removed `--plain`, so the documented command line started exiting 1 — indistinguishable from an adverse review — and every unattended sweep read permanent misconfiguration as a CR failure. The wrapper maps an option-parsing rejection to 71: no review ran, no quota was spent, and the identical invocation fails forever.

**Why the exit code, never stdout.** A killed run that exits 0 with no output lets a caller record a clean gate for a review that never happened. Empty stdout reads exactly like "reviewed, found nothing", so the exit code is the only signal that tells them apart.

**Hung runs.** A hung `coderabbit review` holds the slot (and, with a pool, a seat lock), which is why exit 75 is checked against `pgrep` before it is treated as terminal.

## publish-cr-cli ordering

Phases 1–4 record every gate on the local, unpushed branch, and GitHub's Statuses API 422s on a sha it has never seen (`gh: No commit found for SHA … (HTTP 422)`). So `record cr_cli`'s inline publish no-ops on essentially every unit, and a review-gate check stays yellow for a reason nobody can see from the Checks tab — indistinguishable from "nobody ran cr_cli". Publishing after the push and before `gh pr create` puts the attestation on the sha before the PR-opened event makes a review-gate workflow classify it.

## Outcome eval

**Why the real viewport.** A mobile-width harness silently turned a side panel into a full-screen dialog and hid the surface under test; the eval graded a layout no user would see.

**Why open the file.** A brand-image mirror was graded ✅ on `200` + bytes-match over a random `picsum` filler photo nobody had opened. The round-trip was sound; the artifact was junk. This is also why the eval uses real input, not filler.

## Ops steps as tickets

Three ops steps once went into a single epic comment. Comments and prose have no status, no assignee and no board presence, and an unprovisioned storage bucket silently blocked a whole feature lane.

## One brain

A fix that patches shared behavior on one surface ships duplication that behavioral gates can't catch: the copies pass identically until they diverge. A per-lane fix once shipped while a sibling lane kept the bug.
