# Deep review reference: empirical validation

Part of the deep-review process. The core that runs on every review is section 1 of `~/.claude/commands/PRlaunch.md`; `~/.claude/commands/deep-review.md` maps every step to its file. **Read this file when:** depth is Standard or Deep (5a, 5c); the diff touches a charge, reservation, hold or save path (5c); depth is Deep or the diff touches a migration, deploy config, env var, router or new file, rate limit, background task, database query or external service (5d).

## 5. Empirical Validation

### 5a. Setup

* Branch should already be rebased from Step 2
* **Backend:** `docker compose up -d --build` or your dev server — verify health endpoint responds
  * **All backend commands (linting, tests, imports) run inside Docker.** Never install dependencies locally. Use: `docker compose run --rm --no-deps app bash -c "..."`
* **Frontend:** `npm run dev` — verify dev server compiles and serves

### 5b. Code validation

* **Backstop, not a crutch (v7.1):** Step 3/3.5 diff text should now carry absolute line numbers (Step 3 § Diff format for agent prompts, `reviewer-prompts.md`), so line-number drift should be rarer — but this check stays mandatory. The improved format reduces the error rate; it does not license skipping the correction step in the core's 5b.

### 5c. Runtime validation

* **API endpoints:** `curl` to test auth, error handling, status codes, edge cases
* **Frontend UI:** Playwright (or similar) to test navigation, auth bypass, rendering, user flows
* **Concurrency (executed check):** When the diff touches a charge, reservation, hold or save path (code that debits money or credits, holds a resource, or persists a user's edit), run it. Fire two overlapping requests at it, or force an older-completes-last ordering (delay the first request's response until a second one has finished). Where the path has no server (a worker, a job, a library call), a script that drives the real code path twice at once counts; a mocked unit test does not. Record the command and the final state it observed: the balance, the reservation or hold row, the saved value, the error the UI shows. PASS means the state matches what one request, or the newer request, should leave. A ticked box does not count, and neither does reasoning that the code "looks atomic". Off those paths, parallel requests still probe blocking, races and idempotency. PRlaunch's outcome eval carries this run as a scenario (`~/.claude/commands/PRlaunch.md` §3).
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
