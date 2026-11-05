# Deep review reference: plan mode, LT review, publish, re-review, LT direct fix

Part of the deep-review process. The core that runs on every review is section 1 of `~/.claude/commands/PRlaunch.md`; `~/.claude/commands/deep-review.md` maps every step to its file. **Read this file when:** you post a review on GitHub, plan a Deep review with an LT, handle a re-review round, or apply an LT direct fix (Steps 6-10).

## 6. Plan Mode (deep review only)

* Define: scope, agents to launch, what to validate empirically, expected risks
* LT approves before executing
* Not needed for standard/quick — the overhead doesn't justify the value

---

## 7. LT Review

* Review document before publishing
* Adjust: tone, severity, focus, wording
* Verify cross-references between related PRs are correct
* **Check that finding origins (IN-SCOPE / ADJACENT / OUT-OF-SCOPE) are correctly classified** — misclassification leads to unfair blocking or missed improvements

---

## 8. Publish

### GitHub reviews

* One review per PR, concise
* Each finding: severity + origin + `file:line` + code snippet + suggested fix
* Cross-reference between related PRs
* Verdict: REQUEST CHANGES / APPROVE WITH CHANGES / APPROVE
* **Anti-nit-flooding (v6.8):** prefer a small number of high-conviction comments over a long cosmetic list. If structural or functional issues exist, don't bury them under LOW/style nits — fold nits into one collapsed section or drop them.
* **Zero-score drop-list (v7.1):** the following categories are zero-conviction by convention and never surface as standalone comments — drop them outright rather than folding into a collapsed section: docstrings/type hints/comments, unused-import removal, missing imports, more-specific exception types, and questioning entities that may be defined elsewhere in the codebase. **Evidence overrides this list**, consistent with Step 4's confidence-threshold rule: this drop applies to style-only, no-evidence comments in these categories — a missing-import or exception-type finding backed by an *executed* check (a failing import, a traced compile/runtime error) is a demonstrated defect, not a nit, and must still surface with its evidence line.
* **Heavy-lift disposition is published (v7.7):** every heavy-lift CRITICAL/HIGH (Step 4 § effort axis) publishes with its disposition on the finding itself — `BLOCKING`, or `WAIVED: <reason> → <follow-up ticket URL>`. A heavy-lift finding published with neither, or with "follow-up later", is a process defect; fix the review before posting it.
* **Noise-suppression classes (v7.7):** extends anti-nit-flooding with three named classes that make up ~18% of CodeRabbit's output and resolve at or below the 65% baseline. Do not mirror them:
  * **model-id currency** — do not flag a model identifier as nonexistent, deprecated, or outdated against the reviewer's own memorized (training-data) model catalog; that list is stale in both directions (a reviewer has called a live model nonexistent and recommended one already retired). The only reference is the repo's own authoritative model catalog (e.g. the module or config file where the repo defines its model ids). This suppresses the *memorized-catalog* check, not model ids: an id that contradicts the repo's authoritative model catalog — absent from it, or behind the tier it marks current — is still a valid finding; cite the catalog line as its evidence.
  * **lint / typing** — do not publish what the repo's CI linter/type-checker already enforces (ruff/mypy/eslint/tsc-class findings). Exception: a type error demonstrated by an executed check, or a rule the repo's CI config does not actually run, is not suppressed.
  * **shared-client convention** — do not publish "use the shared client/wrapper" as a general finding. Where it is a real rule, the repo writes it down as an invariant and Step 3's loader supplies it; publish it only citing that loaded invariant. A rule that is real in one repo and merely conventional elsewhere is repo-level by definition, never a general lens.

### Ticket-compliance publish block (v7.4)

When Step 0 resolved a ticket and the Step 3 ticket-compliance agent ran, publish a compliance block **above the findings list**, before any per-finding detail:

```
Ticket compliance — <TICKET-ID>: <title>
======================================

**Fully compliant (N):**
- <requirement> — <file:line>

**Not compliant (N):**
- <requirement> — CLAIMED by the PR, now a HIGH finding (see B/F<n> below)
- <requirement> — not mentioned in the PR, filed as follow-up <TICKET-ID>

**Requires human verification (N):**
- <requirement> — <why code alone can't judge it>

**Scope drift (N):**
- <diff hunk> — no ticket requirement asked for this [, escalated to MEDIUM: touches <auth/payments/data>]
```

Every `not_compliant_requirement` and scope-drift line here must already exist as a finding in the findings list below, carrying the severity assigned by Step 4 § Ticket-compliance severity mapping — this block is a summary and index into the findings, never a second, unlinked verdict.

**Graceful degradation:** no ticket resolved (Step 0) → omit this block entirely. No placeholder in the GitHub review; the INFO note lives only in the internal review document per Step 0 § Graceful degradation.

### Codex-origin findings: publish gate (v7.5)

