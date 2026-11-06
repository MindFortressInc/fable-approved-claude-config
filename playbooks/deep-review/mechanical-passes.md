# Deep review reference: mechanical passes

Part of the deep-review process. The core that runs on every review is section 1 of `~/.claude/commands/PRlaunch.md`; `~/.claude/commands/deep-review.md` maps every step to its file. **Read this file when:** every review, at Steps 1.5 and 1.6: it holds the detector checklist and the caller-enumeration recipe (this repo publishes no scripted version of either yet). This repo publishes no scripted detector or caller enumerator yet, so the tables and recipe below are what you run.

## 1.5. AI-Cheat Mechanical Pass (v7.2)

Test-vacuity (Step 5f) covers exactly one AI-cheat failure mode out of roughly a dozen ways an AI-authored diff fakes success. When agents author most of your PRs, these are your own failure modes, not hypotheticals — a diff that looks like a fix but isn't is exactly the shape a normal reviewer misses, because the code *reads* fine.

This step runs **12 structural detectors** mechanically — pattern checks over the diff, no LLM judgment, no sandbox — *before* Step 3's agents are even launched. The core lists the twelve names; this file is the checklist. Two companion **semantic** detectors (`goal-not-fixed`, `cheat-mock-mutation`) that need judgment rather than pattern-matching are folded into the Step 3 quality-reviewer prompt instead (see Step 3 § AI-cheat semantic pass); this step covers only the mechanical twelve. The first eleven are ported from `moonrunnerkc/swarm-orchestrator`'s cheat-detector suite (303/325, 93.2% recall across 13 categories on their defect-injection oracle; 0.11 findings/PR false-alarm rate on an 18-PR pilot — the bar this pass should stay under; tighten a detector rather than ship it noisy).

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
| `workflow-trust` | A touched `.github/workflows/*.yml` pairs an untrusted-code trigger (`pull_request_target`, `workflow_run`, `issue_comment`, a `pull_request`/`pull_request_review`/`pull_request_review_comment` job with no fork guard in its job-level `if:`, a `deployment`/`deployment_status` job with no creator check) with a persistent runner: any `runs-on` that is not a GitHub-hosted `ubuntu-*`/`windows-*`/`macos-*` image, including custom labels and expressions. Read the whole post-image workflow file from the PR branch, not just the hunk, because the trigger and the `runs-on` are rarely in the same hunk. **Candidate only**, see below |

**Running `workflow-trust` by hand.** These are the rules a scripted version has to get right, so apply them when you read the file:

* **Triggers are event keys under `on:` only.** A branch filter or an input that happens to be named `deployment` is not a trigger. Quoted keys count (`'pull_request_target':`, and the quoted `'on':` or YAML-1.1 `true:` forms of `on`).
* **A runner is persistent unless it is a whole GitHub-hosted image name.** `self-hosted`, any custom label (including look-alikes such as `ubuntu-gpu-selfhosted`), any runner `group:`, and any `${{ }}` expression count as persistent. A `runs-on` block list counts too, wherever its `- ` entries are indented.
* **Guards are per job, and only a job-level `if:` counts.** A guard on one job does not clear another job in the same workflow. The same expression in a step's `if:`, a `run:` string, `env:` or `with:` is not a guard. A folded multi-line job-level `if:` still counts.
* **A guard counts only when it excludes untrusted actors.** A fork guard (`github.event.pull_request.head.repo.full_name == github.repository`, or `...head.repo.fork == false`) clears only the `pull_request` family. A creator check clears only the `deployment` family, and only as an equality on the creator's `login` or `id` (a `creator.type == 'Bot'` check admits every app). A check that selects forks (`...repo.fork`, `full_name != github.repository`) or a negated creator check is not a guard. A job-level `if:` that holds `||` or a negated `!( ... == ... )` group can re-admit untrusted actors, so its guard does not count.
* **Only touched workflow files are read**, and a deleted workflow is skipped.

**Turning a hit into a finding:** each detector hit becomes a Step 3-style finding, using the standard required format (Step 3 § Required finding format):

* **Evidence:** the exact check run (the `git diff`/`git grep` invocation) plus the quoted diff hunk that matched. This is an *executed* check per the v6.9 evidence rule — it is never `UNVERIFIED`.
* **Confidence:** mechanical hits are deterministic; default `Confidence: 8` unless file-specific context makes the match doubtful (e.g. a `no-op-fix`/`coverage-erosion` hit on a file the PR's own description says is deliberately test-free).
* **Severity:** assigned via the *existing* Step 4 decision table — default MEDIUM (the same row as any other real, undemonstrated-CRITICAL/HIGH finding), escalating to HIGH only when Step 4's HIGH criteria are met, which for a cheat detector means the cheat masks a finding the PR itself claims to fix (e.g. an `error-swallow` hit swallowing the exact exception the PR's description says it now handles). This is not a new severity axis — do not invent one.
* A detector hit is a candidate, not an automatic finding: it still goes through the Step 3.5 refuter pass and the Review Precedents ledger like any other candidate before it survives to Step 4 consolidation.
* **`workflow-trust` hits are never findings on their own.** A trigger plus a persistent runner is only dangerous when nothing gates who chooses the code. Hand each hit, verbatim, to the Step 3 quality reviewer with the workflow file. It becomes a finding only when the reviewer cannot quote the actor, creator or fork check that gates the job, and its severity then comes from the Step 4 table like any other finding. A quoted gate dismisses the candidate only when it actually excludes untrusted actors: an equality or allowlist check passes, but a check that mentions the actor without excluding forks (for example `github.actor != 'dependabot[bot]'`) does not. Record the quote, and why it excludes them, as the dismissal reason.

---

## 1.6. Changed-Surface Enumeration (v7.3)

Diff-scoped review has a documented recall gap: independent 2026 benchmarking puts Greptile (which indexes the whole codebase before reviewing the diff) at 82% bug-catch vs CodeRabbit's 44%, and the canonical miss is "a function returns a new type that breaks three callers in files you didn't touch." Nothing in Steps 1-1.5 enumerates callers of a changed signature — this step does, mechanically, like the core's file-size threshold check.

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
