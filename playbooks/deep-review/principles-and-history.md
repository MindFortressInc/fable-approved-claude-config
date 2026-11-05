# Deep review reference: principles and version history

Part of the deep-review process. The core that runs on every review is section 1 of `~/.claude/commands/PRlaunch.md`; `~/.claude/commands/deep-review.md` maps every step to its file. **Read this file when:** you approve a PR, decide whether a step applies, or change this process.

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

## Why gate findings record every severity

**Why this matters:** if all you keep is a single CRITICAL+HIGH count, every MEDIUM and LOW finding is thrown away — including all structural-lens findings, which default to MEDIUM. That makes the review's output unmeasurable: "0 findings on most units" really means "no CRITICAL or HIGH", and nothing records what the process actually said. Do not summarize, do not drop the minor tier — the set you pass to `--findings` IS the measurement.

---

## Version history

Standardized process for PR reviews with empirical validation. Calibrated over dozens of real reviews; the case references (PR #66, #101, #113/#80, …) are the actual incidents that produced each rule.

**v6.7 → v6.8:** structural-quality lens, adapted from Cursor's `thermo-nuclear-code-quality-review` skill. Adds: file-size threshold check (Step 1), structural regression detection prompt (Step 3), structural severity calibration + the code-judo question (Step 4), anti-nit-flooding publish rule (Step 8). Core stance: **structure informs findings; it never gates merges.** A missed simplification opportunity is a suggestion, not a blocker.

**v6.8 → v6.9:** evidence-required findings (no evidence ⇒ max MEDIUM), Step 3.5 adversarial refute pass on CRITICAL/HIGH, fixed severity decision table, evidence-based fix confirmation in re-review. Core stance: **review quality is model-independent — evidence and adversarial refutation replace reliance on LT judgment to recalibrate.**

**v6.9 → v7.0:** repo-invariant loader (Step 3), tenancy/scope-binding pass (Step 3), test-vacuity check (Step 5f). Core stance: **the process learns each repo's rules by reading that repo — it never hardcodes them.** Calibrated from an escape corpus of a few hundred bot findings (CodeRabbit/Greptile) on PRs that had already passed deep-review.

**v7.0 → v7.4:** generated-code exclusions, self-reported confidence with a consolidation threshold, absolute-line-number diff format, a zero-score drop-list and a Review Precedents ledger loaded into every refuter (v7.1); an AI-cheat mechanical pass (Step 1.5) plus two semantic detectors (v7.2); changed-surface enumeration (Step 1.6) plus a signature-compatibility lens (v7.3); ticket resolution, a ticket-compliance agent, its severity mapping and a publish block (v7.4).

**v7.4 → v7.5:** optional Codex cross-family second-opinion lane, **off by default** (Step 3), three-bucket divergence reporting at consolidation with agreement reported as a count, never re-rendered (Step 4), routing every Codex-only candidate through the same evidence-based confirmation as any other finding before it may be promoted (Step 5b), and an explicit publish gate (Step 8). Core stance: **a second model family's disagreement is signal, not a verdict — Codex's own findings are claims, and nothing from this lane reaches a PR comment, a tracker comment, or a memory write until Claude has independently confirmed it against the actual code.**

**v7.5 → v7.7:** effort-weighted severity (Step 4 effort axis) and three named noise-suppression classes (Step 8), derived from a corpus of about 4,000 CodeRabbit review threads (65% baseline resolution): CR severity is **inversely** correlated with getting fixed (Critical 60%, Major 62%, Minor 68%, Trivial 75%) while effort predicts it strongly (quick-win 68%, heavy-lift 45%) — the binding variable is cost-to-fix, not importance. So a heavy-lift CRITICAL/HIGH is merge-blocking or waived with a reason + filed ticket, never silently deferred. **This direction is deliberately counterintuitive: do not "fix" it back to escalate-by-severity, which is the policy that produced the 60%-for-Critical row.** Suppression classes (18% of CR output): model-id currency (39% resolved), lint/typing (63%, only 23% crit+major), shared-client convention (51%).

**Also in v7.7:** a twelfth Step 1.5 detector, `workflow-trust` (an untrusted-code CI trigger on a persistent runner); an optional commonly-missed lens loader (Step 3, off until you point it at a lens file); a provenance step in the tenancy pass (Step 3: where did the scope value come from?); and an executed overlap run in 5c on charge, reservation, hold and save paths. (The private line's v7.6 turned the Codex lane on by default. This copy skips that: the lane stays off unless you enable it.)

**Terminology:** "LT" = the lead/tech reviewer running this process (you, or the agent acting for you). Target branch examples use `develop`; substitute `main` if that's your trunk. The infra checklists reflect one real stack (FastAPI + Alembic + Celery + Docker + Vercel/Railway + Auth0/Stripe) — adapt the specifics to yours; the *categories* are the point.
