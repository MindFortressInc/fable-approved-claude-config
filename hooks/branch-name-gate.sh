#!/bin/bash
# branch-name-gate.sh — PreToolUse hook on Bash for Claude Code.
# When a branch is CREATED, require it to carry the Linear ticket's EXACT
# canonical branch name, so the PR links AND the tracker's status automation
# fires (and the paired linear-startwork.sh can take the ticket):
#   - No ticket token (e.g. `dev-NNN`) -> DENY, tell the agent to use the
#     ticket's canonical gitBranchName (catches `quickfix`, which never links).
#   - token present but != canonical name -> DENY, hand back the exact name to
#     re-run with (catches `me/dev-NNN-quickfix`: links, but off-slug).
#   - == canonical name (or API/config unavailable) -> ALLOW (fail-open).
# Fires on branch CREATION only — `checkout -b/-B`, `switch -c/-C`, bare
# `git branch <new>`, and `git worktree add ... -b/-B <branch>` — never plain
# checkout. Include LINEAR_SKIP=1 in the command to bypass for genuinely
# ticket-less branches (infra/config repos). Fail-open: any missing dep / API
# error / timeout / missing config -> exit 0 (never blocks real work).
#
# Config via environment (the gate no-ops unless these are set):
#   LINEAR_API_KEY        your Linear personal API key, OR
#   LINEAR_KEY_FILE      path to a JSON file holding .env.LINEAR_API_KEY
#   LINEAR_DEV_TEAM_ID   UUID of the team whose issues these branches map to
#   LINEAR_BRANCH_PREFIX ticket token prefix in branch names (default: dev)
#
# Install in ~/.claude/settings.json:
#   "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [
#     { "type": "command", "command": "~/.claude/hooks/branch-name-gate.sh", "timeout": 10 }
#   ] } ] }

set +e
TEAM="${LINEAR_DEV_TEAM_ID:-}"
PREFIX="${LINEAR_BRANCH_PREFIX:-dev}"
API="https://api.linear.app/graphql"

input=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
cmd=$(jq -r '.tool_input.command // ""' <<<"$input" 2>/dev/null)
[ -z "$cmd" ] && exit 0
[ -z "$TEAM" ] && exit 0          # unconfigured -> no-op (don't gate)

# Detect on the command STRUCTURE, not string data: commit messages
# (`-m "…"`, `-F - <<EOF …`) and echoed text routinely contain literal
# "git checkout -b …" that must NOT be read as a real branch creation. Drop the
# spans bash treats as DATA — heredoc BODIES, here-strings, quoted spans,
# comments — and scan only what is left.
#
# This used to be `scan="${cmd%%<<*}"`, which truncated the command at the FIRST
# `<<`, so a branch create that FOLLOWED a heredoc was invisible here and the
# gate failed OPEN. The shared walk (shell-code-only.sh) holds both directions:
# code after a heredoc is scanned, a create-looking string inside the body is not.
lib="$(dirname "$0")/shell-code-only.sh"
# shellcheck source=hooks/shell-code-only.sh
[ -r "$lib" ] && . "$lib"
# Helper absent (a partial vendor of this hooks dir): fall back to the old
# stripper rather than scanning nothing, which would silently disable the gate.
declare -F shell_code_only >/dev/null 2>&1 \
  || shell_code_only() { printf '%s\n' "${1%%<<*}"; }
# Same story for the operand recovery — identity, not nothing.
declare -F unquote_plain_operands >/dev/null 2>&1 \
  || unquote_plain_operands() { printf '%s\n' "$1"; }
# Un-quote the create-flag operand FIRST. Both strippers drop a quoted span
# whole, so a quoted branch name never survived to the extraction below:
# `-b "me/dev-1-x" origin/main` arrived as `-b  origin/main` (denied, blaming the
# start point) and a trailing `-b "me/dev-1-x"` arrived as `-b ` (no operand —
# no create found, anything allowed). $cmd itself is left untouched; only what
# we SCAN is normalised.
scan=$(shell_code_only "$(unquote_plain_operands "$cmd")")
# A no-op on the walk's output (it already drops quoted spans); this is the line
# that strips them on the fallback path above.
scan=$(printf '%s' "$scan" | sed -E "s/\"[^\"]*\"//g; s/'[^']*'//g")

# Escape hatch (owner-authorized): ticket-less branches. Read from $scan, not the
# raw command: a heredoc body or commit message that MENTIONS the hatch is prose,
# not a use of it, and must not disarm the gate.
[[ "$scan" == *"LINEAR_SKIP=1"* ]] && exit 0

# Only act on branch CREATION verbs (same detection as linear-startwork.sh).
newbranch=$(grep -oiE '(checkout +-[bB]|switch +-[cC]) +[^ ;&|]+' <<<"$scan" | head -1 | awk '{print $NF}')
# `git worktree add ... -b/-B <branch>` — worktree workflow's create verb.
if [ -z "$newbranch" ] && grep -qiE 'worktree +add' <<<"$scan"; then
  newbranch=$(grep -oiE ' -[bB] +[^ ;&|]+' <<<"$scan" | head -1 | awk '{print $NF}')
fi
if [ -z "$newbranch" ]; then
  newbranch=$(grep -oiE 'git +branch +[^-][^ ;&|]*' <<<"$scan" | head -1 | awk '{print $NF}')
fi
[ -z "$newbranch" ] && exit 0

deny() {
  jq -n --arg r "$1" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

# Hard floor: the branch MUST carry a ticket token or the tracker can't link the PR.
num=$(grep -oiE "${PREFIX}-[0-9]+" <<<"$newbranch" | head -1 | grep -oE '[0-9]+')
if [ -z "$num" ]; then
  deny "Branch '$newbranch' has no ${PREFIX}-NNN token, so Linear can't link the PR to a ticket. Look up the ticket's exact branch name — get_issue(<ID>) → gitBranchName — and create from that (e.g. git checkout -b me/${PREFIX}-<n>-<slug>). Use LINEAR_SKIP=1 for genuinely ticket-less branches."
fi

# Need curl + key to verify the canonical name; if unavailable, fail-open.
command -v curl >/dev/null 2>&1 || exit 0
key="${LINEAR_API_KEY:-}"
if [ -z "$key" ] && [ -n "${LINEAR_KEY_FILE:-}" ]; then
  key=$(jq -r '.env.LINEAR_API_KEY // ""' "$LINEAR_KEY_FILE" 2>/dev/null)
fi
[ -z "$key" ] && exit 0

q="query { issues(filter: { number: { eq: ${num} }, team: { id: { eq: \"${TEAM}\" } } }) { nodes { identifier branchName } } }"
resp=$(curl -s --max-time 8 -X POST "$API" \
  --config <(printf 'header = "Authorization: %s"\n' "$key") -H "Content-Type: application/json" \
  -d "$(jq -n --arg q "$q" '{query:$q}')")
canonical=$(jq -r '.data.issues.nodes[0].branchName // ""' <<<"$resp" 2>/dev/null)

# API error / ticket not found -> fail-open (the token floor is enough to link).
[ -z "$canonical" ] && exit 0

if [ "$newbranch" != "$canonical" ]; then
  deny "Use Linear's exact branch name for this ticket: '$canonical' (you proposed '$newbranch'). Re-run: git checkout -b $canonical"
fi

exit 0