Read `codex-lane.md` before you publish any finding from the Codex lane. The gate is there.

### Multi-repo publish discipline

**For paired PRs, every** `gh pr review` and `gh pr view` must be run from the correct repo directory. `gh` resolves the target repo from the local `.git` remote — running it from the wrong directory silently posts to the wrong repo's PR number.

**Pattern:** in one paired review, `gh pr review <N>` was run from the backend directory. It posted the review on the backend's PR with that number (an old, already-merged PR) instead of the frontend's. The command succeeded silently — the error was only caught because a human checked GitHub.

**Rule: prefix every** `gh` publish command with an explicit `cd` to the target repo:

```bash
cd /path/to/backend-repo && gh pr review 113 --comment --body "..."
cd /path/to/frontend-repo && gh pr review 80 --comment --body "..."
```

### Communication

* Message to PR owner with top 3 findings and links
* Alerts to team if cross-PR conflicts detected (e.g., migration collisions)

---

## 9. Re-review

When the owner pushes fixes, follow the **Re-review process** defined in Step 0. Key points:

* Analyze only the delta (new commits since last review)
* Verify HIGH+ findings are resolved, spot-check MEDIUMs
* **Confirm fixes by evidence, not by reading the diff (v6.9):** `RESOLVED` requires re-running the original finding's evidence command against the new code and quoting the output; a diff-only check is `PARTIALLY RESOLVED (unconfirmed)`.
* Scan fix commits for regressions
* Update the existing review document (don't create a new one)
* Decide next step:

| Outcome | Action |
| -- | -- |
| All HIGH+ resolved, MEDIUMs acceptable | **Approve** (Step 8) |
| Minor residual items, non-blocking | **LT direct fix** (Step 10) → Approve |
| HIGH+ still open or new issues | **Request changes** again |

---

## 10. LT Direct Fix (optional)

When re-review reveals **residual non-blocking items** that the LT can fix faster than another review round. Also used for first reviews where findings are small enough to resolve directly.

### When to use

* Remaining items are MEDIUM or LOW severity
* Fixes are small and well-scoped (< 20 lines per file)
* No architectural decisions needed — the fix pattern already exists in the codebase
* Time-sensitive: another round-trip with the owner would delay the merge unnecessarily
* CI is red from **cross-PR contamination** (merged code from another branch)
* **Early CI fix (Step 1):** CI is red from syntax errors or import issues in the PR's own code, and the fix is trivial (missing import, type annotation). Apply at Step 1 to unblock the review rather than waiting for Step 10. Document in the review doc as "LT Direct Fix (Step 10 applied early)."

### When NOT to use

* HIGH/CRITICAL items still open — send back to owner
* Fix requires design decisions or the owner's domain knowledge
* Multiple files with complex interdependencies

### Process

 1. **Rebase onto develop first** (if not already done in Step 2) — ensures fixes are applied on top of the latest base. Never fix code that develop has already changed.
 2. **Investigate before fixing** — for each finding, ask: does a fix already exist elsewhere? Is someone else working on this? Is the finding still valid after rebase?
 3. **Assess viability** — for each residual item, classify as viable / not viable now / deferred
 4. **Get LT approval** — confirm scope before coding ("we'll fix X, Y, Z; defer W")
 5. **Implement fixes** on the PR branch — small, targeted changes
 6. **Validate each fix**:
    * **Backend:** Always via Docker. `docker compose run --rm --no-deps app bash -c "..."` for linting, tests, import checks.
    * **Frontend:** `npx tsc --noEmit`, dev server compilation, production build if applicable.
    * Same rigor as Step 5 (runtime, browser automation as applicable).
 7. **Commit with clear attribution** — commit message references finding IDs (e.g., "fix(review): B1+B2+B3 — centralize word count logic")
 8. **Document what's deferred** — commit message and PR comment list remaining items with justification
 9. **Push with** `--force-with-lease` — never `--force`. This protects against overwriting concurrent pushes from the PR owner.
10. **Verify CI is green** — wait for all checks to pass before approving
11. **Confirm merge state** — `gh pr view <N> --json mergeable,mergeStateStatus` must show `CLEAN` + `MERGEABLE`
12. **Approve** — post review approval + updated PR comment with resolved/deferred summary
13. **Communicate** — message to owner with what was fixed, what's deferred, and that PR is approved

### Key rules

* **Verify working directory before every command.** In multi-repo reviews, `gh pr merge`, `git push`, and `git commit` must target the correct repo. A command run from the wrong directory can succeed silently against the wrong PR. Always `cd` explicitly or check `git remote -v` before executing.
* **CI must be green before approval.** If CI fails after your fix commits, diagnose and resolve — don't approve with red checks.
* **Always** `--force-with-lease`. If it fails, someone else pushed — investigate before retrying.
* **Confirm CLEAN + MERGEABLE after push.** GitHub may take a few seconds to recalculate merge state after a force push.
