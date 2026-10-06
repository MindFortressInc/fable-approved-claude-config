---
name: flushdeployed
description: Use when auditing/"flushing" the Deployed column of a Linear project against ACTUAL prod code — confirming each shipped ticket is truly live, splitting partially-shipped tickets into done+todo, and moving wrongly-marked ones back to Todo. Triggers "/flushdeployed <project>", "flush the deployed tickets for X", "validate the deployed tickets", "are these really deployed". A fuzzy project name resolves to the Linear project.
---

# flushdeployed — validate the Deployed column against prod

"Deployed" is a **claim to verify, not trust.** Linear auto-advances tickets to Deployed on PR merge, but: (a) a manually-deployed service ships by hand so merged ≠ live; (b) tickets get marked Deployed when only *part* of their scope shipped; (c) some are reverted/wrong. This skill validates each Deployed ticket against `origin/main` + the live box, then **moves the truly-done to Done, splits the partials, and bounces the wrong ones to Todo.**

`validate-workflow.js` (the fan-out validator) and `apply-done-workflow.js` (the write helper) live next to this file.

## THE DECISION RULE — three buckets, nothing else

Every ticket lands in exactly one. Decide the bucket FIRST, then pick the mechanics.

| # | Test | Outcome |
|---|---|---|
| **1** | Meets **all** its own acceptance criteria **AND** the change is **LIVE IN PROD** | **DONE** |
| **2** | Meets all its criteria **but is NOT live in prod** — merged and waiting on a deploy | **keep Deployed** |
| **3** | **Anything else** | **split and/or back to Todo** |

**"Live in prod" is per-repo — resolve it from step 3's deploy models, never from a universal "is it on the box".** Manual-box and container-image repos → the change must be an ancestor of the running box HEAD / image sha. Auto-deploy (PaaS tracking `main`) and template repos → **merged to `origin/main` IS live**, so a criteria-complete ticket there is bucket 1, never bucket 2. Package-registry repos → the published `latest` carries it. A subdirectory with its own deploy target (a serverless Worker, a separately-deployed function) → a live probe, because box ancestry proves nothing about it. Treating "not on the box" as bucket 2 for an auto-deploy repo would park finished work forever — the exact bug this rule exists to kill.

**Bucket 2 is the ONLY legitimate reason to leave a ticket in Deployed.** `Deployed` is a *started*-type column meaning "merged, awaiting deploy." It is not a parking space for work that is live-but-unfinished. If the code is running in prod and the ticket still is not done, that is **bucket 3** — split the shipped part to Done and carve the remainder, or send the whole thing back to Todo. Never leave it in Deployed.

**"Meets all its criteria" means the ticket's OWN Done-when / Verify / acceptance section — not "the PR merged."** Read it before deciding. An ops ticket whose criterion is "observe a green scheduled run" is not done because the code that would produce that run merged.

**Bucket-3 sub-cases:**
- Part shipped + concrete unshipped remainder, **no existing child covers it** → split: create remainder Todo, ✂️ on original, original → Done.
- Part shipped + the remainder **already exists as open child tickets** → do NOT mint a duplicate. If the ticket is a **container/epic**, move it to your **epics** status — the validator's `proposed_action: move_to_epics` (not Todo — Todo would put shipped code back in the build queue; not Done — that closes a container over live children AND forward-cascades every one of them to Done). If it is a leaf whose remainder is fully carved, → Done.
- Nothing shipped / absent / reverted → **Todo**.
- Genuinely cannot determine → **UNCERTAIN: 🔎 note, flag for human, NO status change** (it stays exactly where it is). This is not bucket 3 — it is the absence of a verdict. Never bounce to Todo on a failed search, an unreachable tracker, or an unresolved deploy model.

**There is no `live=na` bucket.** "I could not confirm the deploy target" is not a reason to leave something in Deployed — resolve the deploy model (step 3; every repo has a knowable one) or mark it UNCERTAIN and flag it. A `live=na` escape hatch parks tickets indefinitely; we retired ours for exactly that reason.

## Entry point: `/flushdeployed <project>`

`<project>` is fuzzy (e.g. `studio` → your "Studio"-named project). No arg → ask which project.

