#!/bin/bash
# pr-gate.sh — PreToolUse hook on Bash for Claude Code.
# Blocks `gh pr create` unless the PRlaunch gates passed for this exact
# repo+branch+HEAD. The evidence is a per-gate JSON ledger:
#   ~/.claude/prlaunch-ok/<repo>--<branch-slug>.json  (written by prlaunch-gate.sh)
# whose exact path this hook ASKS for via `prlaunch-gate.sh path` rather than
# rebuilding -- the two must never be able to drift apart.
# recording, per gate (deep_review/cr_cli/outcome_eval/tests), the HEAD it ran
# against; `prlaunch-gate.sh check` requires all four present AND at current HEAD.
# Any commit after a gate ran changes HEAD and re-blocks (re-gate rule).
# Back-compat: a legacy plain-sha marker file (no .json suffix) that matches HEAD
# is accepted with a migration warning while you transition to the ledger.
# Escape hatch: include PRLAUNCH_SKIP=1 in the command (owner-authorized only).
#
# Optional tracker-link gate: the PR must attach to a ticket (branch carries
# <prefix>-NNN, or the command body carries it, e.g. "Closes <PREFIX>-NNN").
# The ticket-token prefix defaults to `dev` (LINEAR_BRANCH_PREFIX to change it).
# LINEAR_SKIP=1 bypasses it for genuinely ticket-less PRs (infra/config repos).
#
# Install in ~/.claude/settings.json:
#   "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [
#     { "type": "command", "command": "~/.claude/hooks/pr-gate.sh", "timeout": 10 }
#   ] } ] }

PREFIX="${LINEAR_BRANCH_PREFIX:-dev}"
UPREFIX=$(printf '%s' "$PREFIX" | tr '[:lower:]' '[:upper:]')

input=$(cat)
cmd=$(jq -r '.tool_input.command // ""' <<<"$input")

# ---------------------------------------------------------------------------
# TRIGGER DETECTION
#
# Detect on the command STRUCTURE, not on its text. A bare substring test over
# the whole command denied ANY command that merely CONTAINED the phrase, naming
# an action the user never attempted. Docs, commit messages and ticket bodies
# talk about the PR-create verb constantly, so the false positive landed exactly
# on the work that touches this machinery: a commit message quoting it was
# refused, and so was a heredoc writing a file whose contents quoted it.
#
# Stripping quoted spans with a per-line sed cannot see a `"$(cat <<'EOF' … EOF)"`
# body whose quotes span lines. Hence two steps here: remove the spans bash
# itself treats as DATA (heredoc bodies, then quoted spans), then require the
# verb to sit at a real COMMAND POSITION.
#
# Fail-closed is preserved: text is removed only where bash would not execute
# it, and the anchor accepts every position bash would actually start a command
# at — line start, after a separator, behind `VAR=value` / wrapper-word prefixes.
# ---------------------------------------------------------------------------

# Fast path, pure bash, no subprocess: every real invocation contains gh, pr and
# create in that order, so anything else needs no analysis at all. Deliberately
# a loose superset of the anchored regex below — it must never reject a command
# the real trigger would have matched.
case "$cmd" in
  *gh*pr*create*) ;;
  *) exit 0 ;;
esac

# The code-only projection comes from the shared walk in shell-code-only.sh
# (same library branch-name-gate.sh and linear-startwork.sh use): quoted spans,
# heredoc bodies, here-strings and comments are DATA and are dropped, while a
# heredoc opened inside `"$(…)"` is still recognised and code AFTER a heredoc is
# still scanned. If the library is missing, fall back to scanning the WHOLE
# command: that can only over-trigger (deny), never let a real invocation slip.
lib="$(dirname "$0")/shell-code-only.sh"
# shellcheck source=hooks/shell-code-only.sh
[ -r "$lib" ] && . "$lib"
declare -F shell_code_only >/dev/null 2>&1 \
  || shell_code_only() { printf '%s\n' "$1"; }

scan=$(shell_code_only "$cmd")

# A real invocation STARTS a command: at the beginning of a line, or right after
# a separator (`;` `&` `|` `(` `)` `{` `}` `!`), optionally behind any run of
# `VAR=value` assignments, leading redirections (`>/tmp/out gh pr create …`) or
# command-introducing words (`if`, `then`, `sudo`, `time`, …). grep is
# line-oriented, so `^` also covers newline as a separator — which is why the
# heredoc bodies had to go first.
TRIGGER_RE='(^|[;&|(){}!])[[:space:]]*(([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*|[0-9]*[<>]{1,2}&?[[:space:]]*[^[:space:];&|<>]*|if|then|else|elif|while|until|do|time|exec|command|sudo|nohup|env)[[:space:]]+)*gh[[:space:]]+pr[[:space:]]+create([[:space:];&|)}]|$)'
grep -qE "$TRIGGER_RE" <<<"$scan" || exit 0

# Escape hatches read `$scan`, not `$cmd`: a PR body that DESCRIBES the hatch
# ("PRLAUNCH_SKIP=1 exists for emergencies") is prose, not a use of it, and must
# not disarm the gate.
[[ "$scan" == *"PRLAUNCH_SKIP=1"* ]] && exit 0

