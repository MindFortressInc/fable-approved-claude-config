# Deep review reference: refuter prompts and the precedent ledger

Part of the deep-review process. The core that runs on every review is section 1 of `~/.claude/commands/PRlaunch.md`; `~/.claude/commands/deep-review.md` maps every step to its file. **Read this file when:** step 3 produced a CRITICAL/HIGH candidate, before you spawn a refuter.

## 3.5 Adversarial refute pass (before consolidation)

For every **CRITICAL/HIGH candidate** produced in Step 3, spawn an independent **refuter** subagent whose job is to *disprove* the finding, not confirm it. Brief it:

> "Here is the diff + the claimed defect. Try to prove it wrong: a guard exists upstream, the path is unreachable, the framework handles it, a test covers it. Default to REFUTED if you cannot demonstrate the failure."

* **Include the diff text in the refuter prompt, not a repo pointer** — consistent with the multi-PR branch-contamination rule (Step 3, Option 1). This eliminates branch-dependency and lets refuters run in parallel.
* **Deep tier:** 2 refuters per candidate; the finding survives only if **≤1** refutes it.
* **Standard tier:** 1 refuter per candidate.
* **Refuted → downgrade to INFO**, with the refutation recorded in the disposition list. A refuted finding is **never silently dropped** — the reasoning that killed it is written down.

**Cost bound:** refuters run *only* on CRITICAL/HIGH candidates, typically 0–5 per PR — so this adds a bounded, small number of subagent calls, not a per-finding tax.

### Diff format for refuter prompts (v7.1)

Format the diff text required by the "include the diff text" rule in the core's Step 3.5 per Step 3 § Diff format for agent prompts (`reviewer-prompts.md`) before it goes into the refuter prompt. Absolute line numbers let the refuter check a claimed `file:line` against the visible hunk directly instead of re-deriving it, and the same two anti-hallucination rules apply here — a refuter that doesn't see the whole file must not flag out-of-view definitions or truncated-looking scope boundaries as defects in their own right.

### Precedent ledger loader (v7.1)

Before spawning refuters, load this file's own `## Review Precedents` section (below) and paste it **verbatim** into every refuter prompt — same mechanism the repo-invariant loader (Step 3 § Repo invariants) uses for `## Review Invariants`, except the source is this file itself, not the target repo's `CLAUDE.md`:

```bash
awk '/^## /{f=0} /^## Review Precedents/{f=1} f' ~/.claude/playbooks/deep-review/refute-and-precedents.md
```

Give the refuter this explicit instruction:

> "Check the candidate finding against the precedent list above before evaluating it fresh. If it matches a precedent's fact pattern, cite the precedent number and quote its disposition instead of re-deriving the argument from scratch. **A precedent is rebuttable, not a blanket exclusion:** a finding backed by an *executed* check demonstrating the failure beats a precedent — exactly as LT judgment may move severity down but never up without evidence (Step 4 § Severity decision table). If the candidate's fact pattern differs from the precedent's in a way that matters (e.g., it's a genuinely new tenancy-scoping gap, not the one specific documented mismatch a precedent covers), say so explicitly and evaluate it fresh."

This turns the refuter from "reasons every candidate from scratch" into "checks the ledger first, cites it when it fits, and only re-derives an argument when no precedent applies or the precedent is rebutted by evidence."

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
A finding whose only impact is inside a `test_*`/`*.test.ts`/`conftest.py` file (never shipped to production) is capped at MEDIUM even when the equivalent finding in production code would be HIGH. **This caps severity — it does not exclude the finding.** A test that doesn't actually exercise its own claim is exactly the TEST-VACUITY class Step 5f exists to catch (14 of the 246 escapes in our corpus were TEST-VACUITY). **Step 5f takes precedence over this cap:** a cited test still green against the reverted implementation stays HIGH, because the impact is production behavior left untested.
*Case:* adopted from `anthropics/claude-code-security-review`'s test-only-file exclusion, deliberately narrowed against our own test-vacuity lesson before inclusion — do not read this precedent as license to skip test files.

### Rejected — do not re-add

Anthropic's list carries two more exclusions we explicitly reviewed and declined. Recorded here so a future reader doesn't "helpfully" re-add them.

* **Rate-limiting exclusion — REJECTED.** Anthropic's list excludes rate-limiting findings by default. Step 5d explicitly *requires* rate-limit decorators on new endpoints — excluding rate-limiting findings would directly contradict this process. Our own corpus corroborates the rejection: it carries a real, actionable MAJOR rate-limit finding (an SSE limiter keyed on the wrong identity, so authenticated callers stayed on the guest bucket) — not noise.
* **Path-only-SSRF exclusion — REJECTED.** Anthropic's list excludes SSRF findings that only reach a URL-path/scheme check without full request tracing. We have shipped SSRF hardening specifically, and our corpus has an open MAJOR finding making exactly this point — a URL-safety helper that only filters schemes and IP literals returns True for any hostname without resolving it, so DNS resolution/pinning or an allowlist is needed before relying on it.
