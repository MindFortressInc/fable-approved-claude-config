---
description: Standardized PR review with empirical validation (Deep Review Process v7.5)
argument-hint: <pr-url-or-number> [paired-pr-url-or-number]
---

# Deep Review Process v7.5

Standardized process for PR reviews with empirical validation. Calibrated over dozens of real reviews; the case references (PR #66, #101, #113/#80, …) are the actual incidents that produced each rule.

**v6.7 → v6.8:** structural-quality lens, adapted from Cursor's `thermo-nuclear-code-quality-review` skill. Adds: file-size threshold check (Step 1), structural regression detection prompt (Step 3), structural severity calibration + the code-judo question (Step 4), anti-nit-flooding publish rule (Step 8). Core stance: **structure informs findings; it never gates merges.** A missed simplification opportunity is a suggestion, not a blocker.

**v6.8 → v6.9:** evidence-required findings (no evidence ⇒ max MEDIUM), Step 3.5 adversarial refute pass on CRITICAL/HIGH, fixed severity decision table, evidence-based fix confirmation in re-review. Core stance: **review quality is model-independent — evidence and adversarial refutation replace reliance on LT judgment to recalibrate.**

**v6.9 → v7.0:** repo-invariant loader (Step 3), tenancy/scope-binding pass (Step 3), test-vacuity check (Step 5f). Core stance: **the process learns each repo's rules by reading that repo — it never hardcodes them.** Calibrated from an escape corpus of a few hundred bot findings (CodeRabbit/Greptile) on PRs that had already passed deep-review.

**v7.0 → v7.4:** generated-code exclusions, self-reported confidence with a consolidation threshold, absolute-line-number diff format, a zero-score drop-list and a Review Precedents ledger loaded into every refuter (v7.1); an AI-cheat mechanical pass (Step 1.5) plus two semantic detectors (v7.2); changed-surface enumeration (Step 1.6) plus a signature-compatibility lens (v7.3); ticket resolution, a ticket-compliance agent, its severity mapping and a publish block (v7.4).

**v7.4 → v7.5:** optional Codex cross-family second-opinion lane, **off by default** (Step 3), three-bucket divergence reporting at consolidation with agreement reported as a count, never re-rendered (Step 4), routing every Codex-only candidate through the same evidence-based confirmation as any other finding before it may be promoted (Step 5b), and an explicit publish gate (Step 8). Core stance: **a second model family's disagreement is signal, not a verdict — Codex's own findings are claims, and nothing from this lane reaches a PR comment, a tracker comment, or a memory write until Claude has independently confirmed it against the actual code.**

**Terminology:** "LT" = the lead/tech reviewer running this process (you, or the agent acting for you). Target branch examples use `develop`; substitute `main` if that's your trunk. The infra checklists reflect one real stack (FastAPI + Alembic + Celery + Docker + Vercel/Railway + Auth0/Stripe) — adapt the specifics to yours; the *categories* are the point.

**Arguments:** `$ARGUMENTS` — one or two PR identifiers (links or numbers). If two are given, treat as a paired multi-repo review (backend + frontend).

---

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

### Ticket resolution (v7.4)

Most PRs this pipeline reviews were generated **from** a tracker ticket (if your team keeps specs in the tracker, the ticket IS the spec), but nothing before this step ever reads that ticket back during review. Resolve it now so Step 3's ticket-compliance agent and Step 8's publish block have something to check the diff against.

**Resolution order — stop at the first match:**

1. **PR branch name** — e.g. `me/<prefix>-123-slug`.
2. **PR title.**
3. **PR body** — catches `Closes <TICKET-ID>` / `Fixes <TICKET-ID>` / a bare mention.

```bash
PREFIX=dev   # your tracker's ticket prefix
for src in "$BRANCH" "$TITLE" "$BODY"; do
  id=$(printf '%s' "$src" | grep -oiE "${PREFIX}-[0-9]+" | head -1)
  [ -n "$id" ] && { printf '%s\n' "$id" | tr '[:lower:]' '[:upper:]'; break; }
done
```

Prints the resolved `<TICKET-ID>` on the first match; prints nothing if none of the three sources carry one.

**Fetch the ticket** once resolved (e.g. `mcp__linear__get_issue {id: "<TICKET-ID>"}`). Its description is the compliance agent's input in Step 3.

**Graceful degradation (mandatory).** A PR with no resolvable ticket is common and legitimate (hotfixes, LT direct fixes, ops one-offs). **Skip the Step 3 ticket-compliance agent and the Step 8 publish block entirely; record one INFO line** in the review document ("no resolvable ticket — compliance check skipped"). **Never block a PR on missing ticket resolution.** The same degradation applies if the fetch fails or the ticket has no description.

---

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

### Classify depth

| Level | Criteria | Agents | Validation |
| -- | -- | -- | -- |
| **Deep** | Payments, auth, data sensitive, infrastructure, multi-PR features | 4 (security + quality × each PR) | Full: code + runtime + E2E + infra |
| **Standard** | UI features, incremental improvements, refactors | 2 (security + quality) | Code + runtime |
| **Quick** | Typos, docs, config, dependency bumps | 0 (direct review) | Code only |

### File-size threshold (mechanical, v6.8)

Check whether the PR pushes any file from under 1,000 lines to over:

```bash
for f in $(git diff --name-only origin/develop...HEAD); do
  before=$(git show origin/develop:"$f" 2>/dev/null | wc -l)
  after=$(git show HEAD:"$f" 2>/dev/null | wc -l)
  [ "$before" -lt 1000 ] && [ "$after" -ge 1000 ] && echo "CROSSED 1k: $f ($before → $after)"
done
```

Any crossing → record a MEDIUM structural finding: ask for decomposition (extract helpers, subcomponents, modules) or accept a one-line justification if the file remains clearly organized. Never block solely on this.

### Large PRs (>10K lines)

When `gh pr diff` fails with HTTP 406 ("diff exceeded maximum number of lines"), the GitHub API cannot serve the full diff. Fallback to local git:

```bash
# Full diff (may be very large)
git diff origin/develop...HEAD > /tmp/pr_full_diff.txt

# Focused diff on critical areas (preferred for agents)
git diff origin/develop...HEAD -- app/api/ app/routers/ app/services/ > /tmp/pr_critical_diff.txt
```

For agent prompts on large PRs, save focused diffs to temp files and include them in the prompt rather than passing the full 30K+ line diff. Split by area (routers, services, models, migrations) if needed.

### Check cross-PR conflicts

* Other open PRs targeting the same branch?
* Shared files at risk: migrations, config, schemas, shared services
* Migrations: do multiple PRs branch from the same `down_revision`?

### Generated-code exclusions (v7.1)

Machine-generated files get skipped by review agents — no human wrote them, and findings inside them are unactionable (the fix belongs in the generator, not the PR). Exclude by glob:

* **Protobuf:** `**/*_pb2.py`, `**/*.pb.go`
* **OpenAPI stubs:** `**/__generated__/**`, `**/openapi_client/**`
* **GraphQL codegen:** `**/*.generated.ts`, `**/*.graphql.ts`
* **gRPC:** `**/*_grpc.py`
* **Go generators:** `**/*_gen.go`
* **Build output and lockfiles:** autogenerated migration boilerplate (see exception below), `*.d.ts` build output, `.next/`, lockfiles (`package-lock.json`, `poetry.lock`, `uv.lock`, etc.)

**Excluded files are skipped by review agents but still counted** in the File-size threshold and Check cross-PR conflicts checks above — a generated file can still cross the 1K-line threshold or collide with another open PR.

**Migrations are a deliberate exception to this exclusion list — never skip them.** Step 5d carries a migration checklist; autogenerated or not, every migration gets reviewed. Exclude the boilerplate import/header noise around a migration, never the migration itself.

---

## 1.5. AI-Cheat Mechanical Pass (v7.2)

Test-vacuity (Step 5f) covers exactly one AI-cheat failure mode out of roughly a dozen ways an AI-authored diff fakes success. When agents author most of your PRs, these are your own failure modes, not hypotheticals — a diff that looks like a fix but isn't is exactly the shape a normal reviewer misses, because the code *reads* fine.

This step runs **11 structural detectors** mechanically — pattern checks over the diff, no LLM judgment, no sandbox — *before* Step 3's agents are even launched. Two companion **semantic** detectors (`goal-not-fixed`, `cheat-mock-mutation`) that need judgment rather than pattern-matching are folded into the Step 3 quality-reviewer prompt instead (see Step 3 § AI-cheat semantic pass); this step covers only the mechanical eleven. Ported from `moonrunnerkc/swarm-orchestrator`'s cheat-detector suite (303/325, 93.2% recall across 13 categories on their defect-injection oracle; 0.11 findings/PR false-alarm rate on an 18-PR pilot — the bar this pass should stay under; tighten a detector rather than ship it noisy).

**Non-goal:** detectors only, not a second gate. PRlaunch already gates; `swarm-orchestrator`'s sandbox-provisioning/restoration-proof gate mode is explicitly out of scope.

The table below IS the checklist: run each detector by reading the diff (`git diff --no-textconv origin/develop...HEAD`) for its pattern — a scripted version (`cheat-detect.sh`) is not published in this repo yet. Each detector covers both stacks in a single pass — Python (`except: pass`/`except Exception: pass`, `@patch`/`mock.patch` targets, `# type: ignore`, `# noqa`, `assert`/`assertEqual`/`assertTrue` counts) and TS/JS (`jest.mock`/`vi.mock`, `@ts-ignore`/`@ts-expect-error`/`@ts-nocheck`, `eslint-disable`, matcher swaps). `mock-of-hallucination` and `fake-refactor` additionally need `git grep`/`git ls-files` against the checked-out tree to confirm a symbol/module actually exists, so run them from inside the PR branch (the same assumption Step 1's file-size threshold check already makes).

| Detector | Fires on |
| -- | -- |
| `error-swallow` | Bare or comment-only `catch`/`except: pass` added in non-test code |
| `mock-of-hallucination` | `jest.mock`/`vi.mock`/`@patch` target that resolves to nothing in the repo |
| `no-op-fix` | Test changed with no reachable source change in the diff (or vice versa) |
| `fake-refactor` | Exported symbol renamed, but a caller elsewhere in the tree still references the old name |
| `coverage-erosion` | A branch is added with zero test files touched anywhere in the diff |
| `test-relaxation` | A strict matcher/assertion is swapped for a loose one, or a test block is deleted with no replacement in the same file |
| `assertion-strip` | Net assertion count in a test file drops |
| `type-suppression` | `@ts-ignore`/`@ts-expect-error`/`eslint-disable`/`# type: ignore`/`# noqa` added |
| `comment-only-fix` | Every changed line in a source file is a comment or blank |
| `exception-rethrow-lost-context` | A bare rethrow/`raise` is replaced by a new exception with no `cause`/`from` chaining |
| `dead-branch-insertion` | A branch guarded by a literal-false condition is added |

**Turning a hit into a finding:** each detector hit becomes a Step 3-style finding, using the standard required format (Step 3 § Required finding format):

* **Evidence:** the exact check run (the `git diff`/`git grep` invocation) plus the quoted diff hunk that matched. This is an *executed* check per the v6.9 evidence rule — it is never `UNVERIFIED`.
* **Confidence:** mechanical hits are deterministic; default `Confidence: 8` unless file-specific context makes the match doubtful (e.g. a `no-op-fix`/`coverage-erosion` hit on a file the PR's own description says is deliberately test-free).
* **Severity:** assigned via the *existing* Step 4 decision table — default MEDIUM (the same row as any other real, undemonstrated-CRITICAL/HIGH finding), escalating to HIGH only when Step 4's HIGH criteria are met, which for a cheat detector means the cheat masks a finding the PR itself claims to fix (e.g. an `error-swallow` hit swallowing the exact exception the PR's description says it now handles). This is not a new severity axis — do not invent one.
* A detector hit is a candidate, not an automatic finding: it still goes through the Step 3.5 refuter pass and the Review Precedents ledger like any other candidate before it survives to Step 4 consolidation.

---

## 1.6. Changed-Surface Enumeration (v7.3)

Diff-scoped review has a documented recall gap: independent 2026 benchmarking puts Greptile (which indexes the whole codebase before reviewing the diff) at 82% bug-catch vs CodeRabbit's 44%, and the canonical miss is "a function returns a new type that breaks three callers in files you didn't touch." Nothing in Steps 1-1.5 enumerates callers of a changed signature — this step does, mechanically, like the file-size threshold check above it.

**Noise budget is the constraint, not the reach.** The same benchmark that gives Greptile 82% recall charges it 11 false positives per run against CodeRabbit's 2 — reach without a filter imports that number. This step stays quiet by only ever asserting "broken" when it's mechanically certain (a renamed symbol's old name no longer resolves anywhere in its own file); everything short of that certainty is handed to Step 3's signature-compatibility lens as a candidate, never printed as a finding on its own.

**What to do** (a scripted version, `caller-enum.sh`, is not published in this repo yet — the recipe below is the manual equivalent): list every exported/public Python `def`/`class` or TS `export function/const/class/interface/type` whose definition line changed in the diff (conservatively: exactly one deleted def-line paired with exactly one added def-line in the same hunk), and split each into:

* **RENAME** — the name itself changed (`fake-refactor`'s case).
* **RETYPE** — the name is unchanged but the definition line differs (params, return type, etc. changed in place).

Before trusting a RENAME pair, confirm the old name is actually gone — it must not still resolve as a def/class/export anywhere in its own file. Without this, a same-file refactor (a helper extracted into a new function, with the original re-declared later in the same diff but a *different* hunk) reads as a false rename.

For each kept symbol (see cost bound below), enumerate every repo-wide reference and classify each call site:

```bash
git grep -n -w '<old-or-retyped-name>'
```

| Classification | Meaning | Output |
| -- | -- | -- |
| **Updated in this diff** | The call site's own line was touched by this diff | Not reported |
| **Unchanged, still compatible** | Symbol unchanged, or a comment/docstring mention, or (RETYPE) not call-shaped | Not reported |
| **Unchanged and now broken** | RENAME only: an untouched caller still references the name that no longer exists — mechanically certain | `<file>:<line>: caller-enum-broken: ...` — **a finding** |

RENAME classification is unambiguous by construction (the old identifier doesn't resolve anywhere in its own file after a real rename), so a `caller-enum-broken` line is a finding on its own — feed it through the standard finding format (Step 3 § Required finding format) with `Evidence:` set to the `git grep` invocation and hit line, `Confidence: 8` (deterministic, same rationale as Step 1.5's mechanical hits), and severity via the existing Step 4 decision table (default HIGH — broken functionality by definition; not a new severity axis).

**RETYPE candidates are not findings by themselves.** Same-name signature changes (return type, param count/type, nullability) need judgment a grep can't supply — record each unchanged, call-shaped reference to a retyped symbol as `<file>:<line>: caller-enum-candidate: ...` and hand it, verbatim, into the Step 3 quality-reviewer prompt for the signature-compatibility lens (below) to adjudicate. Only a candidate the lens confirms as actually broken becomes a HIGH finding at Step 4 — the same mechanical/semantic split Step 1.5 already established.

**Cost bound:** rank symbols by repo-wide call-site count and cap at the top 20; list the rest as `DROPPED (bounded to top 20 by call-site count)` in the review doc rather than silently truncating.

**Scope, deliberately narrowed:** Python/TS function, method, and class definitions only — the same categories `fake-refactor` already covers. Route paths, env vars, DB columns, and model fields are **not** covered by this mechanical pass; a grep-based detector for those produces matches too uncertain to classify mechanically and widens the noise budget — narrower-and-quiet over wider-and-noisy. Skip vendored/generated paths via the same list as Step 1's Generated-code exclusions — not a second list.

**Out of scope: cross-repo callers.** A backend change breaking a frontend caller (or vice versa) needs an index this step doesn't have — this only ever searches the current repo's working tree.

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

---

## 3. Static Analysis (parallel agents)

Launch agents in parallel with calibrated prompts.

### Severity criteria (include in every prompt)

* **CRITICAL** — Data loss, security breach, financial impact, user data exposure
* **HIGH** — Must fix before merge (broken functionality, auth bypass, payment errors)
* **MEDIUM** — Should fix (DRY, consistency, deprecated patterns, missing validation)
* **LOW** — Nice to have (naming, style, minor optimizations)
* **INFO** — Observations, no action needed

### Required finding format (v6.9, include in every prompt)

Every finding must carry an evidence line and a confidence score:

> `Evidence: <command run> → <output excerpt>`
> `Confidence: <0-10>`

The evidence must be an *executed* check — a grep hit, a `curl` response, a failing test, or a traced call path — not a claim from reading the diff. Instruct every agent:

* **A finding with no executed check cannot be CRITICAL or HIGH.** Auto-cap it at MEDIUM and tag it `UNVERIFIED`.
* An `UNVERIFIED` finding must be **verified or dropped during Step 5** (Empirical Validation) before it can be published — it never ships as-is.
* **Confidence (v7.1):** self-report `Confidence: <0-10>` — the agent's own certainty the finding is real and correctly scoped (10 = certain, 0 = pure speculation). This is a self-report, not a substitute for evidence — a HIGH-confidence claim with no executed check is still capped at MEDIUM by the `UNVERIFIED` rule above. Confidence exists so Step 4 consolidation can mechanically drop low-conviction noise (see Step 4).

### Repo invariants (v7.0, mandatory)

Before launching agents, load this repo's review invariants and paste them
**verbatim** into every agent prompt:

```bash
awk '/^## /{f=0} /^## Review Invariants/{f=1} f' CLAUDE.md
```

Also check `AGENTS.md` if the repo has one, and `.coderabbit.yaml`
`path_instructions` for path-scoped rules that apply to the changed files.

* If no `## Review Invariants` section exists, record a MEDIUM process finding
  ("repo has no Review Invariants section") and proceed with the generic lenses
  only.
* **Never invent repo-specific rules that aren't written down.** An unwritten
  convention is not a finding — it's a suggestion, and it belongs in a PR to
  CLAUDE.md, not in a review. This is what keeps the loader honest across repos
  the reviewer has never seen.
* When a reviewer (bot or human) later catches something these invariants should
  have caught, the fix is a line appended to CLAUDE.md, not a special case here.

### Agent types

* **Security reviewer** — Vulnerabilities, auth, data exposure, input validation, OWASP
* **Quality reviewer** — SOLID, DRY, code smells, test coverage, patterns
* **Ticket-compliance reviewer (v7.4)** — conditional: launched only when Step 0 resolved a ticket. See § Ticket-compliance agent below.

### Quality reviewer: detection prompts

Include these instructions in every quality reviewer prompt:

**Duplicate detection:**

> "For each function the PR modifies or calls, check whether an equivalent function exists in the same module or sibling modules that performs the same operation differently. Report divergences in behavior (e.g., one strips HTML before counting words, the other doesn't). Also flag any import from the API/route layer inside the service layer — services must never import from routes."

**Silent failure paths:**

> "Identify code paths that fail silently — producing incorrect results without raising errors. Examples: a file format accepted by UI but not processed correctly (falls through to wrong handler), an endpoint that exists in code but is never registered (returns 404 without any indication the code is dead), a feature flag that accepts a value but ignores it. These are higher priority than loud failures because they go undetected."

**Integration point registration:**

> "For each new file (router, component, hook, service), verify it is registered/imported at its entry point. Backend: new routers must be registered at the app entry point. Frontend: new file types in a dropzone/uploader must have a processing branch. New hooks must be imported where used. Flag any new file that appears unconnected to the application."

**Tenancy / scope-binding pass (v7.0):**

> "For every query or mutation the diff adds or modifies: identify the target
> model and enumerate the scope columns it actually declares (org_id,
> brand_id, tenant_id — whichever exist on that model). For each scope
> column the model carries, quote the line in this query that binds it. Report
> any unbound scope column as a finding. A parameter that is accepted by the
> function but never reaches the WHERE clause is the same finding as omitting it.
> This applies to counts, aggregates, and analytics queries, not only row reads.
>
> Severity HIGH when the query can return or mutate rows across a tenant or
> product boundary. Evidence line must quote both the model definition and the
> query."

**Structural regression (v6.8):**

> "Evaluate whether the diff degrades the structure of the code it touches. Flag: (1) new ad-hoc conditionals or special-case branches inserted into unrelated or shared flows; (2) one-off booleans, nullable modes, or flags that complicate existing control flow; (3) thin wrappers or identity abstractions that add indirection without buying clarity; (4) casts, `any`/`unknown`, or unnecessary optionality that obscure the real contract where a clearer type boundary could exist; (5) feature-specific logic leaking into general-purpose/shared modules, or logic placed in the wrong layer when a canonical home exists; (6) bespoke helpers that near-duplicate an existing canonical utility; (7) multi-step updates that can leave state half-applied when a more atomic structure is available, and unnecessary sequential orchestration of independent work. Report these as STRUCTURAL findings — they default to MEDIUM (consolidation recalibrates; see Step 4)."

**AI-cheat semantic pass (v7.2):** the 2 semantic categories from the Step 1.5 AI-cheat suite that need judgment rather than pattern-matching. Include both prompts alongside the structural-regression one above:

> "**goal-not-fixed** — Read the PR's own stated claim (title, description, linked ticket) and then the diff. Does the diff actually deliver that claim? A PR that says it fixes X but only touches comments, renames, an unrelated code path, or a path that can't reach the described bug has not delivered the claim — regardless of whether its tests pass. Answer explicitly `goal-fixed` or `goal-not-fixed`, and quote the specific hunk that either implements or fails to implement the claim."

> "**cheat-mock-mutation** — For every test the diff modifies or adds that the PR cites as covering its fix: check whether the test was made to pass by mutating the mock/stub/fixture to match the code's current (possibly still-wrong) output, rather than by fixing the code to produce the output the mock was originally asserting. Quote the mock/fixture edit next to the assertion it feeds, and state whether that assertion would still pass unchanged against the pre-fix implementation — if yes, this is a `cheat-mock-mutation` finding, not a real fix."

Both categories feed the same MEDIUM/HIGH handling as the Step 1.5 structural detectors (Step 1.5 § Turning a hit into a finding) — HIGH only when the cheat masks the PR's own claimed fix, per Step 4's existing decision table; this is not a new severity axis. `goal-not-fixed` composes with the ticket-compliance agent below (see § Ticket-compliance agent for which one runs).

**Signature-compatibility lens (v7.3):** semantic counterpart to Step 1.6's mechanical `caller-enum-candidate` output — judges whether a same-named, retyped symbol's *unchanged* callers are actually compatible with the new signature, the one classification Step 1.6 can't make mechanically. Include in every quality reviewer prompt, alongside the `caller-enum-candidate` lines Step 1.6 produced for this PR (paste them into the prompt verbatim — empty if Step 1.6 found none):

> "For each `caller-enum-candidate` line above, read the caller and the new definition. Would this specific call still work correctly? Check for: a changed return type the caller doesn't handle (e.g. now-nullable value stored in a non-null field, a wrapped/unwrapped result), a newly required parameter the call doesn't supply, a narrowed accepted parameter type the caller's argument doesn't satisfy, a renamed field the caller still accesses under the old name, or a nullability change the caller doesn't guard. Quote both the call site and the changed definition. Classify each candidate explicitly as `compatible` or `broken` — only `broken` is a finding, and only when you can point to the specific mismatch; when the call genuinely still works (e.g. the changed piece of the signature isn't reached by this particular call), classify it `compatible` and say why, rather than flagging it out of caution."

`broken` classifications feed the same Step 4 decision table as everything else — default HIGH, since a genuinely broken caller is broken functionality by definition; this is not a new severity axis. Cross-repo callers are out of scope for this lens too, for the same reason as Step 1.6.

### Ticket-compliance agent (v7.4)

**Conditional on Step 0's ticket resolution** — launches only when a ticket was resolved and its description fetched successfully. Skipped entirely (Step 0 § Graceful degradation) when no ticket resolves — no agent, no finding, no publish block.

Feed the agent the ticket's description (the Scope/Done-when section, if your team keeps specs in the tracker) plus the diff, formatted per Step 3 § Diff format for agent prompts like every other Step 3 prompt. Brief it:

> "Here is a ticket's description and the diff of a PR that claims to implement it. Restate every requirement, sub-task, DoD item, and acceptance criterion from the ticket in your own words — do not paraphrase away detail; a compound requirement ('X and Y') is two requirements, not one. Then, for each, decide from the diff alone whether it is fully delivered, not delivered, or cannot be judged from code (browser/UI testing, live infra, a human call). Separately, identify any code in the diff that no requirement in the ticket asked for."

Emit `The-PR-Agent/pr-agent`'s four-field schema (`pr_agent/tools/ticket_pr_compliance_check.py`) as the output shape:

* `ticket_requirements` — every requirement, sub-task, DoD item, and acceptance criterion, restated in the agent's own words.
* `fully_compliant_requirements` — which the diff fulfils, with `file:line` evidence.
* `not_compliant_requirements` — which it does not, noting whether the PR's own title/description/body claims to have delivered it (feeds the Step 4 severity mapping below).
* `requires_further_human_verification` — which cannot be judged from the diff alone.

Scope drift (code the diff adds that no requirement asked for) is reported alongside this schema, not folded into it — see Step 4 § Ticket-compliance severity mapping.

**Compose with `goal-not-fixed`, do not duplicate it (Step 3 § AI-cheat semantic pass).** `goal-not-fixed` asks "does the diff deliver the PR's own stated claim" (title/description altitude); this agent asks "does the diff deliver the TICKET's requirements" (spec altitude) — the same failure shape, judged from a different source of truth, and a fully overlapping one whenever a ticket resolves. **Single-prompt resolution:** when Step 0 resolves a ticket, run only the ticket-compliance agent and drop `goal-not-fixed` from that PR's quality-reviewer prompt — every `goal-not-fixed` failure is representable as a `not_compliant_requirement` the PR claims to deliver (the HIGH case below), so keeping both would double-report the same defect under two names. When no ticket resolves (Step 0 § Graceful degradation), `goal-not-fixed` runs alone, judging the claim from the PR's own title/description only.

### Multi-PR same-repo: branch contamination

When reviewing multiple PRs that target the same repo, the working tree can only be on one branch at a time. Agents launched in parallel will all read files from whichever branch is checked out, producing false positives for the other PRs.

**Rule: for multi-PR reviews in the same repo, either:**

1. **Include the diff in the agent prompt** instead of telling the agent to read the file (preferred — avoids branch dependency entirely)
2. **Sequence agent launches** — checkout PR A's branch, launch its agents, then checkout PR B's branch, launch those agents
3. **Tell each agent explicitly which branch to checkout** before reading files

Option 1 is strongly preferred because it eliminates the failure mode entirely and allows full parallel execution.

### Diff format for agent prompts (v7.1)

When Option 1 above calls for including diff text in a prompt instead of a repo pointer, prefer a diff whose lines carry **absolute** line numbers over raw `git diff` output. Raw unified diff carries only relative hunk offsets; agents that have to infer absolute line numbers from context-line counting get them wrong often enough that Step 5b exists specifically to catch it (see the backstop note there).

If you have a formatter that converts a diff to `The-PR-Agent/pr-agent`'s `__new hunk__`/`__old hunk__` form — every line on the new (post-change) side carries its absolute line number in the resulting file, `__old hunk__` included only when the hunk removed something — use it, so a finding's `file:line` can be read off directly instead of inferred. (A scripted formatter, `diff-format.sh`, is not published in this repo yet.) Otherwise pass the plain diff with extra context and tell agents to verify every line number against the file:

```bash
git diff --no-textconv -U5 origin/develop...HEAD -- <paths>
```

This applies wherever the process prefers diff text over a repo pointer: this rule (Option 1) and the Step 3.5 refuter prompts below.

**Two anti-hallucination rules — include in every agent prompt that receives diff text instead of the whole file:**

> "Note that you only see changed code segments, not the entire codebase. Avoid suggestions that might duplicate existing functionality or questioning code elements (like variable declarations or import statements) that may be defined elsewhere."

> "If the code ends at an opening brace or statement that begins a new scope (like 'if', 'for', 'try'), don't treat it as incomplete. Acknowledge the visible scope boundary and analyze only the code shown."

### Codex second-opinion lane (optional, OFF by default, v7.5)

A second, independent model family (Codex, via a `~/.claude/hooks/codex-review.sh` wrapper) can run **in parallel** with the Step 3 agents above, reviewing the same diff read-only and reporting its own `findings[]` as structured JSON (`{file, line, severity, claim, evidence}` per entry). It is **never blocking**.

**Off unless you turn it on.** The lane runs only when **both** `CODEX_REVIEW_ENABLED=1` is set **and** the wrapper is installed. This repo does not ship the wrapper or its result schema yet; without it, or with the flag unset, skip the lane — Step 3 and everything after runs exactly as before. Enablement lives in configuration, never in the tool: a wrapper must exit non-zero (no dispatch) unless `CODEX_REVIEW_ENABLED=1`, so it stays safe for anyone who runs it with a clean environment. If you export the flag from `settings.json`'s `env` block, remember every shell Claude Code spawns inherits it — a test that needs the disabled path must unset it explicitly.

**Model is a config value.** `CODEX_REVIEW_MODEL` selects it; unset means the Codex account default (no `-m` flag). There is no built-in default model id. Verify flag behaviour (e.g. `--output-schema`) per model and per surface; never assume it carries over from another model.

**Review turns per window are the constraint.** A subscription-backed Codex has message-windowed rate limits; the resource that runs out is review turns per window, not dollars. When the window is spent, switch `CODEX_REVIEW_MODEL` or skip the lane with `CODEX_REVIEW_ENABLED=0`. An unreachable model at review time is a non-zero exit like any other, and the review proceeds without the lane.

**Mechanism:** dispatch plain `codex exec` with the review instructions folded into an inline prompt — **not** `codex exec review`. The `review` subcommand was proven live to ignore `--output-schema` entirely, even on a valid schema, and always returns freeform prose; it can never produce a structured result. Plain `codex exec <prompt>` does honor `--output-schema`.

**Run it** (with the wrapper installed):

```bash
CODEX_REVIEW_ENABLED=1 ~/.claude/hooks/codex-review.sh run \
  --output /tmp/codex-review-<pr-id>.json \
  --base origin/develop
```

* **Any non-zero exit means "no Codex findings this run" — never a reason to stop the review.** A Codex outage, an expired login, a refused/unentitled model, a malformed dispatch, a schema-validation failure, or the lane being disabled are none of them the review's problem. Log which one happened (for the internal review doc only) and proceed to Step 3.5/4 as if the lane had never run.
* **The diff target is `--base origin/develop`**, matching this process's own `origin/develop...HEAD` convention. Plain `codex exec` has no `--base` flag, so the wrapper builds the exact git command itself — `git diff --no-textconv <ref>...HEAD`, three-dot — and states it explicitly in the prompt for Codex to run.
* Record the resolved model next to the findings (e.g. an `<output>.meta.json` sidecar), so you can always say which model produced each finding.
* Read-only is not a knob — always dispatch `-s read-only`, never `--dangerously-bypass-approvals-and-sandbox`. A second-opinion reviewer does not get write access, full stop.
* Instruct Codex to use `python3`, never plain `python`, for anything it decides to run itself — Codex's own sandbox may have only `python3` on `PATH` (observed live: a Codex-run command failed with `command not found: python` before this instruction was added).

**What comes out of this step is a candidate findings file, not findings.** Codex's `findings[]` are its own unverified claims about the code — see § Codex-origin findings: publish gate (Step 8) for why none of them may surface anywhere until Step 5b has confirmed them. This step only produces the file; Step 4 is where it gets reconciled against Claude's own consolidated findings.

---

## 3.5 Adversarial refute pass (before consolidation)

For every **CRITICAL/HIGH candidate** produced in Step 3, spawn an independent **refuter** subagent whose job is to *disprove* the finding, not confirm it. Brief it:

> "Here is the diff + the claimed defect. Try to prove it wrong: a guard exists upstream, the path is unreachable, the framework handles it, a test covers it. Default to REFUTED if you cannot demonstrate the failure."

* **Include the diff text in the refuter prompt, not a repo pointer** — consistent with the multi-PR branch-contamination rule (Step 3, Option 1). This eliminates branch-dependency and lets refuters run in parallel.
* **Deep tier:** 2 refuters per candidate; the finding survives only if **≤1** refutes it.
* **Standard tier:** 1 refuter per candidate.
* **Refuted → downgrade to INFO**, with the refutation recorded in the disposition list. A refuted finding is **never silently dropped** — the reasoning that killed it is written down.

**Cost bound:** refuters run *only* on CRITICAL/HIGH candidates, typically 0–5 per PR — so this adds a bounded, small number of subagent calls, not a per-finding tax.

### Diff format for refuter prompts (v7.1)

Format the diff text required by the "include the diff text" rule above per Step 3 § Diff format for agent prompts before it goes into the refuter prompt. Absolute line numbers let the refuter check a claimed `file:line` against the visible hunk directly instead of re-deriving it, and the same two anti-hallucination rules apply here — a refuter that doesn't see the whole file must not flag out-of-view definitions or truncated-looking scope boundaries as defects in their own right.

### Precedent ledger loader (v7.1)

Before spawning refuters, load this file's own `## Review Precedents` section (bottom of this document) and paste it **verbatim** into every refuter prompt — same mechanism the repo-invariant loader (Step 3 § Repo invariants) uses for `## Review Invariants`, except the source is this file itself, not the target repo's `CLAUDE.md`:

```bash
awk '/^## /{f=0} /^## Review Precedents/{f=1} f' ~/.claude/commands/deep-review.md
```

Give the refuter this explicit instruction:

> "Check the candidate finding against the precedent list above before evaluating it fresh. If it matches a precedent's fact pattern, cite the precedent number and quote its disposition instead of re-deriving the argument from scratch. **A precedent is rebuttable, not a blanket exclusion:** a finding backed by an *executed* check demonstrating the failure beats a precedent — exactly as LT judgment may move severity down but never up without evidence (Step 4 § Severity decision table). If the candidate's fact pattern differs from the precedent's in a way that matters (e.g., it's a genuinely new tenancy-scoping gap, not the one specific documented mismatch a precedent covers), say so explicitly and evaluate it fresh."

This turns the refuter from "reasons every candidate from scratch" into "checks the ledger first, cites it when it fits, and only re-derives an argument when no precedent applies or the precedent is rebutted by evidence."

---

## 4. Consolidation

* Deduplicate findings across agents (same issue from security + quality → one finding)
* Assign unified IDs: `B1-Bn` (backend), `F1-Fn` (frontend)
* **Enforce the evidence-line format** — the consolidator rejects any finding lacking `Evidence: <command run> → <output excerpt>`. A finding still tagged `UNVERIFIED` (no executed check) is capped at MEDIUM here and must be verified or dropped in Step 5 before publishing.
* **Enforce the confidence threshold (v7.1)** — drop any finding scoring **below 7** on its self-reported `Confidence: <0-10>`. Evidence outranks self-reported confidence: a finding that carries executed evidence (per the evidence-line rule above) is never dropped by this rule, regardless of its confidence score.
* **Assign severity via the decision table below** — not free-form judgment.
* Incorporate context from existing PR comments (don't duplicate what others found)
* Generate consolidated document with consistent format

### Severity decision table (v6.9)

Severity is assigned mechanically from this table, not from agent-reported labels (agents consistently inflate code-quality issues to BLOCKING/HIGH):

| Severity | Criteria |
| -- | -- |
| **CRITICAL** | Demonstrated (evidence) data loss, security breach, or financial impact. **Never structural.** |
| **HIGH** | Demonstrated broken user-facing functionality, an auth/payment path defect, or a silent-corruption path with no downstream catch. |
| **MEDIUM** | Everything real that isn't demonstrated CRITICAL/HIGH (including all default structural findings, per v6.8). |
| **LOW / INFO** | Style, naming, missed simplification (code-judo stays INFO). |

**Hard rules:**

* **No evidence ⇒ max MEDIUM** (the `UNVERIFIED` cap from Step 3).
* **A downstream catch exists ⇒ drop one level** (this absorbs principle #7, technical severity ≠ impact severity).
* **LT judgment may move severities DOWN, never UP without evidence.** Escalating a finding above MEDIUM requires an executed check demonstrating the failure.

### Ticket-compliance severity mapping (v7.4)

Feeds the table above — not a parallel severity axis. Applies only to Step 3's ticket-compliance agent output:

| Compliance verdict | Severity | Action |
| -- | -- | -- |
| `not_compliant_requirement` the PR's own title/description/body **claims** to deliver | **HIGH** | The PR is not what it says it is — same row as any other demonstrated broken-functionality finding. Evidence: quote the claim next to the missing/wrong code. |
| `not_compliant_requirement` the PR is **silent** on | **MEDIUM** | File a follow-up ticket under the source ticket's epic (a nit/ops follow-up on the feature being shipped; the epic stays open until it's done). Never blocks. |
| `requires_further_human_verification` item | **Surfaced, never a severity, never blocking** | No Step 5 recourse — it genuinely can't be settled from code. Stays a standing note for the PR owner/LT. |
| Scope drift — code the diff adds that no ticket requirement asked for | **INFO**, escalate to **MEDIUM** only if it touches auth/payments/data | Catches an agent quietly widening its own mandate. Evidence: the ticket requirement list plus the diff hunk with no corresponding requirement. |

### Classify finding origin

For each finding, classify its origin. This determines the expected action:

| Origin | Definition | Action |
| -- | -- | -- |
| **IN-SCOPE** | Bug or issue introduced by this PR's code changes | Must fix if MEDIUM+. This is the PR author's responsibility. |
| **ADJACENT** | Pre-existing issue in a file the PR touches | Fix opportunistically if trivial (< 5 lines, no risk). Otherwise track as follow-up. |
| **OUT-OF-SCOPE** | Pre-existing issue in a file the PR does NOT touch | Track as a separate issue. Never block the PR for this. |

**Why this matters:** in one review, the most impactful finding (XSS via `javascript:` URIs) was in a file the PR didn't modify. Without origin classification, it's tempting to either block the PR unfairly or ignore the finding entirely. The correct action: fix it opportunistically since the PR touched adjacent components, but don't hold the PR hostage for pre-existing debt.

### Codex second-opinion divergence (v7.5)

Only runs when Step 3's Codex lane actually produced a result file (§ Codex second-opinion lane) — skip this whole section when the lane is off, the wrapper isn't installed, or it exited non-zero, exactly as instructed there.

**Consolidate Claude's own findings first** (the bullets above, through Step 3.5's refute pass), as its own JSON array in the same shape the lane's output uses (`{file, line, severity, claim, evidence}` per entry — so no translation step is needed). Then run the three-bucket join (with the wrapper installed, its dispatch-free `buckets` subcommand does it):

```bash
~/.claude/hooks/codex-review.sh buckets <claude-consolidated-findings.json> <codex-output.json>
# -> {"agreed_count": N, "claude_only": [...], "codex_only": [...]}
```

The join is on **`(file, line)` with a small tolerance — never on claim text**: two model families paraphrase the same defect differently, and matching on wording would manufacture divergence that isn't real.

* **`agreed_count`** — report this as a single number in the internal review doc ("Codex independently corroborated N of Claude's own findings"). **Never re-render the agreed set finding-by-finding** — it is corroboration, not new information, and re-listing it is noise.
* **`claude_only`** — no change to this process. These are already real, already-consolidated findings; they proceed through Steps 4/5 exactly as if the Codex lane had never run.
* **`codex_only` is the entire product of this lane.** Every entry here is a claim Claude's own agents did NOT independently make. **None of them are findings yet** — each one is a fresh candidate that must go through Step 5b's confirmation (file:line verified against actual source, CONFIRMED/PARTIALLY CONFIRMED/NOT CONFIRMED) and the Step 3 evidence/severity rules exactly like a brand-new agent finding, before it may be assigned an origin (§ Classify finding origin, above) and merged into the unified findings list. A `codex_only` candidate that fails confirmation is dropped — noted in the internal doc as "Codex claimed X, not confirmed: <why>", never published anywhere (§ Codex-origin findings: publish gate, Step 8).

### Structural severity calibration (v6.8)

Structural findings (spaghetti growth, thin wrappers, boundary muddying, layer leaks, file-size crossings) follow stricter severity rules than functional ones:

* Default severity: **MEDIUM**.
* **HIGH** only when the PR materially tangles a *shared* path AND the cleaner structure is obvious and scoped (the fix doesn't require a redesign).
* **Never CRITICAL.**
* A *missed simplification opportunity* — code that works but could be dramatically simpler — is a **suggestion (INFO)**, never a blocker. Don't gut working complexity; recommend the reframing and move on.

### The code-judo question (v6.8)

After consolidating findings, ask once, explicitly: **is there a reframing of this change that deletes whole branches, helpers, modes, or layers while preserving behavior?** Refactors that move complexity around without reducing the number of concepts a reader must hold are the target. If a reframing exists, record it as an IN-SCOPE suggestion (INFO) with a concrete sketch of the simpler shape. This question informs the review — it does not gate the merge.

---

## 5. Empirical Validation

### 5a. Setup

* Branch should already be rebased from Step 2
* **Backend:** `docker compose up -d --build` or your dev server — verify health endpoint responds
  * **All backend commands (linting, tests, imports) run inside Docker.** Never install dependencies locally. Use: `docker compose run --rm --no-deps app bash -c "..."`
* **Frontend:** `npm run dev` — verify dev server compiles and serves

### 5b. Code validation

* Verify each finding against actual source: file:line must match
* Correct agent imprecisions (wrong line numbers, incorrect code snippets)
* Mark: CONFIRMED / PARTIALLY CONFIRMED / NOT CONFIRMED
* **Backstop, not a crutch (v7.1):** Step 3/3.5 diff text should now carry absolute line numbers (Step 3 § Diff format for agent prompts), so line-number drift should be rarer — but this check stays mandatory. The improved format reduces the error rate; it does not license skipping the correction step above.
* **Codex-origin candidates are not exempt (v7.5):** every `codex_only` entry from § Codex second-opinion divergence (Step 4) goes through this exact same check — file:line verified against the actual source, CONFIRMED/PARTIALLY CONFIRMED/NOT CONFIRMED — before it is anything more than a claim. Codex's own `evidence` field is Codex's self-report, not Claude's verification; it does not substitute for actually reading the code at that file:line. **A Codex finding is a claim, not evidence, until this step confirms it.**

### 5c. Runtime validation

* **API endpoints:** `curl` to test auth, error handling, status codes, edge cases
* **Frontend UI:** Playwright (or similar) to test navigation, auth bypass, rendering, user flows
* **Concurrency:** Parallel requests to test blocking, race conditions, idempotency
* **Failure scenarios:** Missing env vars, service down, invalid input
* **Happy path E2E** (if applicable): Full user flow from start to finish
* **Security fixes:** Validate the vulnerability is closed with before/after comparison (e.g., endpoint returned 200 before fix, returns 401 after). Test against a running instance, not just code reading. Lesson: a header-trust auth bypass (`X-User-ID` honored without verification) was in one codebase for months — only confirmed exploitable when tested against production with `curl`.
* **Migration fixes:** Validate from scratch (`docker compose down -v && docker compose up -d --build`) — not just incremental migration on existing DB.

### 5d. Infrastructure validation

(Example stack — adapt the specifics; keep the categories.)

#### Migrations (Alembic or equivalent)

- [ ] `down_revision` doesn't conflict with other open PRs
- [ ] Both `upgrade()` and `downgrade()` exist and are correct
- [ ] Column changes are safe: `nullable=True` for new columns (no table lock)
- [ ] `DateTime(timezone=True)` if storing timezone-aware values
- [ ] No data migration needed (or included if needed)
- [ ] Auto-migration on startup won't break

#### Docker / Deployment

- [ ] `docker-compose.yml` changes don't break existing services
- [ ] New services have health checks
- [ ] `Procfile` updated if new process types added
- [ ] Volume mounts don't expose sensitive paths

#### Environment & Config

- [ ] New env vars added to `.env.example` / `.env.local`
- [ ] New env vars added to the production environment
- [ ] Production settings validation updated if new required vars
- [ ] Startup fails fast if critical config is missing (not runtime error)
- [ ] No secrets hardcoded in code

#### Integration Point Registration

- [ ] New backend routers registered at the app entry point
- [ ] New frontend file types in uploaders have processing branches (not just MIME acceptance)
- [ ] New background tasks registered in the task runner's discovery list
- [ ] New React hooks/contexts imported and used where intended
- [ ] No dead code: every new file is reachable from the application entry point

#### Rate Limiting

- [ ] New endpoints have appropriate rate-limit decorators
- [ ] AI-backed endpoints: 5-30/hour depending on cost
- [ ] Auth endpoints: 10/minute
- [ ] Read endpoints: 60-120/minute

#### Background Tasks (Celery or equivalent)

- [ ] New tasks registered in the autodiscover list
- [ ] Tasks have appropriate time limits
- [ ] Task idempotency handled (can retry safely)
- [ ] Schedule updated if periodic task added

#### Database

- [ ] No raw SQL — use the ORM with bound parameters
- [ ] Session management via the standard dependency
- [ ] No N+1 queries introduced
- [ ] Transactions committed explicitly where needed
- [ ] Pool settings appropriate (no connection leaks)

#### External Services

- [ ] Payment provider: products/prices exist in target environment
- [ ] Auth provider: scopes/permissions configured
- [ ] API keys present in all environments
- [ ] Webhook endpoints handle errors correctly (re-raise for retry)

### 5e. Update document

* Mark each finding's validation status
* Add evidence table with method and result

### 5f. Test-vacuity check (v7.0)

A test that passes against the un-fixed implementation proves nothing. For every
test the PR adds or modifies that is cited as covering a finding or the PR's
stated behavior:

1. Revert the implementation under test — `git stash` the source hunk, or
   monkeypatch the previous return value back in.
2. Re-run that test alone.
3. It MUST fail. Quote the failure output.

- Test still green against the reverted implementation → **HIGH finding**
  ("vacuous test"). The behavior it claims to cover is untested, and the PR does
  not pass this gate on that behavior.
- Cannot revert cleanly (pure addition, no prior implementation) → negate the
  test's key assertion input instead, confirm it fails, and record that.
- Keep a known-good control: one deliberately wrong mode that DOES fail, proving
  the rig itself works.

Two failure shapes seen in practice, both of which look like passing tests:

- The test sets up a state the production code cannot actually produce (e.g. a
  fixture writing an org_id the provisioner would never write). Testing an
  impossible state proves nothing.
- The fixed and broken implementations answer identically on the path the test
  exercises; the fix is only observable on a path no test covers.

Origin: a cross-tenant isolation test was written three times and passed against
the bug each time. Round 1 was the impossible-state shape; round 3 was the
identical-answer shape.

---

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

**Hard rule: no finding that originated from the Codex second-opinion lane may reach a GitHub/PR comment, a tracker comment, or a memory write until Step 5b has independently confirmed it against the actual code.** Codex's self-report is a claim, not evidence — treat it with exactly the confirmation standard this process already applies to its own agents' UNVERIFIED findings, never less.

In practice this is automatic, not a step you have to remember: § Codex second-opinion divergence (Step 4) never adds a `codex_only` candidate to the unified findings list in the first place — Step 5b promotes it in, or it is dropped. By the time this Publish step runs, every finding on the list (Codex-origin or not) has already cleared the same bar. If you find yourself about to paste a `codex_only` entry straight into a review comment or a tracker note without it having gone through Step 5b first, stop — that is exactly the shortcut this rule exists to block.

When a Codex-origin finding IS published (i.e., it was confirmed and promoted), attribute it plainly — "(cross-checked via Codex second-opinion lane, confirmed)" next to the finding — so the PR owner and any later reader know a second model family caught it, and so you have a clean signal of which lane actually surfaced which defect.

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

---

## Principles

### On judgment

1. **Context before action** — before fixing anything, understand why it's broken. If CI is red on a file the PR doesn't touch, investigate who introduced it and whether a fix already exists. The cost of investigating for 2 minutes is always lower than the cost of writing a redundant fix that creates a merge conflict later.
2. **The process is a framework, not a script** — follow the steps, but apply judgment at every one. If a step doesn't make sense for the situation, adapt. A reviewer who follows the process blindly and misses an obvious rebase gap is less effective than one who skips a step but catches the right problem. No checklist replaces engineering intuition.
3. **Exhaustive is not the same as effective** — finding 10 issues with 0 false positives sounds thorough, but if one of those fixes is redundant because you didn't check develop first, the thoroughness was misallocated. Prioritize: state of the branch > analysis of the code > volume of findings.

### On evidence

4. **Evidence over opinion** — a finding without proof is not a finding
5. **Validate real, not theoretical** — run the app, test the endpoints, prove it
6. **Calibrate severity** — DRY in tests ≠ security in payments
7. **Technical severity ≠ impact severity** — evaluate the full error path, not just the point of failure. A MEDIUM code smell is non-blocking if the downstream system catches the failure (e.g., a fragile client-side race condition where the backend enforces auth anyway). Conversely, a LOW-looking silent failure can be HIGH if nothing downstream catches it.

### On collaboration

 8. **Don't duplicate** — check existing reviews/comments before posting
 9. **LT reviews before publishing** — tone and focus matter
10. **Owner fixes first, LT fixes last** — default: send back to owner. Exception: residual non-blocking items where LT direct fix (Step 10) is faster than another round-trip

### On safety

11. **Cross-PR awareness** — PRs don't exist in isolation; check for conflicts and CI contamination from merged branches
12. **Green CI before approval** — never approve with failing checks, even if the failure isn't from the PR's own code
13. **Rebase before review** — always analyze code that reflects the actual state of the target branch. A review against stale code is a review of something that won't exist after merge.
14. **Silent failures over loud ones** — a 500 error is better than a 200 with corrupt data. Prioritize finding code paths that appear to work but produce wrong results: formats accepted but not processed, endpoints defined but not registered, features enabled but ignored. These go undetected in testing and production.
15. **Multi-repo discipline** — every `git`/`gh` command targets a specific repo. In paired reviews, never assume the shell is in the right directory. Verify before executing. A command that succeeds silently against the wrong repo is worse than one that fails loudly.

---

## Review Precedents (v7.1)

A codified ledger of rulings on cases already dispositioned once — so Step 3.5 refuters cite a precedent instead of re-deriving the same argument every run. Loaded verbatim into every refuter prompt (Step 3.5 § Precedent ledger loader).

**Rebuttable, not a blanket exclusion.** Every precedent below can be overridden by a finding backed by an *executed* check demonstrating the failure — this mirrors the existing rule that LT judgment may move severity down but never up without evidence (Step 4 § Severity decision table, "LT judgment may move severities DOWN, never UP without evidence"). A precedent narrows *where the burden of proof sits*; it never removes the burden. Cases 1 and 15 below are examples of a precedent doing real, evidenced work — not a rubber stamp.

**These are OUR rulings — replace them with yours.** 1–15 are seeded from our own corpus: each is traceable to a real disposition on a real PR in our services, written up here generically. They illustrate the *shape* of a good precedent (a narrow fact pattern, the reason it isn't a defect, the evidence that settled it); the specific facts are about our codebase, not yours. Start your own ledger from your own dispositioned false positives. 16–20 are curated adoptions from `anthropics/claude-code-security-review`'s precedent list, reviewed against our stack and corpus before inclusion. Rejected candidates are recorded separately below the numbered list, unnumbered, so they are never mistaken for active precedents.

### Corpus-derived (our own dispositions)

Sources: an escape corpus of ~300 CodeRabbit/Greptile/review-bot comments across ~90 PRs in three of our repos, cross-referenced against the actual PR review threads for full disposition text, plus the local CodeRabbit CLI review store as corroborating context. Each disposition below is a recorded "false positive" reply on the PR (and, in one case, CodeRabbit's own explicit withdrawal after human confirmation) — not inference from unresolved-thread counts.

**1. A documented mixed-representation tenant key is not automatically a tenancy gap.**
In one domain of ours, a tenant-scoping column carries either an issued UUID or a raw slug, depending on which writer created the row. Naively filtering or joining across both representations *drops legitimate rows* rather than adding tenant safety. Before flagging such a param as "accepted but unused" or a query as "ignoring the tenant key", check whether the target model's docstring documents this deliberate scoping.
*Disposition:* threading the key into the lookup would have missed every row created by the slug-writing path and broken a lifecycle mirror; the mismatch was documented as deliberate in the getter's docstring.
**Scope warning — read before applying:** this precedent covers exactly one documented representation mismatch. It is NOT a general license to wave off tenancy-scoping findings. Our own escape corpus found TENANCY-SCOPE to be the single largest and most severe escape category (36 of 246 findings, 28 of them MAJOR+) — a refuter must still verify the specific mixed-representation fact applies before citing this precedent, not pattern-match on the column name alone.

**2. An opt-out enforced by a different documented mechanism is not a missing consent store.**
Our consent ledger is deliberately keyed on email/phone only; opt-out on a third channel is enforced via a reply hard-stop. A "missing channel-keyed consent store" finding restates a known, separately-tracked scope boundary — not a new compliance gap.
*Disposition:* the channel opt-out is enforced at the hard-stop, and the email-keyed check applies whenever an email is known.

**3. A per-call sync on a flag-gated shadow surface is a deliberate observability mechanism, not a hot-path inefficiency.**
A per-call re-sync of a few rows that materializes disabled-bucket rows as real traffic exercises them, on a temporary flag-gated shadow surface invoked from a fire-and-forget background task, is off the interactive hot path.
*Disposition:* a per-process guard would add cross-worker staleness and test-state complexity for a path that is removed at cutover.

**4. Best-effort writes on an ingress path don't need a durable inbox/outbox wrapper.**
A write documented as best-effort (it must never break the ingress) and already idempotent per inbound message id is not a data-integrity gap for lacking a durable outbox — that pattern is a substrate-level change tracked separately.

**5. A tracked open design fork is not a new defect.**
Suppression/consent scoping deliberately keyed per product; resolving it across a parent/child identity boundary was an open, already-tracked design decision. Opt-outs were fully honored on the sending identity.

**6. Provision-time template tokens can never reach send-time render unresolved — only send-time tokens need a null-check.**
Brand tokens baked at provision time with guaranteed non-empty fallbacks are not send-time tokens. A blanket "unresolved template variable" finding must distinguish the two token classes before flagging.

**7. A test helper with exactly one call site is not a coverage gap for a hypothetical second one.**
Parametrizing a fixture helper against a call site that doesn't yet exist is speculative generality. Flag it only once a second call site actually exists.

**8. A branch-count lint on a script that deliberately mirrors a sibling script's structure is not a refactor finding.**
When high branch count exists specifically because the function mirrors a sibling script kept side-by-side for reviewability, a dispatch-table refactor that would diverge the pair is a structural regression, not an improvement — even when a linter flags the count. Applies only when the mirroring is real and the linter is non-gating.

**9. CI action-tag pinning is a repo-wide policy change, never introduced piecemeal by one PR.**
A new CI job's unpinned action tags that mirror an existing job's convention in the same workflow are not in-scope for that PR — SHA-pinning, if adopted, applies to all jobs at once.

**10. Co-located Next.js API routes are exempt from a shared backend-fetch wrapper.**
A backend-fetch wrapper exists to reach the API backend with a bearer token and tenant headers. A same-origin `fetch("/api/...")` to a co-located Next.js API route is a different request shape — routing it through the backend wrapper would incorrectly attach backend auth to an anonymous same-origin request. (Established across ~15 files in our frontend.)

**11. Reusing an already-shipped domain client is consistency, not a violation of a single-API-client rule.**
Importing from a domain client that sibling components and their tests already use is reuse, not a new client. A "route through the shared API client" rule targets net-new fetch call sites, not reuse of an existing adopted module.

**12. TS2686 does not fire on type-only React namespace references under a modern toolchain.**
Under `@types/react` 19 + tsc 5.9, `React.ComponentType`/`React.ReactNode` used in **type position** resolve via the global namespace and do not trip the "UMD global" TS2686 error — TS2686 only fires for **value-position** UMD-global use (confirmed via an actual tsc repro). A "missing React import" finding on a type-only reference is a false positive on that toolchain — re-check yours.

**13. A shared card-styling convention overrides a single-tab theming nitpick.**
When a className combination is the verbatim shared convention across every card of a page family and is already theme-aware via CSS vars, fixing a "contrast" finding in a single tab would create page-wide inconsistency — the concern, if valid, is page-wide scope, not a single-tab patch.

**14. Server-only pages have a documented exception to a browser-only API client.**
A browser-only client (token minting, `localStorage`, hardcoded `cache: "no-store"`) cannot serve a server-only anonymous read and would defeat a route's bounded-ISR caching design. A server-only domain client that follows an existing server-only precedent is the correct exception, not a violation of the single-API-client rule.

**15. Non-transactional migration steps stay out of the shared migration engine — withdrawn by CodeRabbit itself after evidence.**
Our migration runner wraps every migration file in one `BEGIN`/`COMMIT`, specifically so the migrations-table claim-insert and the migration body commit atomically (race-safe concurrent-boot claiming). `CREATE INDEX CONCURRENTLY` cannot run inside a transaction; asking for it is asking for a new non-transactional migration pathway — a broad, risky engine change, out of scope for a single migration PR. Every prior index-creating migration in that repo used plain `CREATE INDEX`, zero `CONCURRENTLY`. This is the strongest-evidenced precedent in the ledger: after a human reply with the engine trace and the all-plain-index precedent, **CodeRabbit withdrew its own finding.**
*Evidence (CodeRabbit, after human confirmation — self-withdrawal):* "agreed. The migration transaction provides a necessary concurrent-boot invariant, and introducing a non-transactional path solely for these indexes would be a disproportionate, out-of-scope engine change. Given the established plain-index precedent and the all-NULL initial columns, this is intended and acceptable. Withdrawing the finding."

### Adopted (curated from external precedent)

Source: `anthropics/claude-code-security-review`'s precedent list (`.claude/commands/security-review.md`). Only the 5 entries below survive review against our own threat model and corpus — this is **not** a port of their full 12-item list (e.g., their UUID-unguessable precedent is deliberately omitted: it never came up in scope discussion and isn't corroborated by our corpus, so it is neither adopted nor rejected — just not carried over).

**16. React/Angular auto-escape interpolated content — do not report XSS without an explicit escape-hatch API in the diff.**
React and Angular auto-escape bound/interpolated content by default. Do not report XSS in a React component unless it uses `dangerouslySetInnerHTML` on unsanitized input, or in an Angular component unless it uses `[innerHTML]`/`bypassSecurityTrust*` on unsanitized input.
*Case:* adopted from `anthropics/claude-code-security-review`, reviewed against a React 19 + Next.js stack. Corroboration: the one XSS-tagged finding in our corpus flags a markdown renderer resolving attacker-controlled link URLs — exactly the class this precedent does *not* excuse, since it's not plain JSX interpolation.

**17. Client-side authorization checks are UX, not the security boundary.**
A lack of permission/role checking in client-side JS/TS is not a vulnerability by itself — the backend must independently validate every input and enforce every authorization decision. If the backend trusts the client-side check, flag the *backend* endpoint, not the frontend.
*Case:* adopted from `anthropics/claude-code-security-review`; consistent with a server-side tenant-dependency pattern that enforces authorization regardless of what the frontend renders or hides.

**18. Environment variables and CLI flags are trusted, operator-controlled input.**
In a deployment model where env vars and CLI flags are set by operators and CI-managed secrets, they are operator-controlled, not attacker-controlled — do not report them as an injection or tampering vector.
*Case:* adopted from `anthropics/claude-code-security-review`. Corroboration: every `env_var`-tagged finding in our CR-CLI store is about *correctness* (missed `os.getenv` read forms, multi-line `os.environ` scan gaps, unclear `KeyError`s on missing config) — none treat env vars as attacker-controlled.

**19. Markdown/docs-only findings are capped below the blocking gate.**
A finding whose only content is a prose/documentation change in a `.md` file (no executable code, no runtime-consumed config) is not a code-defect finding. Route a substantive documentation error as a MEDIUM "docs accuracy" note, not CRITICAL/HIGH.
*Case:* adopted from `anthropics/claude-code-security-review`'s markdown-file exclusion. Corroboration: our CR-CLI store's ~400 `markdown`-tagged findings are overwhelmingly style nitpicks (fenced-code language tags, internal narrative consistency).

**20. Test-only files get a relaxed severity cap, never a waiver.**
A finding whose only impact is inside a `test_*`/`*.test.ts`/`conftest.py` file (never shipped to production) is capped at MEDIUM even when the equivalent finding in production code would be HIGH. **This caps severity — it does not exclude the finding.** A test that doesn't actually exercise its own claim is exactly the TEST-VACUITY class Step 5f exists to catch (14 of the 246 escapes in our corpus were TEST-VACUITY).
*Case:* adopted from `anthropics/claude-code-security-review`'s test-only-file exclusion, deliberately narrowed against our own test-vacuity lesson before inclusion — do not read this precedent as license to skip test files.

### Rejected — do not re-add

Anthropic's list carries two more exclusions we explicitly reviewed and declined. Recorded here so a future reader doesn't "helpfully" re-add them.

* **Rate-limiting exclusion — REJECTED.** Anthropic's list excludes rate-limiting findings by default. Step 5d explicitly *requires* rate-limit decorators on new endpoints — excluding rate-limiting findings would directly contradict this process. Our own corpus corroborates the rejection: it carries a real, actionable MAJOR rate-limit finding (an SSE limiter keyed on the wrong identity, so authenticated callers stayed on the guest bucket) — not noise.
* **Path-only-SSRF exclusion — REJECTED.** Anthropic's list excludes SSRF findings that only reach a URL-path/scheme check without full request tracing. We have shipped SSRF hardening specifically, and our corpus has an open MAJOR finding making exactly this point — a URL-safety helper that only filters schemes and IP literals returns True for any hostname without resolving it, so DNS resolution/pinning or an allowlist is needed before relying on it.

---

# Begin

Now execute the process for `$ARGUMENTS`. Start at Step 0 (Input) — identify whether it's a single PR or a paired multi-repo review and whether this is a first review or re-review. Then proceed through the steps with judgment.