deny() {
  jq -n --arg r "$1" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

# Repo dir: explicit `cd <dir>` / `git -C <dir>` in the command wins, else hook cwd
dir=$(jq -r '.cwd // ""' <<<"$input")
# Resolve it from the LAST `cd` BEFORE the trigger — that is the directory the
# PR is actually created from. `cd /tmp && cd repo && …` runs in `repo`, and a
# trailing `&& cd /elsewhere` runs only after the PR already exists. `git -C`
# is only a fallback: it runs one command elsewhere without moving the shell,
# so it must never override an actual cd.
#
# This is a text heuristic, not a shell parser: it cannot see control flow, so
# `cd /a || cd /b && …` and a subshell-scoped `(cd /b && true) && …` can still
# pick the wrong directory. That is tolerable because it fails toward DENYING
# (the wrong repo/branch almost never has a ledger at this HEAD), and the deny
# message names the repo it resolved, so the fix is obvious. Replacing the
# heuristic with an explicit repo-dir signal is tracked separately.
#
# Cut at the position the trigger ACTUALLY matched, over the same `$scan` the
# match was made on. Cutting at the first textual occurrence let a mention
# earlier in the command truncate the search before the real invocation and
# resolve the wrong directory ("cannot resolve a git repo from '$HOME'").
before_trigger=$(awk -v re="$TRIGGER_RE" '
  { if (match($0, re)) { print substr($0, 1, RSTART - 1); exit } print }
' <<<"$scan")
explicit=$(grep -oE '(^|[ ;&|(])cd [^ ;&|]+' <<<"$before_trigger" | tail -1 | awk '{print $NF}')
[[ -n "$explicit" ]] \
  || explicit=$(grep -oE 'git -C [^ ;&|]+' <<<"$before_trigger" | tail -1 | awk '{print $NF}')
[[ -n "$explicit" && -d "${explicit/#\~/$HOME}" ]] && dir="${explicit/#\~/$HOME}"

toplevel=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) \
  || deny "PR gate: cannot resolve a git repo from '$dir'. Run gh pr create from the repo, or run /PRlaunch."
branch=$(git -C "$dir" branch --show-current)
head=$(git -C "$dir" rev-parse HEAD)
slug="${branch//\//-}"
gate_helper="$(dirname "$0")/prlaunch-gate.sh"

# ASK prlaunch-gate.sh for the ledger path instead of rebuilding it. This hook
# and that script have to agree on one file; when both derived it independently
# they agreed only by coincidence, and the coincidence was itself the bug --
# `basename "$(git rev-parse --show-toplevel)"` is the WORKTREE directory, which
# two repos can share.
repo=$(bash "$gate_helper" path --repo --repo-dir "$dir" 2>/dev/null)
ledger=$(bash "$gate_helper" path --repo-dir "$dir" 2>/dev/null)
# Fail-CLOSED fallback: if the helper can't answer, fall back to the old
# derivation. That can only mislead us about whether to ENTER the ledger branch
# below -- the verdict itself comes from `$gate_helper check`, which re-resolves
# the identity itself, so a wrong path here denies rather than allows.
[[ -n "$repo" ]] || repo=$(basename "$toplevel")
[[ -n "$ledger" ]] || ledger="$HOME/.claude/prlaunch-ok/${repo}--${slug}.json"
# Legacy plain-sha marker stays on the old dirname key -- the only key it was
# ever written under. It is safe there: it is accepted only when its content
# equals THIS repo's HEAD, and a foreign repo's sha never does.
marker="$HOME/.claude/prlaunch-ok/$(basename "$toplevel")--${slug}"

# Tracker link gate: the PR must attach to a ticket (branch carries <prefix>-NNN,
# or the body/title carries the ticket id). An unlinked PR joins the under-linked
# graveyard. LINEAR_SKIP=1 for genuinely ticket-less PRs (config/infra repos).
#
# The ticket search stays on `$cmd` — the ticket reference lives in the PR
# title/body, i.e. in exactly the quoted/heredoc spans `$scan` strips — while the
# LINEAR_SKIP hatch reads `$scan`, for the same reason as PRLAUNCH_SKIP above.
if [[ "$scan" != *"LINEAR_SKIP=1"* ]]; then
  if ! grep -qiE "${PREFIX}-[0-9]+" <<<"$branch" && ! grep -qiE "${PREFIX}-[0-9]+" <<<"$cmd"; then
    deny "PR BLOCKED: this PR won't link to a tracker ticket. Name the branch me/${PREFIX}-NNN-... or put 'Closes ${UPREFIX}-NNN' in the body, so the tracker auto-attaches it. (LINEAR_SKIP=1 for genuinely ticket-less PRs.)"
  fi
fi

# Prefer the per-gate JSON ledger. When it exists it is authoritative: all four
# PRlaunch gates must be recorded at the current HEAD. Delegate to prlaunch-gate.sh
# `check` (it lives beside this hook) — it names the exact missing/stale gate and
# the prescriptive fix, which we surface verbatim in the deny reason.
if [[ -f "$ledger" ]]; then
  if reason=$(bash "$gate_helper" check --repo-dir "$dir" 2>&1); then
    exit 0
  fi
  deny "PR BLOCKED (PRlaunch ledger): $reason"
fi

# BACK-COMPAT: no JSON ledger, but a legacy plain-sha marker.
# Accept only if it matches HEAD, with a migration warning; a stale marker denies.
if [[ -f "$marker" ]]; then
  if [[ "$(cat "$marker")" == "$head" ]]; then
    jq -n '{systemMessage:"legacy PRlaunch marker accepted — migrate to the per-gate ledger (prlaunch-gate.sh)"}'
    exit 0
  fi
  deny "PR BLOCKED: $repo/$branch changed since PRlaunch gates passed (gated $(cut -c1-8 "$marker"), HEAD ${head:0:8}). Re-run the affected gates (/PRlaunch Phase 4) — green earlier ≠ the final version is green."
fi

deny "PR BLOCKED: no PRlaunch gate record for $repo/$branch. Run /PRlaunch (deep-review → CR CLI → outcome eval → re-gate) before opening a PR."
