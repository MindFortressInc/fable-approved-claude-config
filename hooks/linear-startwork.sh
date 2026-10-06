#!/bin/bash
# linear-startwork.sh — PostToolUse hook on Bash for Claude Code.
# When a branch carrying a Linear ticket token (e.g. `dev-NNN`) is CREATED,
# take the linked ticket automatically:
#   - Flip a NOT-STARTED state (backlog/unstarted/triage) -> In Progress.
#     Never regresses In Review/Deployed/Done — avoids the tracker-automation
#     race that re-opens merged tickets.
#   - Assign yourself ONLY if the ticket is unassigned. If someone else holds
#     it, touch nothing and emit a heads-up so you confirm before taking it.
# Fires on branch CREATION only — `checkout -b/-B`, `switch -c/-C`, bare
# `git branch <new>`, and `git worktree add ... -b/-B <branch>` (the
# worktree-per-ticket workflow's create verb) — never plain checkout of an
# existing branch. Any error / missing dep / missing config -> silent no-op
# (exit 0): the hook never blocks or breaks the session.
#
# Config via environment (the hook no-ops unless these are set, so it is safe
# to vendor unconfigured):
#   LINEAR_API_KEY              your Linear personal API key, OR
#   LINEAR_KEY_FILE            path to a JSON file holding .env.LINEAR_API_KEY
#   LINEAR_DEV_TEAM_ID         UUID of the team whose issues these branches map to
#   LINEAR_INPROGRESS_STATE_ID UUID of the workflow state to move the ticket into
#   LINEAR_ASSIGNEE_ID         your Linear user UUID (assigned only when unassigned)
#   LINEAR_BRANCH_PREFIX       ticket token prefix in branch names (default: dev)
# (Find the UUIDs with get_team / list_issue_statuses / get_user in the Linear MCP,
#  or the GraphQL API.) Pairs with branch-name-gate.sh (PreToolUse), which enforces
# the canonical branch name at create so the PR links and this hook can fire.
#
# Install in ~/.claude/settings.json:
#   "hooks": { "PostToolUse": [ { "matcher": "Bash", "hooks": [
#     { "type": "command", "command": "~/.claude/hooks/linear-startwork.sh", "timeout": 10 }
#   ] } ] }

set +e
TEAM="${LINEAR_DEV_TEAM_ID:-}"
INPROGRESS="${LINEAR_INPROGRESS_STATE_ID:-}"
ASSIGNEE="${LINEAR_ASSIGNEE_ID:-}"
PREFIX="${LINEAR_BRANCH_PREFIX:-dev}"
API="https://api.linear.app/graphql"

command -v jq   >/dev/null 2>&1 || exit 0
command -v curl >/dev/null 2>&1 || exit 0
[ -z "$TEAM" ] && exit 0          # unconfigured -> no-op

input=$(cat)
cmd=$(jq -r '.tool_input.command // ""' <<<"$input")
[ -z "$cmd" ] && exit 0

# Detect on the command STRUCTURE, not string data: commit messages
# (`-m "…"`, `-F - <<EOF …`) and echoed text routinely contain literal
# "git checkout -b …" that must NOT be read as a real branch creation. Drop the
# heredoc body (everything from the first <<) and quoted spans before scanning.
scan="${cmd%%<<*}"
scan=$(printf '%s' "$scan" | sed -E "s/\"[^\"]*\"//g; s/'[^']*'//g")

# Only act on branch CREATION verbs.
newbranch=$(grep -oiE '(checkout +-[bB]|switch +-[cC]) +[^ ;&|]+' <<<"$scan" | head -1 | awk '{print $NF}')
# `git worktree add ... -b/-B <branch>` — the worktree workflow's create verb.
# -b may sit before or after the path, so grab the token after the flag.
if [ -z "$newbranch" ] && grep -qiE 'worktree +add' <<<"$scan"; then
  newbranch=$(grep -oiE ' -[bB] +[^ ;&|]+' <<<"$scan" | head -1 | awk '{print $NF}')
fi
if [ -z "$newbranch" ]; then
  newbranch=$(grep -oiE 'git +branch +[^-][^ ;&|]*' <<<"$scan" | head -1 | awk '{print $NF}')
fi
[ -z "$newbranch" ] && exit 0

