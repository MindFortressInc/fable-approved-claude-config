---
description: Ship session work — local deep-review → local CR CLI → live outcome-eval → open PR → wrapup
---

# PR Launch

Take everything worked on in this session through three local quality gates (no PR yet), then open the PR, then wrap up.

**All three quality gates run LOCALLY on the working tree / branch.** No draft PRs, no cloud CodeRabbit in the inner loop. The PR is only opened once the local gates are clean. ("CR CLI" below = the CodeRabbit CLI — substitute your secondary automated reviewer.)

The three gates catch different bug classes and are NOT redundant:
- **Deep review** grades the *diff* — correctness, security, architecture. ("Will this overwrite the DB before the user accepts?")
- **CR CLI** grades the *repo* — style, nits, patterns.
- **Outcome eval** grades the *running product from the user's seat* — does the feature do the useful thing, and does the output look right? None of that is visible in a diff (phase 3).

Order is fixed: deep-review first (nit fixes mutate code it just judged), CR CLI second, outcome eval last (judge the experience once the code is correct and clean; a bug it finds sends you back through the earlier gates).

> ### The re-gate rule (non-negotiable — this is what "all green" means)
> **Any code change in a later gate invalidates the earlier gates.** A gate's green is only valid for the exact code it ran against. If gate N (or the eval, or a fix you make anywhere) edits code, every earlier gate's result is stale — re-run the gates that the change could affect, on the new code, before finalizing.
> - Fixed a deep-review finding during CR CLI? → re-run deep-review's relevant check on the new diff.
> - The outcome eval found a bug and you changed code to fix it? → that code hasn't been deep-reviewed *or* CR-linted *or* re-tested. Run those on the fix, then re-run the eval scenario.
> - Trimmed a log line, fixed a lint nit, anything → at minimum re-run tests + lint on the final tree.
>
> **You cannot finalize (push/PR) until the FINAL committed tree — the exact bytes you're shipping — has passed every applicable gate.** "It was green earlier" is not "the final version is green." Match the re-run to the change: a logging-only or type-alias delta needs tests+lint+a smoke confirmation, not a full multi-agent re-review; a logic change needs the real gate. Proportionate, but never skipped. Phase 4 is the explicit checkpoint that enforces this before any push.

Use TodoWrite to track all 7 phases, plus phase 5's ops-step sweep as its own item. Mark each done as you go.

---

## 0. Identify session work

Before anything else, list what we're shipping:

- Walk the conversation for repos + branches touched.
- Per repo: `git status --short`, `git log @{u}..HEAD --oneline` (or `git log origin/main..HEAD` if no upstream), `git branch --show-current`.
- Group changes into shippable units. Usually = one branch = one PR. If a branch spans multiple tickets, ask the owner whether to split.
- Make sure changes are committed to a feature branch (not sitting uncommitted, not on `main`). If uncommitted, commit them now — HEREDOC message, match repo style via `git log --oneline -5`, and include the attribution trailer unless `PRLAUNCH_NO_TRAILER=1` is set (see below).
- Report the list back to the owner and confirm before proceeding: "I'm about to ship these N units through PRlaunch — confirm?"

**Attribution trailer — default ON, per-repo opt-out.** By default, commits get `Co-Authored-By: Claude <noreply@anthropic.com>` and the PR body ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Some repos forbid AI-attribution trailers, and a `commit-msg` hook may strip or reject them. **`PRLAUNCH_NO_TRAILER=1` is the single opt-out**, and it drops *both* sites — the commit trailer here and the PR-body line in Phase 5. To decide whether a repo wants it set, read commit **bodies**, not subjects:

```bash
git log -20 --pretty=%B | grep -ci 'co-authored-by: claude'   # --oneline shows subjects only, never a trailer
```

A non-zero count means the repo already accepts the trailer — leave the default on. **A zero count is not proof of an opt-out** (the repo may just be new, or no agent has committed to it yet): check the repo's contributing policy and its `commit-msg` hook (`.git/hooks/commit-msg`, `.husky/`) before deciding, and ask the owner when it stays inconclusive rather than guessing.

Attribution is a per-repo policy, not a property of this command.

**A `commit-msg` hook desyncs the ledger in two different ways — they need different responses:**

- **Rewritten** (hook edits the message, commit succeeds): the commit you shipped has a *different* sha than the one you were about to record. Re-resolve `git rev-parse HEAD` after committing and before recording any gate, or every gate stamps a sha you aren't shipping and Phase 5 blocks with a confusing STALE report.
- **Rejected** (hook exits non-zero): there is **no commit** — HEAD is unchanged and your work is still uncommitted. Stop and fix the message, then commit successfully; recording a gate now stamps the *parent* commit and green-lights code that was never committed.

**Ticket state at start-of-work is automatic if you run the tracker hooks.** The `linear-startwork.sh` PostToolUse hook flips a ticket to In Progress + assigns you the moment its ticket-token branch is created (and alerts instead of stealing if someone else holds it). So by the time you're here, each unit's ticket *should* already be In Progress + assigned. Per unit: confirm it is; if a unit's branch never carried a ticket token (so the hook never fired), the `pr-gate` link check (Phase 5) will block its PR anyway — locate/file the ticket now and either rename the branch or put `Closes <TICKET-ID>` in the body.

If there's no shippable work, stop and say so.

---

## 1. Deep review loop — LOCAL

For each unit, run the deep-review *process* (the core below; `~/.claude/commands/deep-review.md` maps every step to its file) against the **local diff**:

```bash
git diff origin/main...HEAD       # what we're about to ship
git log origin/main..HEAD         # commits we're about to push
```

Run the core below in full (correctness, security, runtime validation, empirical verification of claims). No PR number needed — the process is the process; you just point it at the local diff instead of a GitHub PR.

<!-- deep-review-core:start -->
### Deep review core

This part runs on every review. The rest is in reference files, moved there unchanged. Read a reference when its row applies. "Step N" below is a deep-review step and "phase N" is a PRlaunch phase. Commands use `origin/develop` as the target branch: substitute the branch this PR targets (`gh pr view [<pr>] --json baseRefName -q .baseRefName`, or before a PR exists the branch you will open it against) and use that one value in every command. "LT" is the lead/tech reviewer running this process.