## Defaults — JUST RUN IT, don't prompt
This skill has fixed defaults. When invoked, proceed with them silently — do NOT open a preamble of clarifying questions:
- Verified-live ticket that **meets its criteria** → **move to Done with an evidence note** (bucket 1).
- Run size → **all Deployed tickets in one sweep** (not a pilot).
- Bucket 2 is the only keep-Deployed. Everything else resolves to Done / split / Todo / epics — **except UNCERTAIN, which is comment-only and changes no status.** Failing to gather evidence is not a bucket; never let a failed lookup push a ticket to Todo.
Only stop to ask if the project name is ambiguous/unresolvable, or the user explicitly names a different mode in their message (read-only audit, pilot batch, keep-Deployed-instead-of-Done). Otherwise run the whole pipeline end-to-end and report.

## Pipeline

**1. Resolve the project + status names.**
`mcp__linear__list_projects {query:"<fuzzy>"}` → project name + team. `mcp__linear__list_issue_statuses {team:"<team>"}` → confirm the `Deployed`, `Done`, `Todo` names (Deployed is a *started*-type column here, not terminal).

**2. Pull every Deployed ticket.** `mcp__linear__list_issues {project, state:"Deployed", limit:100}` — paginate via `cursor` until `hasNextPage:false`. ⚠️ The result blows the tool token cap and is saved to a file; parse it with python (`json.loads`), don't Read it. The `id` field IS the identifier; `gitBranchName` gives the branch. Sort by `updatedAt` desc if you need a pilot batch.

**3. Establish prod ground truth (do this ONCE, in the canonical clones — not throwaway checkouts).** For each repo a ticket might touch:
```
git -C ~/code/<repo> fetch origin main --quiet
git -C ~/code/<repo> log -1 --format='%h %ci' origin/main
```
- **Auto-deploy repos** (e.g. Vercel/PaaS deploying `main`) → `origin/main == LIVE` (`deploy:"vercel-auto"`). Confirm via a `/health` git_sha or `vercel ls --prod`.
- **Manually-deployed repos** → get the live box HEAD (the deploy cutoff):
  `ssh <deploy-box> "cd /srv/<repo> && git rev-parse HEAD && git log -1 --format='%ci' HEAD"`
  A change is **live only if its merge commit is an ancestor of that box HEAD.** The box routinely lags `origin/main` by hours/commits (`deploy:"ec2-manual"`).
- **Several manual repos can share ONE box** — get each repo's own box HEAD (`ssh <box> "cd /srv/<repo> && git rev-parse HEAD"`). If `box HEAD == origin/main HEAD` that repo is caught up → merged == live; use `deploy:"ec2-manual"` with its own `live_box`.
- **Template repos** (a repo instantiated per-tenant, not a running site) → merged-to-main == shipped; use `deploy:"template"`.
- **Repos with no deploy target found** (not on the box, no PaaS config like `vercel.json`) → resolve the model before fan-out (`vercel project ls`, the registry, the host's process list). If it genuinely cannot be resolved, `deploy:"unknown"` → agents return UNCERTAIN, never a keep-Deployed.
- **Container-image deploys** (built to a registry, loaded on the host — no git on the box) → usually ancestry-provable after all: images are commonly tagged with the commit (`docker ps --format '{{.Names}}\t{{.Image}}'` → `…:sha-<short>`). Compare that sha to `origin/main`; if it matches, merged == LIVE and you pass `deploy:"ec2-manual"` with `live_box` = that commit. If the image sha is behind main, it is a real deploy lag → `MERGED_PENDING_DEPLOY`, and it deserves an ops ticket (Gotchas).
- **A repo can hide a non-git deploy target in a subdirectory** (e.g. a `wrangler.toml` Worker inside a backend repo, shipped by `wrangler deploy` not by the box) — box ancestry proves nothing about those paths. Grep for `wrangler.toml` / `Dockerfile` / `serverless.yml` before trusting a repo-level model, and live-probe such paths.
- **Package-registry repos** (publish to npm on a version bump) → liveness is the published version, not git: read the real package name from the repo's `package.json` (don't guess the scope — it 404s), then `npm view <name> dist-tags`. Live iff `latest` is at or past the bump that carried the change (`deploy:"npm"`).
- Quick recon of what's actually deployed on a shared box: `ssh <box> "ls -1d /srv/*; ps aux | grep -Ei 'uvicorn|gunicorn|node' | grep -oE '<repo-prefix>-[a-z]+' | sort -u"`.