num=$(grep -oiE "${PREFIX}-[0-9]+" <<<"$newbranch" | head -1 | grep -oE '[0-9]+')
[ -z "$num" ] && exit 0

# Resolve repo dir (explicit cd / git -C wins, else hook cwd) and confirm the
# branch actually exists now (the create succeeded).
dir=$(jq -r '.cwd // ""' <<<"$input")
explicit=$(grep -oE '(cd|git -C) [^ ;&|]+' <<<"$cmd" | head -1 | awk '{print $NF}')
[ -n "$explicit" ] && [ -d "${explicit/#\~/$HOME}" ] && dir="${explicit/#\~/$HOME}"
git -C "$dir" rev-parse --verify --quiet "refs/heads/$newbranch" >/dev/null 2>&1 || exit 0

# Resolve the API key: env var first, else a configured JSON file.
key="${LINEAR_API_KEY:-}"
if [ -z "$key" ] && [ -n "${LINEAR_KEY_FILE:-}" ]; then
  key=$(jq -r '.env.LINEAR_API_KEY // ""' "$LINEAR_KEY_FILE" 2>/dev/null)
fi
[ -z "$key" ] && exit 0

emit() { jq -n --arg m "$1" '{systemMessage:$m}'; }

# --- read current ticket state + assignee ---
rq="query { issues(filter: { number: { eq: ${num} }, team: { id: { eq: \"${TEAM}\" } } }) { nodes { id identifier state { type } assignee { id name } } } }"
rresp=$(curl -s --max-time 8 -X POST "$API" \
  --config <(printf 'header = "Authorization: %s"\n' "$key") -H "Content-Type: application/json" \
  -d "$(jq -n --arg q "$rq" '{query:$q}')")

node=$(jq -c '.data.issues.nodes[0] // empty' <<<"$rresp" 2>/dev/null)
if [ -z "$node" ]; then
  emit "Note: created branch $newbranch but couldn't find the matching ticket in that team — left the tracker untouched."
  exit 0
fi

iid=$(jq -r '.id' <<<"$node")
ident=$(jq -r '.identifier' <<<"$node")
stype=$(jq -r '.state.type' <<<"$node")
aid=$(jq -r '.assignee.id // ""' <<<"$node")
aname=$(jq -r '.assignee.name // ""' <<<"$node")

# state flip only from a not-started state
flip=0
case "$stype" in backlog|unstarted|triage) flip=1 ;; esac

# assign only when unassigned; flag a conflict when held by someone else
assign=0; other=""
if [ -z "$INPROGRESS" ]; then flip=0; fi          # no target state configured -> don't flip
if [ -z "$ASSIGNEE" ]; then assign=0
elif [ -z "$aid" ]; then assign=1
elif [ "$aid" != "$ASSIGNEE" ]; then other="$aname"; flip=0   # held by someone else: touch nothing, just alert
fi

fields=$(jq -n --argjson flip "$flip" --arg sid "$INPROGRESS" \
               --argjson assign "$assign" --arg aid "$ASSIGNEE" \
  '{} + (if $flip==1 then {stateId:$sid} else {} end)
      + (if $assign==1 then {assigneeId:$aid} else {} end)')

ok=1
if [ "$fields" != "{}" ]; then
  mq='mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success } }'
  mresp=$(curl -s --max-time 8 -X POST "$API" \
    --config <(printf 'header = "Authorization: %s"\n' "$key") -H "Content-Type: application/json" \
    -d "$(jq -n --arg q "$mq" --arg id "$iid" --argjson input "$fields" '{query:$q,variables:{id:$id,input:$input}}')")
  [ "$(jq -r '.data.issueUpdate.success // false' <<<"$mresp" 2>/dev/null)" = "true" ] || ok=0
fi

# compose what changed
did=""
[ "$flip" = 1 ] && did="$ident → In Progress"
[ "$assign" = 1 ] && did="${did:+$did, }assigned to you"

if [ "$ok" = 0 ]; then
  emit "⚠️ Tried to take $ident on branch create but the tracker update failed (API error) — set it manually."
elif [ -n "$other" ]; then
  emit "⚠️ Heads up — $ident is assigned to ${other}. I did NOT reassign it to you.${did:+ (Set $did.)} Making sure you want to take it before continuing?"
elif [ -n "$did" ]; then
  emit "✅ $did (auto, on branch create)."
fi
exit 0