**Reviewers are separate agents.** Classify depth below sets how many to launch. Reviewing the diff yourself is the Quick row only.

| Read this file, always from `~/.claude/playbooks/deep-review/`, never a copy in the repo under review | When |
| -- | -- |
| `reviewer-prompts.md` | Depth is Standard or Deep, or the branch, title or body carries a ticket id: read it before you write a reviewer prompt |
| `mechanical-passes.md` | Every review, at Steps 1.5 and 1.6: it holds the detector checklist and the caller-enumeration recipe (this repo publishes no scripted version of either yet) |
| `codex-lane.md` | You turned the optional Codex lane on and its run wrote a result file, or you want to change its model |
| `refute-and-precedents.md` | Step 3 produced a CRITICAL/HIGH candidate, before you spawn a refuter |
| `consolidation-detail.md` | Every review, at Step 4: effort axis, ticket-compliance mapping, structural calibration, the code-judo question |
| `validation.md` | Depth is Standard or Deep (5a, 5c); the diff touches a charge, reservation, hold or save path (5c); depth is Deep or the diff touches a migration, deploy config, env var, router or new file, rate limit, background task, database query or external service (5d) |
| `triage-and-branch.md` | You are reviewing a PR rather than your own local diff, this is a re-review or a paired multi-repo review, CI is red, the diff is over 10K lines, or Step 2 below shows the branch is behind its base |
| `publish-and-fix.md` | You post a review on GitHub, or Steps 6-10 apply |
| `principles-and-history.md` | You approve a PR, decide whether a step applies, or change this process |

### 1. Triage

#### Classify depth

| Level | Criteria | Agents | Validation |
| -- | -- | -- | -- |
| **Deep** | Payments, auth, data sensitive, infrastructure, multi-PR features | 4 (security + quality × each PR) | Full: code + runtime + E2E + infra |
| **Standard** | UI features, incremental improvements, refactors | 2 (security + quality) | Code + runtime |
| **Quick** | Typos, docs, config, dependency bumps | 0 (direct review) | Code only |

#### File-size threshold (mechanical, v6.8)

Check whether the PR pushes any file from under 1,000 lines to over:

```bash
for f in $(git diff --name-only origin/develop...HEAD); do
  before=$(git show origin/develop:"$f" 2>/dev/null | wc -l)
  after=$(git show HEAD:"$f" 2>/dev/null | wc -l)
  [ "$before" -lt 1000 ] && [ "$after" -ge 1000 ] && echo "CROSSED 1k: $f ($before → $after)"
done
```

Any crossing → record a MEDIUM structural finding: ask for decomposition (extract helpers, subcomponents, modules) or accept a one-line justification if the file remains clearly organized. Never block solely on this.

#### Check cross-PR conflicts

* Other open PRs targeting the same branch?
* Shared files at risk: migrations, config, schemas, shared services
* Migrations: do multiple PRs branch from the same `down_revision`?

### 2. Branch Alignment (mandatory)

**Before any analysis, ensure the PR branch reflects what will actually be merged.**

Run this before Steps 1.5 and 1.6: a rebase changes the diff they scan.

```
git fetch origin develop
git log --oneline HEAD..origin/develop   # develop commits not in PR
```

Any output: read `triage-and-branch.md` before any analysis.

### 1.5. AI-Cheat Mechanical Pass (v7.2)

After Step 2, run this and Step 1.6 from inside the branch under review. This repo publishes no scripted `cheat-detect.sh` yet, so the detector table in `mechanical-passes.md` is the checklist: run each of these against `git diff --no-textconv origin/develop...HEAD`, reading the post-image file where its row says so.

Detectors: `error-swallow`, `mock-of-hallucination`, `no-op-fix`, `fake-refactor`, `coverage-erosion`, `test-relaxation`, `assertion-strip`, `type-suppression`, `comment-only-fix`, `exception-rethrow-lost-context`, `dead-branch-insertion`, `workflow-trust`.

Each hit becomes a candidate through `mechanical-passes.md` § Turning a hit into a finding.

### 1.6. Changed-Surface Enumeration (v7.3)

Follow the manual recipe in `mechanical-passes.md` (a scripted `caller-enum.sh` is not published here yet): list each changed exported definition, split it into RENAME or RETYPE, and `git grep -n -w` every kept symbol. A `caller-enum-broken` line is a finding; a `caller-enum-candidate` line goes to the Step 3 signature-compatibility lens.

### 3. Static Analysis (parallel agents)

Launch agents in parallel with calibrated prompts.

#### Repo invariants (v7.0, mandatory)

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

#### Commonly missed (mandatory, every repo)

If you keep a shared lens file, a markdown file with a `## Generic Review Invariants` section and a numbered `## Commonly Missed` section listing the defect classes external reviewers most often catch after this process has passed a PR, paste both sections verbatim into every agent prompt. The loader reads it and prints the block to paste, with its instruction line and source:

```bash
python3 ~/.claude/hooks/commonly-missed.py prompt
```

* **Off by default.** Set `COMMONLY_MISSED_SOURCE` to a local file path or an `https://` URL to turn it on. Unset, the loader prints nothing on either stream, exits 0, and the review runs with the built-in lenses only.
* Paste stdout verbatim. It already carries the instruction to check every invariant and every numbered class explicitly, reporting a finding or one line on why the diff is clean of it.
* If stdout is empty while `COMMONLY_MISSED_SOURCE` is set, the lens file could not be loaded from the source or from an earlier copy. Record the one `INFO: commonly-missed:` line from stderr in the review document and continue with the built-in lenses in `reviewer-prompts.md`. This never blocks a review.

#### Agent types

* **Security reviewer** — Vulnerabilities, auth, data exposure, input validation, OWASP
* **Quality reviewer** — SOLID, DRY, code smells, test coverage, patterns
* **Ticket-compliance reviewer (v7.4)** — conditional: launched only when a ticket resolves (§ Ticket resolution, `reviewer-prompts.md`). See § Ticket-compliance agent there.