**4. Fan out one validator agent per ticket** via the supporting workflow (read-only; agents propose, they don't write Linear):
```
Workflow({ scriptPath: "~/.claude/skills/flushdeployed/validate-workflow.js",
  args: { refs: {<repo>: {path, main, deploy, live_box?, box_date?}}, tickets: [{id,title,branch}] } })
```
Each agent: finds the merge in `origin/main` (`git log origin/main -i --grep="<ticket-id>"`), confirms the **actual change is present** (`git show origin/main:<path>` / `git grep`, not just a merge commit), checks live-ness (`git merge-base --is-ancestor <merge> <box>`), and returns a structured verdict.

Give **every** agent **all** repo refs (tickets cross repos). Big speed/accuracy win: **pre-route + pre-compute liveness before fan-out** — dump each repo's `git log origin/main --since=<project-start> --format='%H|%h|%ci|%s'` once, map every ticket→repo+merge in python by grepping the bare ticket-number in commit subjects, and pre-run `merge-base --is-ancestor <commit> <box>` for manual-deploy commits to know LIVE vs PENDING up front. Inject the result as a `⟪routing: …⟫` hint appended to each ticket's **title** in the workflow args (the validator builds its prompt from id/title/branch only, so title is the injection point). Agents still confirm change-present, but aim instantly.

**5. Review verdicts, then re-verify the decisive claim yourself** before any write: for every manual-deploy→Done ticket, independently run `git -C ~/code/<repo> merge-base --is-ancestor <merge> <box> && echo LIVE`. (zsh does NOT word-split unquoted vars — loop over a real array `arr=(a b c)`, not a string.)

**6. Apply Linear actions.** Map each verdict onto the three buckets above. Agents frequently propose `note_keep_deployed` for tickets that are plainly bucket 1 or bucket 3 — **override them**; the only `note_keep_deployed` you accept is a genuine bucket 2 (merged, provably not live).

| Verdict | Bucket | Action |
|---|---|---|
| DEPLOYED_LIVE + live=live + **criteria met** | **1** | `save_comment` ✅ evidence → `save_issue {state:"Done"}` |
| DEPLOYED_LIVE + live=live + **criteria NOT met** | **3** | live code, unfinished ticket — split (or → epics if it is a container with open children). **Never leave in Deployed.** |
| MERGED_PENDING_DEPLOY (on main / on `develop`, not live) + **criteria met** | **2** | `save_comment` ⏳ → **leave Deployed** — the one legitimate keep |
| MERGED_PENDING_DEPLOY + **criteria NOT met** | **3** | not-live code, unfinished ticket — treat as PARTIAL. **Never leave in Deployed.** |
| PARTIAL | **3** | remainder **Todo** (NO `parentId` on the original — see cascade guard), `save_comment` ✂️, original → Done. If children already cover the remainder, don't duplicate: container → epics (`proposed_action: move_to_epics`), leaf → Done |
| WRONG_NOT_DEPLOYED | **3** | `save_comment` ⚠️ → `save_issue {state:"Todo"}` |
| UNCERTAIN | — | `save_comment` 🔎, no status change, flag for human |

**How to write** (orchestrator keeps context lean by delegating trivial writes, but does the split by hand):
- **Do every PARTIAL split YOURSELF** (orchestrator, direct `save_issue`/`save_comment`) — never hand a split to an agent (split-write hazard, see Gotchas). Sequence: create remainder Todo → capture new id → ✂️ comment on original → original `{state:"Done"}`.
- **Delegate the mechanical Done/note writes** to `apply-done-workflow.js` (next to this file): `Workflow({scriptPath:"~/.claude/skills/flushdeployed/apply-done-workflow.js", args:{items:[{id, note, state}]}})`. `state:"Done"` for verified-live, **omit `state`** for keep-Deployed notes (⏳/✅-na/🔎). One trivial agent per ticket, `effort:low`, no analysis, no new tickets.
- **⚠️ AFTER applying, re-pull the Deployed column and ASSERT `count == expected-kept-set`** (`list_issues {state:"Deployed"}`; small enough to not blow the cap). Apply-agents freelance status — a note-only agent may silently also flip the ticket to Done and *lie* in `action_taken`. If the count is off, `get_issue` the missing ids and revert. **Do not trust agent-reported `action_taken`.**
- **⚠️ ALSO run the post-apply CASCADE SWEEP — the backstop that catches what the Deployed count structurally cannot.** `list_issues {team:"<team>", state:"Done", updatedAt:"-PT12M", limit:60}` (blows the cap → parse the saved file with python, never Read), **following the cursor until `hasNextPage:false`** — a busy 12-minute window can exceed one page, and a cascaded issue on page 2 escapes the sweep — then, across ALL pages, flag every issue whose `completedAt` falls inside your run window but whose id was **not in your apply set**. Each hit is a cascade candidate. Verify with `get_issue` → its `stateHistory` shows a bare `<prior state>→Done` at the parent's completion timestamp with no work behind it; restore to the snapshotted status per the Gotchas. This is a *second, independent* check — run it even when the pre-apply child snapshot was taken.

**7. Report** a verdict table + counts. Note the deploy lag: if pending tickets exist, a single manual deploy flushes them all to live at once.

## Gotchas (learned the hard way)
- **Workflow `args` arrives as a STRING** in the script — coerce: `const x = typeof args === 'string' ? JSON.parse(args) : args`.
- **`list_issues` exceeds the token cap** → it auto-saves to a file; parse with python, never Read.
- **zsh won't word-split `$var`** in `for` loops → `^{commit}` also trips extended-glob; use arrays + quote, or test with `git cat-file -t`.
- **Don't trust a merge commit alone** — confirm the fixed code is actually in `origin/main` (catches reverts/no-ops/scope-gaps → the PARTIAL cases).
- **Ops/infra tickets** (e.g. "install X on the box") aren't git-provable; look for a ticket comment documenting a verified manual prod action before calling them live, else UNCERTAIN.
- **Verified case moves to Done with an evidence note** (commit/PR# + file:line + live-basis) so the audit is auditable.
- **Split-write hazard (agents self-applying):** in `split_then_done`, agents reliably create the remainder ticket as Todo but then mis-apply the FINAL `state:Done` write to the NEW ticket instead of the original — seen 11/12 in one real run. **Fix: do the split in the orchestrator by hand (§6), never in an agent.** After any split, re-pull the remainder and confirm it's still `Todo`.
- **Parent-Done auto-completes split children (tracker automation, NOT an agent):** on one run, creating the remainder with `parentId = the original ticket` then marking the original **Done** caused Linear to silently cascade the brand-new remainder Todo → Done ~300ms later (visible in the remainder's `stateHistory`: Todo→Done at the parent's completion timestamp). Caught only because the post-apply **Todo**-column re-pull came back empty instead of N. **Fix: create split remainders with NO parent (or parent = a non-Done epic), never `parentId = the-about-to-be-Done original`.** If already flipped, set the remainder back to `Todo` **and** `parentId:null` so the cascade can't re-fire (the description already links the original for traceability). **Re-pull the Todo column after splits, not just Deployed** — assert it holds exactly the new remainder ids.
  - **The cascade also hits PRE-EXISTING children, not just split remainders you create.** Any non-completed sub-issue of a to-be-Done ticket gets silently completed — on one run that included a child **In Review→Done while its PR was still OPEN and another engineer was actively working it**. `action_taken` said nothing; the Deployed-column count was correct; only an explicit per-child check caught it.
  - **Mandatory pre-apply step: enumerate the children.** Call `list_issues {parentId:"<id>"}` once per ticket in your to-be-Done set (cheap, and — because it queries by parent rather than by project — no cross-project blind spot), and snapshot `{id, status, parentId}` for every non-completed child. After applying, re-`get_issue` each one and **restore any that flipped to its snapshotted status** (`In Review` stays `In Review` — do NOT blanket-restore to Todo). The cascade fires on the parent's completion transition, so a restore afterwards sticks and you can keep the real parent link. Snapshot immediately before the apply, not at the start of the sweep — concurrent sessions move tickets too.
  - **The Deployed-column count will NOT catch this, by construction** — the victim was never in Deployed. One run silently closed a long-standing *Backlog* bug ~430ms after its parent went Done, while every other assertion passed. Only the §6 cascade sweep surfaced it.
  - **Expect false positives from concurrent sessions — check scope before reverting anything.** Before touching a flagged id, confirm **both** that its `parentId` is in your to-be-Done set **and** that its `completedAt` trails that parent's by <2s. Otherwise leave it alone — it is someone else's work, not your cascade.
- **⚠️ CHILD→PARENT AUTO-CLOSE — the REVERSE cascade; the one the child snapshot structurally cannot catch.** The tracker also auto-completes a **PARENT** when its LAST open child is completed. Marking one ticket Done once silently flipped its parent — the board's standing **"Bulldozer 1-offs"** intake epic, whose whole job is to never close — to Done, silently destroying `/bulldozer`'s queue for that board. Every other assertion passed.
  - **Mandatory: after applying, `get_issue` the PARENT of every ticket you moved to Done** and assert none flipped to a completed state. Restore any that did to its prior status (check `stateHistory` for the pre-flip state).
  - The §6 sweep DOES surface these, but only if you inspect flagged rows with `parentId: null` instead of dismissing them as "another session's direct write" — a top-level standing epic has no parent, so it looks exactly like a false positive. Distinguish by checking whether the flagged id is the **parent of** one of your Done tickets.
- **⚠️ READ THE TICKET'S OWN ACCEPTANCE SECTION — AND ITS OPEN CHILDREN'S DESCRIPTIONS — BEFORE ANY Done WRITE.** A ticket whose code fix had genuinely shipped looked like a clean bucket 1, but (a) its own Verify bar required a *scheduled* run that *succeeded*, and there were none; and (b) its ops-tail child said in writing "do not close the parent until this is closed." **A child ticket can carry a "do not close the parent" clause that the parent's own body never mentions** — the child enumeration you already run for the cascade guard gives you those ids for free, so read their descriptions too.
- **⚠️ A RED CI CONCLUSION MAY BE THE MONITOR WORKING.** A staleness-monitor workflow showed 100% `failure`, which reads as "still broken." The failing step's log said the deploy pin was 6 commits behind main, then `exit 1` — the non-zero exit **is the alarm firing by design**. **For any alarm / monitor / gate / guard ticket, never grade on the CI conclusion — read the failing step's log** and check the run EVENT too (a `workflow_dispatch` success does not satisfy a criterion that says `event: schedule`).
- **A container/epic must never sit in Deployed.** If it has open children carrying the remainder, its home is your **epics** status. Moving it to Todo misrepresents shipped code; moving it to Done closes a container whose children are still open (and fires the forward cascade over all of them). On one run, moving a single container to epics instead of Done removed 15 of that run's 23 cascade exposures — **choosing the right bucket is also the cheapest cascade mitigation there is.**
- **File the deploy ops-ticket for any lag you find.** A bucket-2 keep with no owner just re-appears next sweep. File one ops ticket per lagging deploy target with `blocks:` pointing at the parked tickets, so they all point at the one action that frees them.
- **Args >~20KB blow the Workflow call.** Routing hints for ~50 tickets inline was 30KB. Fix: write per-ticket hint files to a scratch dir and patch a scratch COPY of `validate-workflow.js` to `cat ${hintDir}/${t.id}.md` as prompt step 0. Workflow *scripts* have no filesystem access, but the *agents* do — that asymmetry is the lever.
- **Apply-agents freelance STATUS even when told "note only":** a keep-Deployed agent has posted its comment correctly but ALSO silently moved the ticket to Done, then falsely reported `action_taken:"noted_kept_deployed"`. Caught only because the post-apply Deployed count came back one short. **ALWAYS re-pull the Deployed column after applying and assert `count == expected kept-set`; never trust `action_taken`.** (§6.)
- **Read-only validate agents mis-fill `action_taken` too:** in propose mode (`apply=false`) most agents return non-`none` values like `noted_kept_deployed` despite writing nothing. Before applying, confirm no writes actually happened (ticket `updatedAt` predates the run / no flushdeployed comment via `list_comments`). Drive apply decisions off `verdict`+`live`, not `action_taken`/`proposed_action`.
- **NO-HIT tickets** (ticket-id absent from every commit subject) are usually repo-standup or ops tickets, or squash-merges that reference a sibling id (e.g. `ABC-101/102` fails an `abc-102` regex). Hand these a manual hint; don't bounce them to Todo on a failed grep.
- **Workflow syntax check:** `node --check` flags a script's top-level `return`/`await` as "Illegal return" — that's expected (the harness wraps the body in an async fn). Verify by wrapping the body (minus `export const meta`) in `async function __w(){…}` before `node --check`.
- **Know each repo's deploy model — auto-deploy ≠ manual box.** A PaaS repo whose prod tracks `main` (confirm via a `/health` git_sha or `vercel ls --prod`) is zero-lag; a manual-deploy box lags. Tickets can cross repos with different models — give every agent all repo refs. And if a repo's branches flow `feature→develop→main`, a merge on `develop` only is NOT live (→ WRONG_NOT_DEPLOYED).
