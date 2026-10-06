export const meta = {
  name: 'flushdeployed-validate',
  description: 'Validate "Deployed" Linear tickets against prod code. Read-only (propose) by default; apply mode lets each agent perform its own Linear write.',
  phases: [{ title: 'Validate', detail: 'one agent per ticket: merge-in-main + change-present + live-vs-pending' }],
}

// args = {
//   apply?: bool,                         // false => propose only; true => each agent writes its own Linear change
//   linear?: {team, project, label, done, todo, deployed},   // required when apply=true
//   refs: { "<repo>": {path, main, deploy, live_box?, box_date?}, ... },
//   tickets: [{id,title,branch}, ...]
// }
// deploy: "vercel-auto" (main==live) | "ec2-manual" (live iff merge ancestor of live_box) | "template" (main==shipped)
//         | "npm" (live iff the published latest carries it; optional `published` pre-check string) | "unknown"
const input = typeof args === 'string' ? JSON.parse(args) : args
const refs = input.refs
const tickets = input.tickets
const apply = !!input.apply
const L = input.linear || {}

const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['id', 'verdict', 'proposed_action', 'action_taken', 'confidence', 'evidence', 'proposed_note'],
  properties: {
    id: { type: 'string' },
    title: { type: 'string' },
    repos: { type: 'array', items: { type: 'string' } },
    merge_evidence: { type: 'string' },
    change_present: { type: 'string', enum: ['yes', 'partial', 'no', 'na'] },
    live: { type: 'string', enum: ['live', 'pending', 'na'] },
    verdict: { type: 'string', enum: ['DEPLOYED_LIVE', 'MERGED_PENDING_DEPLOY', 'PARTIAL', 'WRONG_NOT_DEPLOYED', 'UNCERTAIN'] },
    proposed_action: { type: 'string', enum: ['move_to_done', 'note_keep_deployed', 'split_then_done', 'note_move_to_todo', 'move_to_epics', 'note_only_manual'] },
    action_taken: { type: 'string', enum: ['moved_to_done', 'noted_kept_deployed', 'split_done', 'noted_todo', 'moved_to_epics', 'noted_manual', 'write_failed', 'none'] },
    new_ticket_id: { type: 'string', description: 'id of the Todo created for a split, else ""' },
    proposed_note: { type: 'string' },
    split_remainder: { type: 'string' },
    evidence: { type: 'string', description: 'concise: commit hashes + file:line proving present/absent' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
}

function refsBlock() {
  const lines = Object.entries(refs).map(([repo, r]) => {
    if (r.deploy === 'vercel-auto') return `- ${repo} (${r.path}): origin/main = ${r.main}. Auto-deploys to Vercel on merge → origin/main == LIVE.`
    if (r.deploy === 'ec2-manual') return `- ${repo} (${r.path}): origin/main = ${r.main}. MANUAL deploy. LIVE box = ${r.live_box} (${r.box_date}). A change is LIVE only if its merge commit is an ANCESTOR of ${r.live_box}; in main but not ancestor = MERGED-NOT-DEPLOYED. (NOTE: if live_box == origin/main HEAD, the box is fully caught up — every merged change is LIVE.)`
    if (r.deploy === 'template') return `- ${repo} (${r.path}): origin/main = ${r.main}. TEMPLATE repo — not a deployed website. A change is "effective" (counts as shipped) once it is merged to origin/main (future instantiations consume main). Treat merged-to-main + change-present == LIVE; live basis = "on template main".`
    if (r.deploy === 'npm') return `- ${repo} (${r.path}): origin/main = ${r.main}. **PACKAGE PUBLISH** — publishes workspace packages to the registry on a version bump to main. NOT a website; liveness = the PUBLISHED registry version, not git. Procedure: read the real package name from \`git -C ${r.path} show origin/main:<package dir>/package.json\` (do not guess the scope — a wrong scope 404s), then \`npm view <name> dist-tags\`. LIVE iff the published \`latest\` is at or past the version bump that carried the change. Pre-checked this run: ${r.published || '(run npm view)'}.`
    return `- ${repo} (${r.path}): origin/main = ${r.main}. Deploy model uncertain — report "merged to main", set live=na, and return UNCERTAIN unless the ticket is plainly bucket 3. Do NOT over-claim LIVE, and do NOT propose keep-Deployed on an unresolved model.`
  })
  return `## Prod ground truth (objects ALREADY fetched; read ONLY via origin/main — DO NOT git fetch/checkout/pull or touch any working tree/index)\n${lines.join('\n')}`
}

function applyBlock() {
  if (!apply) return `## This is READ-ONLY. Do NOT write to Linear. Only return the proposed action + note; the orchestrator applies it.`
  return `## APPLY the result yourself in Linear (statuses: done="${L.done}", todo="${L.todo}"; team="${L.team}"; project="${L.project}"; label="${L.label}").
Load write tools: ToolSearch \`select:mcp__linear__save_issue,mcp__linear__save_comment\`. Then:
- move_to_done -> save_comment({issueId:id, body:<✅ note>}); save_issue({id, state:"${L.done}"}); action_taken="moved_to_done".
- note_keep_deployed -> save_comment only; action_taken="noted_kept_deployed".
- split_then_done -> ⚠️ **PREFERRED: do NOT apply this yourself — return proposed_action="split_then_done" with action_taken="none" and let the ORCHESTRATOR split by hand** (agents mis-target the final Done write, and a remainder created with parentId=the-about-to-be-Done original gets cascade-completed ~300ms later). If apply mode is nonetheless in force: save_issue({title:"[split from "+id+"] <remainder summary>", team:"${L.team}", project:"${L.project}", state:"${L.todo}", labels:["${L.label}"], description:<remainder, naming "+id+" in prose for traceability>}) — **NO parentId, ever** (or parent = a non-Done epic); capture new_ticket_id; save_comment({issueId:id, body:<✂️ note referencing new_ticket_id>}); save_issue({id, state:"${L.done}"}); action_taken="split_done". ⚠️ CRITICAL: the FINAL save_issue Done-write targets the ORIGINAL ticket's id ONLY (the id passed into this prompt). The freshly-created remainder ticket (new_ticket_id) MUST stay in "${L.todo}" — NEVER pass new_ticket_id to a state:"${L.done}" write. Two separate tickets: original→Done, remainder→Todo.
- note_move_to_todo -> save_comment({issueId:id, body:<⚠️ note>}); save_issue({id, state:"${L.todo}"}); action_taken="noted_todo".
- move_to_epics -> save_comment({issueId:id, body:<✂️ note naming the open children that carry the remainder>}); save_issue({id, state:<the resolved epics status>}); action_taken="moved_to_epics". **Resolve that status name first via mcp__linear__list_issue_statuses({team:"${L.team}"}) — do not hardcode it**; if the team has no such status, do NOT guess: leave the ticket alone, use note_only_manual and say so in evidence. Use this for a CONTAINER/EPIC whose remainder is already carved as open children: Done would close it over live children (and cascade-complete them), Todo would misrepresent shipped code, and Deployed is not a park.
- note_only_manual -> save_comment only; action_taken="noted_manual".
If any write throws, set action_taken="write_failed" and put the error in evidence. Idempotency: if a prior flushdeployed comment already exists on the ticket and the status already matches, do NOT duplicate — set action_taken="none".`
}

function prompt(t) {
  return `You are validating whether a Linear ticket marked **Deployed** is GENUINELY shipped to PROD code. Treat "Deployed" as a CLAIM to verify, not trust.

TICKET: ${t.id} — ${t.title}
Linear git branch: ${t.branch || '(none)'}

${refsBlock()}

## Steps
0. **THE THREE BUCKETS — decide which one this ticket is in.** (1) meets ALL its own acceptance criteria AND the change is LIVE IN PROD **per THIS repo's deploy model above** => DONE. NOTE: for a vercel-auto or template repo, merged-to-origin/main IS live — do NOT demand a box. (2) meets all its criteria but is NOT live in prod (manual/container repo where the merge is not an ancestor of the running box HEAD/image sha, or a commit sitting on a staging/develop branch the deploy target does not serve) => keep Deployed — **this is the ONLY legitimate keep-Deployed**. (3) anything else => split and/or back to Todo. Live-code-but-unfinished-ticket is bucket 3, NOT a keep. There is no "deploy target unconfirmed" keep — resolve the model or return UNCERTAIN.
1. Read the full ticket: ToolSearch \`select:mcp__linear__get_issue,mcp__linear__list_comments\`; get_issue({id:"${t.id}", includeRelations:true}) and list_comments({issueId:"${t.id}"}) (PR links, prior flushdeployed notes, earlier splits). **If Linear is unreachable you CANNOT grade acceptance criteria (1a) or check children for a blocking clause (1b) — so you may NOT return DEPLOYED_LIVE or any status-changing action. Return UNCERTAIN, record the failed read in evidence, and let a human look.** Proceed from title + git only to enrich that UNCERTAIN note.
   **1a. Quote the ticket's own "Done when" / "Verify" / "Acceptance" section and grade EACH bullet separately.** "The PR merged" is NOT the bar. An ops ticket whose criterion is "observe a green scheduled run" is not done because the code that would produce that run merged. If any bullet is unmet, this is PARTIAL or UNCERTAIN — never DEPLOYED_LIVE.
   **1b. List the ticket's open children (\`list_issues {parentId:"${t.id}"}\`) and READ THEIR DESCRIPTIONS.** A child can carry an explicit "do not close the parent until this is closed" clause that the parent's own body never mentions. If you find one and that child is still open, the parent is NOT done — say so in evidence. Also note whether the children already cover the unshipped remainder, in which case a split needs no NEW ticket.
2. Find the merge in prod. For each repo: git -C <repo.path> log origin/main -i --grep="${t.id}" --oneline | head -20 ; also grep the branch slug and the bare dev number. Record merge commit + PR#.
3. Confirm the actual CHANGE is present (guard reverts/no-ops/scope-gaps): git -C <repo.path> show origin/main:<path> and/or git -C <repo.path> grep -n "<distinctive string>" origin/main -- <path>. A merge commit alone is NOT sufficient.
4. Liveness by deploy model of the repo the change landed in:
   - vercel-auto / template: change present in origin/main ⇒ LIVE (no further check).
   - ec2-manual: git -C <repo.path> merge-base --is-ancestor <merge> <live_box> && echo LIVE || echo PENDING. (If live_box == origin/main HEAD the box is caught up ⇒ any merged change is LIVE.) ⚠️ **EXCEPTION — check the changed PATHS first.** A repo can hide a non-git deploy target in a subdirectory (e.g. a \`wrangler.toml\` Worker shipped by \`wrangler deploy\`), so box ancestry proves NOTHING about those paths. If the diff touches such a path, run a live probe against its endpoint or return UNCERTAIN.
   - npm: follow the procedure in the ground-truth block above.
4b. **If this is an alarm / monitor / gate / guard / CI ticket: never grade on the run's CONCLUSION — read the failing step's log.** A red run can be the alarm FIRING CORRECTLY on a real problem (\`exit 1\` is how a monitor reports). Conversely a green run can be vacuous. Decide from the log, and check whether the run EVENT matters too (a \`workflow_dispatch\` success does not satisfy a criterion that says \`event: schedule\`).
5. Verdict (CONSERVATIVE):
   - DEPLOYED_LIVE — merge found + change present + LIVE per the model (vercel-auto / template merged-to-main; OR ec2-manual merge ⊑ live_box; OR npm latest carries it) **AND every bullet of the ticket's own Done-when/Verify section from step 1a is met**. Live code with an unmet acceptance bullet is PARTIAL (bucket 3), never DEPLOYED_LIVE.
   - MERGED_PENDING_DEPLOY — change in origin/main but NOT yet live (ec2-manual not-ancestor-of-live_box, or npm latest predates the bump).
   - PARTIAL — only some described scope in prod, OR the code is live but an acceptance bullet is unmet; a concrete remainder is unshipped. If that remainder is ALREADY carved as open child tickets, do not propose a new split: propose move_to_epics for a container/epic, or move_to_done for a leaf whose remainder is fully covered.
   - WRONG_NOT_DEPLOYED — use ONLY when you POSITIVELY confirmed the change is ABSENT from origin/main (inspected the cited file/path; the fix is not there / was reverted). A failed merge-search ALONE is NOT enough.
   - UNCERTAIN — cannot conclude: no merge found AND you can't confirm absence; ops-only ticket needing a live host check; ambiguous scope. (A ticket comment documenting a VERIFIED manual prod action with proof can upgrade an ops ticket to DEPLOYED_LIVE.) **When unsure between WRONG and UNCERTAIN, pick UNCERTAIN — never bounce a ticket to Todo on a failed search alone.**

Note bodies (concise, EVIDENCE-DENSE — commit hash, PR#, file:line):
- ✅ "flushdeployed verified — <repo merge/PR#>, <file:line in origin/main>, <live basis>."
- ⏳ "flushdeployed — merged to <repo> origin/main (<commit/PR#>) but NOT yet on the box (<live_box>, <box_date>). Live on next deploy."
- ✂️ "flushdeployed split — shipped part verified live (<evidence>); remainder split to <new id>: <one-liner>."
- ⚠️ "flushdeployed — NOT verified in prod. Checked <repos/paths>; <what is absent>. Moving back to Todo."
- 🔎 "flushdeployed — could not auto-verify: <why>. Needs manual check: <what>."

${applyBlock()}

Return the structured record. Set confidence honestly.`
}

phase('Validate')
const results = await parallel(
  tickets.map((t) => () => agent(prompt(t), { label: `${apply ? 'apply' : 'check'}:${t.id}`, phase: 'Validate', schema: SCHEMA, effort: 'high' }))
)
return results.filter(Boolean)