#### Codex second-opinion lane (optional, OFF by default, v7.5)

**Skip this unless you turned it on:** it runs only when `CODEX_REVIEW_ENABLED=1` is set **and** the `~/.claude/hooks/codex-review.sh` wrapper is installed, which this repo does not ship yet. Without both, Step 3 and everything after runs exactly as if the lane did not exist. With both:

```bash
CODEX_REVIEW_ENABLED=1 ~/.claude/hooks/codex-review.sh run \
  --output /tmp/codex-review-<pr-id>.json \
  --base origin/develop
```

* **Any non-zero exit means "no Codex findings this run" — never a reason to stop the review.** A Codex outage, an expired login, a refused/unentitled model, a malformed dispatch, a schema-validation failure, or the lane being disabled are none of them the review's problem. Log which one happened (for the internal review doc only) and proceed to Step 3.5/4 as if the lane had never run.

### 3.5 Adversarial refute pass (before consolidation)

For every **CRITICAL/HIGH candidate** produced in Step 3, spawn an independent **refuter** subagent whose job is to *disprove* the finding, not confirm it.

Before spawning refuters, read `refute-and-precedents.md`: it holds their brief, how many per tier, what a refuted finding becomes, the diff format for their prompts and the precedent ledger every refuter gets.

### 4. Consolidation

