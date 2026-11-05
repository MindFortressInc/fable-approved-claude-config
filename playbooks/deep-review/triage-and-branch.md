# Deep review reference: triage and branch alignment

Part of the deep-review process. The core that runs on every review is section 1 of `~/.claude/commands/PRlaunch.md`; `~/.claude/commands/deep-review.md` maps every step to its file. **Read this file when:** you are reviewing a PR rather than your own local diff, this is a re-review, CI is red, the diff is over 10K lines, other open PRs may share these files, or the core's Step 2 check shows the branch is behind its base.

## 0. Input

Receive PR link(s). Identify if there are related PRs (e.g., backend + frontend for the same feature).

### Determine review iteration

| Iteration | Trigger | Scope |
| -- | -- | -- |
| **First review** | New PR or first time reviewing | Full process (steps 1-9) |
| **Re-review (Nth)** | Owner pushed fixes after previous review | Focused process (see below) |

### Re-review process (2nd+ iteration)

1. **Load prior context** — read the previous review document (`DEEP_REVIEW_*.md`) and GitHub review comments
2. **Diff since last review** — only analyze commits pushed after the last review (`git log --since="<last review date>"` or compare commit SHAs)
3. **Verify HIGH+ fixes** — for each HIGH/CRITICAL finding from the previous round:
   * Is the fix correct and complete?
   * Does it introduce new issues?
   * Mark as: RESOLVED / PARTIALLY RESOLVED / NOT ADDRESSED
   * **Evidence-based confirmation (v6.9):** marking a finding `RESOLVED` requires **re-running the original finding's evidence command against the new code and quoting the output** (e.g., the grep now returns nothing, the endpoint now returns 401). A fix verified only by *reading the diff* is `PARTIALLY RESOLVED (unconfirmed)`, not RESOLVED.
4. **Spot-check MEDIUM fixes** — verify a sample, not necessarily all
5. **Scan for regressions** — new code in the fix commits could introduce new problems; run a quick security + quality pass on the delta only
   * **New findings require empirical validation** — if the delta introduces new functionality (scope expansion), any new finding must be validated with grep/code trace, not just observed from the diff. The re-review shortcut does not exempt from evidence standards.
6. **Re-validate runtime** if the fix touches critical paths (payments, auth, data integrity)
7. **Update the review document** — add a "Re-review" section with date, findings status, and new findings if any
8. **Post follow-up on GitHub** — concise comment referencing the original review, marking which findings are resolved
9. **Approve or request another round** — if all HIGH+ resolved, approve; otherwise request changes again

### Key differences from first review

* **No full agent sweep** — only run agents on the delta (new commits), not the entire PR
* **No full triage** — depth classification carries over from the first review
* **Faster** — focus is verification, not discovery
* **Document continuity** — append to the existing review document, don't create a new one

## 1. Triage

### Gather context

* Read: title, description, **existing comments/reviews**, **CI/checks status**
* Check files changed, additions/deletions, commit count
* Check merge state: `gh pr view <N> --json mergeable,mergeStateStatus`

### CI / Checks status

* If CI is **red from the PR's own code** → stop, owner must fix before review proceeds
* If CI is **red from merged code** (e.g., develop merge brought in another PR's bug):
  1. **Investigate first**: who introduced it? Is there already a PR fixing it? How recent is the merge?
  2. Only after understanding the context, note in review. LT may fix directly if trivial (see Step 10)
  3. **Never fix blindly** — the fix may already exist in develop or in another open PR
* If CI is **red from infrastructure** (e.g., deprecated GitHub Actions, missing Docker image, build platform config):
  1. **Always check actual build logs** before classifying — use `vercel inspect <url> --logs`, `gh api .../jobs/{id}/logs`, or equivalent. Do NOT assume root cause from prior patterns (e.g., "build platform fails = permissions" was wrong once — actual cause was a TypeScript error).
  2. If fixable (workflow files, migration guards, type fixes): fix directly at Step 10, CI must be green before approval.
  3. If permissions/external (platform team roles, expired tokens): note as non-blocking, inform team separately.
  4. **Expect cascading failures** when fixing CI from scratch — each fix may reveal the next layer (e.g., artifact version → Docker image → migration heads → migration enum). Budget 3-4 iterations.
  5. **CI fix ≠ production fix.** When fixing infrastructure issues (e.g., `alembic upgrade heads`), grep ALL entrypoints — not just CI workflows. Production startup scripts (`start.sh` etc.), Dockerfiles, and Procfiles may have the same bug. In one real deploy, the migration command was fixed in GitHub Actions but missed in the production startup script → deploy failure + 8 min downtime.

### Large PRs (>10K lines)

When `gh pr diff` fails with HTTP 406 ("diff exceeded maximum number of lines"), the GitHub API cannot serve the full diff. Fallback to local git:

```bash
# Full diff (may be very large)
git diff origin/develop...HEAD > /tmp/pr_full_diff.txt

# Focused diff on critical areas (preferred for agents)
git diff origin/develop...HEAD -- app/api/ app/routers/ app/services/ > /tmp/pr_critical_diff.txt
```

For agent prompts on large PRs, save focused diffs to temp files and include them in the prompt rather than passing the full 30K+ line diff. Split by area (routers, services, models, migrations) if needed.

---

## 2. Branch Alignment (mandatory)

**Before any analysis, ensure the PR branch reflects what will actually be merged.**

A review performed against stale code is a review of something that won't exist after merge. Findings may be redundant (already fixed in develop), misleading (caused by code develop already changed), or wrong (conflicting with changes the reviewer can't see).