* Deduplicate findings across agents (same issue from security + quality → one finding)
* Assign unified IDs: `B1-Bn` (backend), `F1-Fn` (frontend)
* **Enforce the evidence-line format** — the consolidator rejects any finding lacking `Evidence: <command run> → <output excerpt>`. A finding still tagged `UNVERIFIED` (no executed check) is capped at MEDIUM here and must be verified or dropped in Step 5 before publishing.
* **Enforce the confidence threshold (v7.1)** — drop any finding scoring **below 7** on its self-reported `Confidence: <0-10>`. Evidence outranks self-reported confidence: a finding that carries executed evidence (per the evidence-line rule above) is never dropped by this rule, regardless of its confidence score.
* **Assign severity via the decision table below** — not free-form judgment.
* Incorporate context from existing PR comments (don't duplicate what others found)
* Generate consolidated document with consistent format

#### Severity decision table (v6.9)

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

#### Classify finding origin

For each finding, classify its origin. This determines the expected action:

| Origin | Definition | Action |
| -- | -- | -- |
| **IN-SCOPE** | Bug or issue introduced by this PR's code changes | Must fix if MEDIUM+. This is the PR author's responsibility. |
| **ADJACENT** | Pre-existing issue in a file the PR touches | Fix opportunistically if trivial (< 5 lines, no risk). Otherwise track as follow-up. |
| **OUT-OF-SCOPE** | Pre-existing issue in a file the PR does NOT touch | Track as a separate issue. Never block the PR for this. |

### 5. Empirical Validation

#### 5b. Code validation

* Verify each finding against actual source: file:line must match
* Correct agent imprecisions (wrong line numbers, incorrect code snippets)
* Mark: CONFIRMED / PARTIALLY CONFIRMED / NOT CONFIRMED

#### 5e. Update document

* Mark each finding's validation status
* Add evidence table with method and result

#### 5f. Test-vacuity check (v7.0)

A test that passes against the un-fixed implementation proves nothing. For every
test the PR adds or modifies that is cited as covering a finding or the PR's
stated behavior:

1. Revert the implementation under test — with your work committed, overwrite
   the source from the base (`git show <base>:<src> > <src>`) and restore it
   afterwards with `git checkout HEAD -- <src>`; or copy the test into a
   `git worktree add --detach <scratchpad>/base <base>` checkout; or
   monkeypatch the previous return value back in. Never `git stash` it:
   `refs/stash` is shared by every worktree of the repo, so the pop can land a
   parallel agent's WIP in your tree.
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
<!-- deep-review-core:end -->

Then, for what the core found:

1. Categorize findings: severity (CRITICAL / HIGH / MEDIUM / LOW) **and origin** (IN-SCOPE / ADJACENT / OUT-OF-SCOPE — see Classify finding origin in the core above). Structural findings (v6.8 lens) default to MEDIUM and follow the same disposition flow; a missed-simplification suggestion is never blocking.
   - **Fix-placement check (one-brain rule):** if the diff fixes shared-substrate behavior on ONE surface/lane, that's a HIGH structural finding. Tripwires: (a) a service importing from a routers/BFF layer to borrow logic; (b) a new comment saying "mirror"/"parity"/"same as <other path>" — that's a copy of an existing implementation; (c) the fix description scoped "on the <connector/X> path" when other lanes share the bug class (we shipped a per-lane fix once and a sibling lane kept the bug). Disposition: move the fix to the shared layer, or — if extraction is genuinely too big for this PR — keep the copy AND file a consolidation ticket referenced in the PR body. A parity comment with no ticket is an unrecorded disposition; phase 6 should catch it.
2. Fix every IN-SCOPE CRITICAL + HIGH in the working tree. Discuss MEDIUM with the owner — fix or defer.
3. **Anything not fixed gets a recorded disposition — never silently dropped:** OUT-OF-SCOPE or deferred findings → **file a tracker ticket** (or batch into one), or waive with a one-line reason. Add each to the running disposition list (finding → fixed | ticket <id> | waived: reason) that phase 6 verifies. ADJACENT findings: fix opportunistically only if trivial (<5 lines, no risk), else ticket.
4. Commit fixes (separate commit, don't amend — easier to reason about).
5. Re-run the deep-review pass on the new diff.
6. Loop until CRITICAL + HIGH are zero or explicitly waived by the owner.
7. **Stamp the gate** — once deep-review is clean, record it against the current HEAD (run from inside the repo):

   ```bash
   ~/.claude/hooks/prlaunch-gate.sh record deep_review --findings '<json>'
   ```

   This writes the gate's HEAD sha into the ledger the `pr-gate` hook enforces; any commit after this stales the entry and you re-record it here (the re-gate rule, as code).

   **`--findings` is the whole finding set, EVERY severity — not just the blockers.** Pass a JSON array (or `@/path/to/findings.json` when it's too big for argv); one object per finding from step 1's categorisation:

   ```json
   [{"severity":"HIGH","origin":"IN-SCOPE","file":"api/x.py","line":88,
     "title":"Replied-queue listing has no host-app scoping","disposition":"fixed"},
    {"severity":"MEDIUM","origin":"ADJACENT","file":"api/y.py","line":12,
     "title":"Structural: helper duplicated across lanes","disposition":"ticket <TICKET-ID>"}]
   ```

   `severity` is required on every element (the hook rejects the set otherwise); `disposition` is the same `fixed` / `ticket <id>` / `waived: <reason>` value phase 6 verifies. Pass `'[]'` when the pass genuinely found nothing — that records "reviewed, found zero", which is NOT the same as never recording. The set is stored on the gate entry (with a per-severity histogram) and mirrored as a `gate_findings` row into the automation ledger when `ledger-append.sh` is installed. Do not summarize or drop the minor tier: the set IS the measurement (why: `~/.claude/playbooks/deep-review/principles-and-history.md` § Why gate findings record every severity).

**Do not proceed to phase 2 until deep-review is clean.**

---

## 2. CR CLI loop — LOCAL

Local CodeRabbit CLI (it bypasses the cloud credit block, but its own budget is a few runs per hour per developer), **through the wrapper**:

```bash
cd <repo>
~/.claude/hooks/cr-review.sh --base main >out.txt 2>&1
```

**Call the wrapper, not bare `coderabbit review`.** With nothing configured it runs the CLI once under your own login, exactly like the bare command — what it adds is one exit-code contract every caller can trust (step 7; why: `~/.claude/playbooks/prlaunch/rationale.md` § CR CLI). **Seat pool (optional, off by default):** register one seat per teammate key with `~/.claude/hooks/cr-seats.sh add <name>` (key on stdin or in `$CR_SEAT_API_KEY`, never on the command line; or `adopt <name>` for the current login), and the wrapper rotates them with per-seat locks and cooldowns. `cr-seats.sh list` shows each seat's usage; with no seats registered, none of this runs. If the repo's default branch isn't `main`, swap it (`gh repo view --json defaultBranchRef -q .defaultBranchRef.name`).

**Redirect the full run to a file (`>out.txt 2>&1`) and read the file — never pipe it through `| tail -N`.** In a background command only the final tail survives, and the findings before it are lost for good.

1. **Classify every finding by origin FIRST** (CR CLI reviews the WHOLE repo, not just your diff, so it surfaces pre-existing main-branch findings on files you never touched). Anchor on:
   ```bash
   git diff --name-only origin/main..HEAD    # the files actually in scope
   ```
   - **IN-SCOPE** — finding is in a file your diff changed → this PR owns it.
   - **OUT-OF-SCOPE** — finding is in a file your diff did NOT touch → pre-existing; never bundle the fix into this feature PR.
2. Filter known CR junk (build your own list; ours includes a `dependency_overrides try/finally` suggestion that's known-buggy, and opaque char-count flags on YAML tone fields):
   - Don't blindly apply CR's suggested patches; verify them first. CR's own severity/origin labels are unreliable (it has called `user_id == None` a CRITICAL — a false positive; SQLAlchemy compiles `== None` to `IS NULL`).
3. **Every finding gets an explicit disposition — nothing is silently dropped:**
   - IN-SCOPE actionable → fix in the working tree, commit.
   - IN-SCOPE nit → fix if trivial, else file a follow-up ticket.
   - OUT-OF-SCOPE but legitimate → **file a tracker ticket** (batch related findings into one ticket; link the source PR + the finding's `file:line`), parented per the 3 follow-up filing buckets in `## Work Taxonomy` of `~/.claude/CLAUDE.md` — never parentless; a Bulldozer 1-off files **unassigned**, the other two assign the owner. This is the step that's easy to skip — don't. "Out of scope" means *track it elsewhere*, not *discard it*.
   - Junk / not-a-real-issue → waive with a one-line reason (recorded in the wrapup report).
   Maintain a running disposition list (finding → fixed | ticket <id> | waived: reason). Phase 6 verifies it.
4. Commit fixes.
5. Re-run `~/.claude/hooks/cr-review.sh --base main >out.txt 2>&1`.
6. Loop until every IN-SCOPE finding is fixed-or-ticketed and every OUT-OF-SCOPE finding is ticketed-or-waived.
7. **Read the wrapper's exit code — never infer the gate from stdout** (a wrapper that dies prints nothing, which reads exactly like "reviewed, found nothing"):
   - **75 — limit-blown skip (authorized):** the account is rate-limited (or, with a seat pool, every seat is rate-limited or at cap; the wrapper already rotated and applied cooldowns). Before treating it as terminal, `pgrep -f 'coderabbit review'` and kill stale hung runs (a hung run holds the slot), and if you killed one, re-run the wrapper once — skip only if that also returns 75. If the dashboard shows credits exhausted, that is the same skip. `cr-seats.sh list` shows which seats are cooling/busy/at-cap.
   - **70 — the wrapper failed internally** (unreadable seat state, broken ranking, killed mid-review): no review ran and no skip was earned. RETRY it once; never record it as a skip. A second 70 is a real CR failure: stop and report it.
   - **71 — the CodeRabbit CLI rejected our arguments** (`error: unknown option '<flag>'`): not a skip, not a verdict, and never retried unchanged — it fails identically forever. A human has to fix the invocation; stop and report it. The current invocation is `--base <branch>` with no `--plain`; use `--agent` if you want JSON.
   - **Any other non-zero** is a real CR failure, not a limit — do not skip on it.

   Never wait out long timers, and never retry more than once (71: not at all).

   On 75 only: record `CR CLI: skipped — <rate limit|credits exhausted>` in the disposition list, say so in the PR body's Testing section, and let cloud CR on the pushed PR + `/babysit-prs` be the backstop. The other two gates still run in full. Record the skip in the gate ledger so the `pr-gate` hook's `cr_cli` entry is satisfied with a reason:

   ```bash
   ~/.claude/hooks/prlaunch-gate.sh record cr_cli --skipped "<rate limit|credits exhausted>"
   ```
8. **Stamp the gate** — on the clean (non-skip) path, once every finding is fixed/ticketed/waived, record it against the current HEAD:

   ```bash
   ~/.claude/hooks/prlaunch-gate.sh record cr_cli --findings '<json>'
   ```

   Same `--findings` contract as deep-review: every severity CR CLI reported this unit, with its disposition. Use CR's own severity words (`critical`/`major`/`minor`/`trivial`) — they are stored verbatim so this gate stays directly comparable to CodeRabbit's own review store. On a limit-skip there is nothing to record; pass no `--findings`. The wrapper's stderr names the run (`run=<id>`, plus `seat '<name>'` when a pool is registered); pass them as `--run-id <id>` (and `--seat <name>`) to tie the ledger entry to that exact run.

**Do not proceed to phase 3 until CR CLI is clean *and* every finding has a recorded disposition — or the gate is recorded as limit-skipped.**

---

## 3. Outcome eval — LIVE, from the user's seat

The two gates above read code. This one **runs the product and grades what the user actually gets**: the diff is perfect, both reviews pass, and the feature still shows raw `**markdown**`, an LLM asks "which chapter?" when the chapter is open on screen, a spinner never resolves, the layout breaks at the real viewport width.

> **The failure mode isn't *not running* the feature — it's running it and grading the wrong signal.** `POST … 200`, `db md5 unchanged`, "a bubble appeared", a byte-match on a file: that is transport. Outcome is the words and pixels a human receives. 200s and hashes are necessary, never sufficient. If you didn't quote or view the real output, you didn't grade it.

### When it applies
Required when the unit touches a **user-facing surface**: UI, an AI/LLM response a human reads, an API response rendered to a user, copy, layout, or a multi-step flow. **A unit that produces or moves a binary artifact a human or a downstream render consumes — an image, video, PDF, generated file, an uploaded/mirrored asset — IS a user-facing surface even when the code reads like plumbing:** the artifact is the output, so you open it and look (step 5), you do not grade its checksum. If the unit is pure internal plumbing / test-only / config with **no human-readable or viewable output at all**, write one line — "no user-facing surface — outcome eval N/A" — and move on (phase 4 still applies) — and record the N/A in the ledger with a reason so the `pr-gate` hook's `outcome_eval` entry is satisfied (no scenarios needed): `~/.claude/hooks/prlaunch-gate.sh record outcome_eval --na "no user-facing surface"`. When in doubt, it applies — and "it's plumbing" is not a licence to skip the look when there's a file to look at.

**A charge, reservation, hold or save path never takes the N/A exit.** That is code that debits money or credits, holds a resource, or persists a user's edit. If the diff touches one, the eval carries one overlap scenario even when there is no screen: two overlapping requests, or an older-completes-last ordering, run against the live rig the way deep-review 5c describes (where the path has no server, a script that drives the real code path twice at once counts; a mocked unit test does not). The PASS criterion is still written from the user's seat, before the run: what the user is left with (one charge on their balance, their newer text when they reopen it, the error they still see). The scenario file records the command and the final state observed, read from the store that produces what the user sees. Grade it the way every scenario is graded: quote the observed state. A ticked box is not a run.

### The method (in order — do not reorder)

1. **Write the scenarios and PASS criteria BEFORE running anything.** One scenario per user-facing behavior the PR adds or changes. Each criterion must be phrased as *what the user receives*, from their seat — not what the system did internally. Save them to the scenarios sidecar next to the ledger, then register it — this is the `pr-gate` precondition for recording `outcome_eval`:

   ```bash
   SCEN=$(~/.claude/hooks/prlaunch-gate.sh path --scenarios)   # never rebuild this path by hand
   mkdir -p "$(dirname "$SCEN")"   # the ledger dir is only created by `record`, which may not have run yet
   # write your scenarios + PASS criteria into "$SCEN" (one per user-facing behavior), then:
   ~/.claude/hooks/prlaunch-gate.sh record scenarios "$SCEN"
   ```

   Writing them first is the whole point: it removes the post-hoc rationalization that lets a failing result get graded ✅.
   - ✅ "The assistant's reply names the open chapter or its content and does NOT ask which chapter."
   - ✅ "The chat bubble shows real bold; zero literal `**` or `#` in the rendered text."
   - ❌ "POST returns 200." ❌ "ui_context payload contains active_scene_id." ❌ "a bubble exists." ❌ "the mirrored file is byte-identical / `200` + `image/jpeg`." (All transport — necessary, not sufficient. A checksum or content-type match proves the bytes moved, not that the artifact is the *right* artifact.)
   - **Use a REAL representative input, not synthetic filler.** Eval a real brand / real document / real user record when one is reasonably obtainable — `picsum`, `lorem ipsum`, `foo@test.com`, an all-zeros blob can pass an eval the real input would fail (wrong format, size, encoding, content), and they leave junk in real buckets/DBs. If only synthetic input is feasible, say so and why.

2. **Run each scenario on the live local rig** (start the dev servers per the project's run skill; don't conflict with the owner's ports). Drive the real UI (Playwright/MCP) or hit the real endpoint.

3. **Read the actual artifact and judge it against the criterion — out loud, in your report.** For each scenario quote the real output: the words the LLM said, the rendered text content, what the screenshot shows. Then state PASS/FAIL with the evidence. "I read the reply: '…Which chapter are you rewriting?' → FAIL, it should know it's the open one." A scenario you can't quote the output for is not graded.

4. **For AI / LLM features, grade the response itself** (this is the recurring miss — LLM outputs are emergent from prompt+data and invisible to both code gates):
   - **Context awareness** — did it use the context it was given (the open scene, the selection, prior turns), or ask for something it already had?
   - **On-target** — did it address the actual thing (the selected passage, the named character), or answer generically?
   - **Voice/format** — does it read like the product's voice and render correctly (formatted, not raw markdown; no `#` headers dumped into a chat bubble)?
   - **No dead ends** — did the turn actually resolve in the UI (no stuck "thinking…" after the server returned), and is the result persisted/applied where the user expects?

5. **Visual pass — actually look.** Take screenshots (or read rendered `textContent`, not your own boolean asserts) of every changed surface. Look for: raw markdown, overlapping/stuck overlays, broken layout, truncated/garbled text. **Check the real viewport** — if the product is used at desktop width, eval at desktop width. **This applies to any file/binary the change produces or moves, too** — open the image (Read it), play the clip, open the PDF, and describe what it actually *depicts*; a hash/size/`Content-Type` check is not a look. (Incidents behind both rules: `~/.claude/playbooks/prlaunch/rationale.md` § Outcome eval.)

6. **The annoyance test.** For each surface ask: *if I were the user and hit this, would I be annoyed or confused?* That question catches the "basic stuff" class better than any assertion. If yes → it's a finding, even if every status code was 200.

### Disposition
Every FAIL is a finding with the same disposition rules as phases 1–2: fix IN-SCOPE in the working tree and re-run the scenario; or ticket; or waive with a reason. **Loop until every scenario PASSES or is explicitly waived by the owner.** Add results to the running disposition list. A bug found here that you fix → commit it and, if it changed code the earlier gates judged, re-run the relevant gate on the new diff.

### Data safety
If the rig writes to a real local DB, snapshot the rows you'll touch first and restore + md5-verify after. Never leave eval residue in the owner's data.

Once every scenario PASSES (or is waived), **stamp the gate** against the current HEAD:

```bash
~/.claude/hooks/prlaunch-gate.sh record outcome_eval
```

**Do not proceed to phase 4 until every applicable scenario PASSES (or is waived) and you have quoted the real output for each.**

---

## 4. Re-gate checkpoint — confirm the FINAL tree is green

The gates ran in sequence, but each fix mutated the tree *after* an earlier gate judged it. Before pushing, prove the **exact bytes you're about to ship** are green — not "green at some point during the run." This phase is short when nothing changed late, and essential when the eval (or a CR fix) sent you back into code.

1. **List everything that changed since each gate last ran.** `git diff` the tree; look at every commit made during phases 1–3 plus any uncommitted working-tree edits. For each change, name which gate(s) it could invalidate.
2. **Re-run those gates on the final code, proportionate to the change** (per the re-gate rule above):
   - logic / behavior change → re-run the real gate (deep-review check, and/or the eval scenario it affects).
   - logging / types / comments / lint-nits → re-run **tests + lint** at minimum, plus a smoke confirmation of any path involved.
   - Always end with: tests green, lint/type-check clean, on the final tree.
3. **If the change touched a user-facing path, re-confirm the affected outcome-eval scenario** on the final code — quote the real output again. (A fix made "to satisfy the eval" is itself untested code; the eval isn't passed until it passes on the fixed version.)
4. **Confirm the tree is the tree you'll ship:** `git status --short` clean except intentional never-commit files; everything you intend to push is committed.

Once tests + lint are green on the final tree, **stamp the tests gate** with the exact command you ran:

```bash
~/.claude/hooks/prlaunch-gate.sh record tests --cmd "<the actual test command run>"
```

**Do not proceed to phase 5 until tests + lint are green on the final committed tree and every code change since each gate has been re-gated.** If this checkpoint surfaces a new fix, you've changed code again — loop back through the affected gate, then return here. Finalize only when a full pass produces zero new changes.

---

## 5. Push + open PR

For each unit:

1. **Pre-flight: verify the gate ledger** (the global `pr-gate.sh` hook BLOCKS `gh pr create` unless all four gates — deep_review, cr_cli, outcome_eval, tests — are recorded in the ledger at the current HEAD). You stamped each at its phase boundary; this confirms the final tree is fully gated:

   ```bash
   ~/.claude/hooks/prlaunch-gate.sh check
   ```

   If `check` fails it names the exact missing or stale gate and the HEAD to re-run it on — go re-run that phase, `record` it again, and re-check. Any commit after a gate ran stales that gate's entry (the re-gate rule, enforced by the hook), so a late fix means re-recording the affected gate here before `check` goes green. Do NOT hand-write ledger entries to satisfy the hook — that defeats the entire gate. (Emergency owner-authorized bypass: `PRLAUNCH_SKIP=1` in the command.)

2. `git push -u origin <branch>` (if no upstream) or `git push`.
3. **(Optional) Publish the CR-CLI attestation, now that the sha exists on GitHub:**

   ```bash
   ~/.claude/hooks/prlaunch-gate.sh publish-cr-cli
   ```

   Off by default: it only posts a `review-gate/cr-cli` commit status when `PRLAUNCH_PUBLISH_CR_STATUS=1` is set — useful if your CI has a review-gate check that reads it; otherwise it says publishing is off and exits 0. **After the push** (GitHub's Statuses API 422s on a sha it has never seen, so the inline publish during phases 1–4 no-ops) and **before `gh pr create`** (the PR-opened event is what makes a review-gate workflow classify this sha; why: `~/.claude/playbooks/prlaunch/rationale.md` § publish-cr-cli ordering). It republishes the recorded entry and never re-records; if the recorded sha isn't HEAD it refuses and tells you to re-run Phase 2.

4. **Sweep ops steps → one tracker ticket each, filed BEFORE you write the body.** An **ops step** is anything a human must do outside this PR for the change to actually work in prod: provision infra (bucket, DB, queue, domain), create or scope a credential/token, DNS, an IAM/permission grant, an env var to set on a box, a dashboard/console click, third-party account config, a post-deploy migration or flag flip, or a verification code cannot perform. The usual tells: anything you stubbed, faked, or pointed at a local rig to get the eval green, and anything that would 4xx/5xx on prod today.

   **Each one gets its own tracker issue** (`mcp__linear__save_issue`) — not a comment on the epic, not a bullet in this PR body, not a line in the wrapup report, and not one batched catch-all. Prose has no status, no assignee and no board presence (`~/.claude/playbooks/prlaunch/rationale.md` § Ops steps as tickets). Defaults: title prefixed `Ops:`, **same project as the originating ticket, and its labels copied verbatim** (don't invent an `Ops` label or add labels of your own), parent = the originating ticket, assign the owner, Urgent if it blocks the feature in prod. The description states the **exact command or console path**, the **blast radius** (what stays broken until it's done, and what else it touches), and **how to verify** it worked.

   The body's **Ops** section then links the tickets — the steps live in the tickets, never in the prose. **Zero ops steps is a sentence you write, not a silence:** "Ops: none — nothing to provision, configure, or grant." Say it in the body and in the phase-6 report.

5. Open the PR — **ready, not draft** (local gates are already clean). **The PR must link to its tracker ticket** — the `pr-gate` hook blocks `gh pr create` unless the branch carries a ticket token or the body carries the ticket id (e.g. `Closes <TICKET-ID>`), so the tracker auto-attaches it. (`LINEAR_SKIP=1` only for genuinely ticket-less PRs like config/infra.) The body MUST include a **Testing** section reporting everything actually run and passed before submission — past tense, with results, including the logic/outcome scenarios. This is the evidence trail for reviewers; an empty or future-tense ("- [ ] should test X") Testing section means Phase 5 isn't done:

   ```bash
   gh pr create --title "<short title>" --body "$(cat <<'EOF'
   ## Summary
   <1-3 bullets — focus on the why>

   ## Testing
   All gates run locally on the final tree (HEAD <short-sha>) before this PR was opened:

   - **Tests:** <suite> — N passed, 0 failed (`<command used>`)
   - **Lint/types:** clean (`<command used>`)
   - **Deep review (diff):** N findings → all fixed/ticketed/waived; M iterations to clean
   - **CR CLI (repo):** clean after N findings dispositioned — or: skipped, rate-limit/credits (cloud CR on this PR is the backstop)
   - **Outcome eval (live, user's seat):** M scenarios PASS — one line per scenario with the observed result, e.g. "rewrite request → reply addressed the selected passage, knew the open chapter, rendered formatted (no raw markdown)"
     - or: N/A — no user-facing surface
   - **Re-gate:** final tree re-verified after last code change (tests + lint + <affected gate/scenario>)

   ## Ops
   Human steps required before this works in prod — each tracked as its own ticket:
   - <TICKET-ID> — <one line: what a human must do>
   - or: none — nothing to provision, configure, or grant

   ## Test plan (reviewer)
   - [ ] <anything a human reviewer should still verify>

   Closes <TICKET-ID>

   🤖 Generated with [Claude Code](https://claude.com/claude-code)
   EOF
   )"
   ```

   Drop the `🤖 Generated with …` line when the repo opts out of the attribution trailer (`PRLAUNCH_NO_TRAILER=1` — see Phase 0). Everything above it stays.

6. Cloud CodeRabbit will auto-run on the ready PR — that's a confirmation pass, not the gate. The gate was already met locally.
7. Report the PR URL back to the owner.

---

## 6. Wrapup

Same flow as `/wrapup`:

1. **Tracker** — update tickets to "In Review", then **verify the PR attachment actually landed**: `mcp__linear__get_issue <id>` and confirm the PR URL is in `attachments`/links. If it's missing (the `Closes`/branch auto-link didn't fire), attach it explicitly (`mcp__linear__save_issue` with `links: [{url, title}]`) — don't assume the magic word stuck (tickets are systematically under-linked). Comment on session progress.
2. **Disposition gate** — walk the running disposition list from phases 1+2+3. Every finding must be `fixed`, `ticket <id>`, or `waived: <reason>`. **If any OUT-OF-SCOPE-but-legit finding has no ticket yet, file it now** (batch related ones; link source PR + `file:line`), parented and assigned exactly as in phase 2 step 3 (buckets: `## Work Taxonomy` of `~/.claude/CLAUDE.md`; a Bulldozer 1-off files **unassigned**, the other two assign the owner) — never parentless. A finding with no disposition is a bug in the wrapup — resolve it before reporting. **Ops steps disposition the same way:** every ops step from phase 5 appears here as its own ticket id, never as a sentence. If one survives only as prose — in a PR body, an epic comment, or this report — it isn't tracked; file it now. Zero ops steps → say so explicitly.

   **Durable per-unit record (guarded no-op if the helper is absent).** For EACH shipped unit, append one `prlaunch`/`unit` event to `~/.claude/automation-ledger.jsonl` (a durable quality record a weekly scorecard can aggregate). Pull `cr_cli` (clean or its skip-reason) and `outcome_eval` (`na` or scenario count) straight from this unit's gate ledger; supply the deep-review CRITICAL+HIGH count, `regate` done, and whether `PRLAUNCH_SKIP` was used:
   ```bash
   if [ -x ~/.claude/hooks/ledger-append.sh ]; then
     # This unit's gate ledger. NEVER reconstruct this path by hand:
     # prlaunch-gate.sh OWNS it and keys it on the repo's identity (origin
     # remote name), not the checkout's directory -- in a worktree a hand-built
     # `<dirname>--<branch>` never matches and every read comes back empty.
     # Ask the owner; fall back to the old derivation on hooks predating `path`.
     L=$(~/.claude/hooks/prlaunch-gate.sh path 2>/dev/null) || L=""
     [ -n "$L" ] || L=~/.claude/prlaunch-ok/"$(basename "$(git rev-parse --show-toplevel)")--$(git branch --show-current | tr '/' '-')".json
     # FAIL LOUD. A missing/corrupt ledger must ABORT the append, never land a row
     # with cr_cli:"" -- an empty string is indistinguishable from a real value
     # once a reader aggregates it. No 2>/dev/null.
     [ -f "$L" ] || { echo "wrapup: gate ledger not found at $L — NOT appending a row" >&2; exit 1; }
     SKIP=$([ -n "${PRLAUNCH_SKIP:-}" ] && echo true || echo false)
     # An ABSENT gate must abort too, not default: `.gates.cr_cli.skipped // "clean"`
     # on a ledger with no cr_cli entry reports a gate that never ran as "clean".
     jq -e '.gates.cr_cli != null and .gates.outcome_eval != null' "$L" >/dev/null \
       || { echo "wrapup: $L has no cr_cli/outcome_eval gate — NOT appending a row" >&2; exit 1; }
     CR=$(jq -er '.gates.cr_cli.skipped // "clean"' "$L") \
       || { echo "wrapup: cannot read .gates.cr_cli from $L — NOT appending a row" >&2; exit 1; }
     # outcome_eval: "na" if the gate ledger recorded --na, else your real scenario count
     if [ -n "$(jq -r '.gates.outcome_eval.na // ""' "$L")" ]; then OE="na"; else OE="<N> scenarios"; fi
     # NOTE: `dr` is a CRITICAL+HIGH count ONLY, kept for a scorecard's
     # existing series. It is NOT the measurement -- the full finding set is
     # recorded at each gate boundary via `record <gate> --findings`, which
     # also emits a `gate_findings` ledger event. Never treat `dr == 0` as
     # "deep-review found nothing"; it means "no CRITICAL or HIGH".
     U=$(jq -n -c --arg repo "<repo>" --argjson pr <PR#> \
         --argjson dr <deep-review CRITICAL+HIGH count> \
         --arg cr "$CR" --arg oe "$OE" \
         --argjson regate true --argjson skip "$SKIP" \
       '{skill:"prlaunch", event:"unit", repo:$repo, pr:$pr,
         gates:{deep_review_findings:$dr, cr_cli:$cr, outcome_eval:$oe, regate:$regate, prlaunch_skip:$skip}}')
     ~/.claude/hooks/ledger-append.sh "$U" \
       || echo "WARN: unit row REJECTED — this unit is missing from the durable record" >&2
   fi
   ```

   **Say the WARN out loud in the step 7 report if it fires** — `ledger-append.sh` writes nothing on a malformed payload, and the unit silently vanishes from the record. The `||` hangs off the INVOCATION, not off the `[ -x … ]` test, so an absent helper stays a silent no-op. The `exit 1`s abort only this optional append, never the PR: telemetry never blocks shipping.
3. **GitHub** — verify all PRs from this run show correctly; note any other open PRs touched this session.
4. **Branches** — confirm no leftover dirty/unpushed state in any repo touched.
5. **Memory** — capture anything non-obvious from this run (gotchas, decisions, unexpected deep-review / CR CLI / outcome-eval findings worth remembering). Check for existing entries to UPDATE before creating new ones.
6. **Cleanup queue** — `python3 ~/.claude/hooks/cleanup-sweep.py --count`. If `>0`, run the `/cleanup` sweep (show queued deletes, confirm with the owner, then `cleanup-sweep.py --run <i>` each approved entry — or `--run-all` — which deletes the parsed targets directly; do NOT re-run the queued `cmd`. `--remove <i>` only for declined entries). Report cleared vs. left; `0` → "Cleanup: nothing pending".
7. **Report** — one consolidated message:

```
## PRlaunch complete

**PRs opened (ready for review)**
- repo-a #123 — <ticket> — <one-line summary> — <url>
- repo-b #456 — <ticket> — <one-line summary> — <url>

**Deep-review (local)**
- #123: N CRITICAL/HIGH fixed across M iterations
- #456: clean on first pass

**CR CLI (local)**
- #123: N in-scope findings fixed
- #456: 1 finding waived (reason)

**Outcome eval (live, user's seat)**
- #123: M scenarios, all PASS — e.g. "critique reply addressed the selected passage, knew the open chapter, rendered formatted"
- #456: 1 scenario FAILED → fixed (raw markdown in chat bubble) → re-ran PASS
- N/A for <unit> — no user-facing surface

**Out-of-scope findings tracked** (deep-review + CR CLI + outcome eval)
- <ticket> — <what> (filed from #123's CR sweep)
- waived: <finding> — <reason>

**Ops steps filed** (human action required before this works in prod)
- <ticket> — <what a human must do> — blocks #123 in prod
- (or: none — nothing to provision, configure, or grant)

**Tracker**
- <ticket> → In Review
- <ticket> → In Review

**Branches**
- all clean

**Memory**
- Saved: <slug> — <why> (or: nothing new)

**Open follow-ups**
- <thing>
```

---

## Guardrails

- **Unstick rule (eval/browser rigs): 3 strikes → change the substrate, not the parameters.** If the same tool call fails or times out 3 times (screenshot, click, generate), STOP retrying variations of it. Escalate in order: (1) tear down and recreate the surface (close the Playwright page and renavigate — this cleared a permanently wedged page.screenshot once; restart the dev server), (2) switch instrument (read textContent/DOM state instead of pixels; hit the API instead of the UI), (3) switch driver entirely (a vision-capable remote agent that can run MCPs and loop — use it when local Playwright keeps fighting back). Log what unstuck it. Burning 10+ minutes re-trying one wedged call is a process failure, not persistence.
- Confirm with the owner before: first push of any new branch, opening a PR, applying fixes flagged as questionable by deep-review or CR CLI.
- **Never merge.** The owner's team merges.

Each rule below is stated in full where it runs; this is the checklist.

- **Gate order:** deep-review → CR CLI → outcome eval (top of file). Diff, repo, running product: complementary, not redundant.
- **Re-gate on every change** — "green earlier" ≠ "the final version is green" (the re-gate rule; phase 4 is the checkpoint).
- **Outcome eval grades outcomes, not transport** — quote the words, view the pixels, open the file; real input, not filler (phase 3).
- **Both code gates run LOCALLY** before the PR is opened; cloud CodeRabbit on the PR is a confirmation pass. CR CLI may be skipped only on the wrapper's exit 75 — recorded, never silent (phase 2 step 7).
- **The pr-gate hook is the enforcement, not the process.** Never hand-write ledger entries (phase 5 step 1); get the ledger and scenarios paths from `prlaunch-gate.sh path`, never by rebuilding them (phase 6's fallback exists only for hooks predating `path`); `PRLAUNCH_SKIP=1` is for owner-authorized emergencies only.
- **Out-of-scope ≠ discard** — every finding from all three gates is fixed, ticketed or waived-with-reason (phase 6 step 2).
- **Ops steps are tickets, not prose**, one each, filed before the body is written; "none" is said out loud (phase 5 step 4).
- **One brain, no lane-fixes** — copy + consolidation ticket, never copy + parity comment (phase 1 fix-placement check; why: `~/.claude/playbooks/prlaunch/rationale.md` § One brain).