### Process

1. **Checkout PR branch** and fetch latest:

   ```
   git checkout <pr-branch>
   git fetch origin develop
   ```
2. **Check divergence** from target branch:

   ```
   git log --oneline origin/develop..HEAD   # PR commits not in develop
   git log --oneline HEAD..origin/develop   # develop commits not in PR
   ```
3. **If develop has new commits**, rebase:

   ```
   git rebase origin/develop
   ```
4. **If conflicts arise**, evaluate:

   | Conflict type | Action |
   | -- | -- |
   | Trivial (imports, formatting, non-overlapping) | Resolve and continue |
   | Substantive (same logic modified by both sides) | **Stop.** Send back to owner to resolve — they have the domain context |
   | CI contamination fix already in develop | **Drop the redundant fix** — develop's version wins |
   | Squash-merge ancestry (see below) | Skip duplicate commits, verify survivors |
   | Orthogonal divergence (see below) | Skip rebase, note divergence, proceed |
5. **After rebase, verify surviving commits:**
   * Does every surviving commit belong to the PR author?
   * Are there residual commits from a parent branch that was squash-merged separately?
   * Are there non-code artifacts (plan docs, design files) that hitchhiked from the parent branch?
   * Remove anything that doesn't belong to this PR's scope.
6. **After rebase**, verify the build compiles before proceeding:
   * **Backend:** `docker compose run --rm --no-deps app bash -c "flake8 --count ..."` (or your linter)
   * **Frontend:** `npx tsc --noEmit`
7. **Do NOT push yet** — the rebase is for the reviewer's analysis. Push only after fixes are applied (Step 10).

### Multi-repo reviews: directory verification

When reviewing paired PRs (backend + frontend), every `git` and `gh` command must target the correct repository. **Do not rely on implicit working directory** — verify explicitly before executing.

In one paired review, `gh pr merge <N>` (frontend) was executed from the backend directory. It "succeeded" silently against the wrong repo, and the frontend PR remained OPEN. The error was only caught because a human checked GitHub manually.

**Rule: for multi-repo reviews, prefix every** `gh`/`git` command with an explicit `cd` to the correct repo, or verify the remote URL before executing.

### Squash-merge ancestry

When a developer branches from another feature branch (not develop), and that parent branch is later squash-merged into develop, the individual commits from the parent become duplicates that conflict during rebase. This is expected.

**Pattern:** one PR was branched from another's branch. When the parent was squash-merged into develop, the child inherited 18 duplicate commits. During rebase, all 18 conflicted and had to be skipped. Only the PR author's own commits survived.

**Rule: after rebasing a branch with squash-merge ancestry, verify that only the PR author's commits survive. Watch for residual commits and non-code artifacts (plan docs, design files) from the parent branch.**

### Orthogonal divergence

When develop has new commits but the rebase produces substantive conflicts, check whether the missing commits touch any files in the PR:

```bash
# Files changed in develop since PR branched
git diff --name-only HEAD..origin/develop

# Files changed in the PR
git diff --name-only origin/develop...HEAD
```

If there is **no overlap** between the two file sets, the divergence is orthogonal — the missing develop commits and the PR are changing completely different parts of the codebase. In this case:

1. **Abort the rebase** (`git rebase --abort`)
2. **Note the divergence** in the review doc (what's missing, why it's safe to skip)
3. **Proceed with the review** — the PR code is valid for analysis even without the latest develop
4. **The owner must still rebase before merge** — this skip is for review purposes only

### Why this step exists

In one review, a fix was written for a bug only to discover during a late rebase that develop had already resolved the same bug differently. The fix was redundant work, and the resulting merge conflict had to be resolved manually. Rebasing first would have revealed that the issue was already solved and eliminated an entire finding from the review.

**Rule: context before action. Understand what develop already contains before analyzing what the PR changes.**
