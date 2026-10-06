#!/usr/bin/env bash
# check-careful.sh — PreToolUse hook on Bash.
#
# Goal: stop nagging on routine cleanup, and when it DOES ask, ask in plain
# English a human can actually answer.
#
# For `rm -r`: every delete target is classified. If all targets are
# routine/regenerable (virtualenvs, build caches, test DBs, temp/log files)
# the command is ALLOWED SILENTLY. If any target isn't recognized, the prompt
# lists each item with a ✓ (routine) or ⚠ (please check) and a plain label —
# not a regex verdict. Other rare-but-catastrophic commands (true force-push,
# SQL DROP/TRUNCATE, kubectl delete, docker prune) get a plain-English warning.
#
# Deletes NEVER block a goal/loop. Because a /goal is an undetectable Stop hook
# (no env/stdin/file signal), the hook can't know whether a loop is driving it —
# so it never prompts on a delete in ANY context: recognized-safe deletes run
# silently; an unrecognized ⚠ delete is DEFERRED (not run) and appended as JSON
# to ~/.claude/cleanup-needed.log for a later cleanup sweep (/cleanup, /wrapup,
# /PRlaunch resolve it; babysit surfaces it). The only interactive prompt left
# is for rare non-delete catastrophes (force-push, SQL DROP, kubectl/docker).
#
# Loop-mode (~/.claude/hooks/loop-mode): only affects those non-delete commands
# now — when armed, they auto-proceed instead of prompting. See loop-mode-arm.sh
# and cleanup-sweep.py.
#   enable:  ~/.claude/hooks/loop-mode-arm.sh [minutes]   (or: touch the file)
#   disable: rm ~/.claude/hooks/loop-mode
# Adapted from garrytan/gstack careful/bin/check-careful.sh.
set -euo pipefail

INPUT=$(cat)

# Extract the "command" field from tool_input (jq, python3, then naive grep).
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null || true)
if [ -z "$CMD" ]; then
  CMD=$(printf '%s' "$INPUT" | python3 -c 'import sys,json; print(json.loads(sys.stdin.read()).get("tool_input",{}).get("command",""))' 2>/dev/null || true)
fi
if [ -z "$CMD" ]; then
  CMD=$(printf '%s' "$INPUT" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*:[[:space:]]*"//;s/"$//' || true)
fi
if [ -z "$CMD" ]; then
  echo '{}'
  exit 0
fi

CMD_LOWER=$(printf '%s' "$CMD" | tr '[:upper:]' '[:lower:]')
WARN=""
WARN_IS_RM=false   # whether WARN came from the rm parser (deferrable cleanup)

# --- rm -r handling: delegate to the parser (quote/comment/newline aware) -
# careful-rm.py prints a plain itemized WARN when an rm -r targets anything not
# routine/regenerable, and nothing when every target is safe (or no gated rm).
# A bash word-loop can't be trusted here — it mis-reads comments, newlines, and
# `rm` mentioned inside quoted arguments.
RM_HELPER="$HOME/.claude/hooks/careful-rm.py"
if printf '%s' "$CMD" | grep -qE '(^|[;&|[:space:]])rm([[:space:]]|$)' 2>/dev/null; then
  if [ -f "$RM_HELPER" ]; then
    if ! WARN=$(printf '%s' "$CMD" | python3 "$RM_HELPER" 2>/dev/null); then
      # parser errored -> conservative gate only if a recursive rm is present
      WARN=""
      printf '%s' "$CMD" | grep -qE 'rm\s+(-[a-zA-Z]*r|--recursive)' 2>/dev/null \
        && WARN="This command includes a recursive delete (rm -r) I couldn't fully analyze — review the paths before approving."
    fi
  elif printf '%s' "$CMD" | grep -qE 'rm\s+(-[a-zA-Z]*r|--recursive)' 2>/dev/null; then
    WARN="This command includes a recursive delete (rm -r). Review the paths before approving."
  fi
  [ -n "$WARN" ] && WARN_IS_RM=true
fi

# --- Other rare-but-catastrophic commands (plain-English) ----------------
# True force-push only: --force-with-lease is the sanctioned safe variant.
if [ -z "$WARN" ] && printf '%s' "$CMD" | grep -qE 'git\s+push\s+[^;|&]*(--force([[:space:]]|$)|-f([[:space:]]|$))' 2>/dev/null \
  && ! printf '%s' "$CMD" | grep -q -- '--force-with-lease' 2>/dev/null; then
  WARN="This force-pushes and OVERWRITES the remote branch's history — anyone else's commits on that branch can be lost. (The safe version is --force-with-lease.)"
fi
# SQL DROP/TRUNCATE only when a SQL client is being invoked.
if [ -z "$WARN" ] && printf '%s' "$CMD_LOWER" | grep -qE '(psql|mysql|sqlite3)\b' 2>/dev/null \
  && printf '%s' "$CMD_LOWER" | grep -qE 'drop\s+(table|database)|truncate\s' 2>/dev/null; then
  WARN="This runs a SQL DROP/TRUNCATE — it permanently deletes a table (or all of its rows) from the database."
fi
if [ -z "$WARN" ] && printf '%s' "$CMD" | grep -qE 'kubectl\s+delete' 2>/dev/null; then
  WARN="This deletes Kubernetes resources — it could take down something that's running (possibly in production)."
fi
if [ -z "$WARN" ] && printf '%s' "$CMD" | grep -qE 'docker\s+(rm\s+-f|system\s+prune)' 2>/dev/null; then
  WARN="This force-removes Docker containers or prunes images/volumes — running containers or cached data can be lost."
fi

# --- Credential redaction --------------------------------------------------
# cleanup-needed.log stores the full verbatim command text of every deferred
# delete, so a command that happened to carry a token inline would otherwise
# park a live credential in plaintext on disk (we hit this: a GitHub PAT did
# exactly that). Scrub credential-shaped strings before anything is persisted.
# Covered: GitHub fine-grained PATs, classic GitHub tokens (ghp_/gho_/ghu_/
# ghs_/ghr_), sk- API keys, AWS access key IDs (AKIA, permanent) and AWS STS
# temporary access-key IDs (ASIA), Authorization Bearer/token headers. The
# leading (^|[^A-Za-z0-9_]) guard keeps short prefixes like sk- from matching
# inside ordinary words (e.g. "task-...").
redact_secrets() {
  printf '%s' "$1" | sed -E \
    -e 's/github_pat_[A-Za-z0-9_]{20,}/***REDACTED***/g' \
    -e 's/(^|[^A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}/\1***REDACTED***/g' \
    -e 's/(^|[^A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}/\1***REDACTED***/g' \
    -e 's/(^|[^A-Za-z0-9_])(AKIA|ASIA)[0-9A-Z]{16}/\1***REDACTED***/g' \
    -e 's/([Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn]:?[[:space:]]*([Bb][Ee][Aa][Rr][Ee][Rr]|[Tt][Oo][Kk][Ee][Nn])[[:space:]]+)[^[:space:]"'"'"']+/\1***REDACTED***/g'
}

# --- Output --------------------------------------------------------------
LOOPMODE_FILE="$HOME/.claude/hooks/loop-mode"
CLEANUP_LOG="$HOME/.claude/cleanup-needed.log"

# loop-mode active = file exists AND unexpired. Content: empty/non-numeric =>
# armed indefinitely (manual touch); numeric epoch => armed until it passes
# (expired => self-disarm so a leftover never poisons an interactive session).
loop_mode_active() {
  [ -f "$LOOPMODE_FILE" ] || return 1
  local content now
  content=$(tr -d '[:space:]' < "$LOOPMODE_FILE" 2>/dev/null || true)
  case "$content" in
    ''|*[!0-9]*) return 0 ;;
  esac
  now=$(date +%s 2>/dev/null || echo 0)
  [ "$now" -lt "$content" ] && return 0
  rm -f "$LOOPMODE_FILE" 2>/dev/null || true
  return 1
}

if [ -z "$WARN" ]; then
  echo '{}'
  exit 0
fi

# A DELETE NEVER BLOCKS a goal/loop. The hook can't tell whether a /goal or
# /loop is driving it (a /goal is an undetectable Stop hook), so the only
# guarantee is to never prompt on a delete in ANY context: an unrecognized rm is
# DEFERRED (not run) and queued for a cleanup sweep, and the run continues.
# Recognized-safe deletes already returned `{}` above and ran silently.
if [ "$WARN_IS_RM" = true ]; then
  NOW=$(date +%s 2>/dev/null || echo 0)
  CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null || true)
  # Never persist raw credentials: redact the command text, the itemized
  # warning derived from it, and the cwd (a scratch dir can be named after a
  # token) before any of it lands in the cleanup queue.
  SAFE_CMD=$(redact_secrets "$CMD" 2>/dev/null) || SAFE_CMD="[careful] redaction failed — command text withheld"
  SAFE_WARN=$(redact_secrets "$WARN" 2>/dev/null) || SAFE_WARN=""
  SAFE_CWD=$(redact_secrets "$CWD" 2>/dev/null) || SAFE_CWD="[careful] redaction failed — cwd withheld"
  # Flag path-bearing fields that redaction altered: cleanup-sweep.py's
  # extract_targets() resolves delete targets against cmd/cwd, so a redacted
  # value no longer describes a real filesystem path — the sweep must refuse
  # to auto-run it (see _redacted_path_field in cleanup-sweep.py).
  REDACTED_PATH_FIELD=false
  if [ "$SAFE_CMD" != "$CMD" ] || [ "$SAFE_CWD" != "$CWD" ]; then
    REDACTED_PATH_FIELD=true
  fi
  # Append THROUGH cleanup-sweep.py --append so this write and a concurrent
  # sweep's read-modify-write rewrite share one lock: a bare
  # `>> "$CLEANUP_LOG"` here is silently erased if a sweep in another session
  # rewrites the queue from a snapshot taken just before it. Falls back to the
  # bare append if the helper is missing/unusable — a queued-but-racy entry
  # beats a dropped one, and this hook must never fail a delete deferral.
  ENTRY=$(jq -nc --argjson ts "${NOW:-0}" --arg cwd "$SAFE_CWD" --arg cmd "$SAFE_CMD" --arg w "$SAFE_WARN" \
    --argjson redacted_path_field "$REDACTED_PATH_FIELD" \
    '{ts:$ts, cwd:$cwd, cmd:$cmd, reason:$w, _redacted_path_field:$redacted_path_field}' 2>/dev/null || true)
  if [ -n "$ENTRY" ]; then
    printf '%s\n' "$ENTRY" | python3 "$HOME/.claude/hooks/cleanup-sweep.py" --append 2>/dev/null \
      || printf '%s\n' "$ENTRY" >> "$CLEANUP_LOG" 2>/dev/null || true
  fi
  jq -n '{
    systemMessage: "[careful] Did not run an unrecognized delete — queued it for cleanup instead (run /cleanup to review/clear). This never blocks a goal or loop.",
    hookSpecificOutput: {hookEventName:"PreToolUse", permissionDecision:"deny", permissionDecisionReason:"[careful] Deferred this delete to ~/.claude/cleanup-needed.log — NOT executed (intentional; deletes never block a goal/loop). Safe to continue; do NOT retry. Review later with /cleanup."}
  }' 2>/dev/null && exit 0
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"[careful] deferred delete to cleanup queue; continue, do not retry"}}'
  exit 0
fi

# Non-delete flagged commands (force-push, SQL DROP, kubectl/docker): rare and
# catastrophic. Auto-proceed under loop-mode; otherwise ask. (This is the only
# remaining prompt; it does not fire on deletes.)
if loop_mode_active; then
  jq -n --arg cmd "$CMD" '{
    systemMessage: "[careful/loop-mode] Auto-proceeded a flagged non-delete command.",
    hookSpecificOutput: {hookEventName:"PreToolUse", permissionDecision:"allow", permissionDecisionReason:"[careful] loop-mode active — auto-proceeded (non-delete)"},
    additionalContext: ("loop-mode auto-approved a flagged command: " + $cmd + ".")
  }' 2>/dev/null && exit 0
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
fi

jq -n --arg reason "[careful] $WARN" '{hookSpecificOutput:{hookEventName:"PreToolUse", permissionDecision:"ask", permissionDecisionReason:$reason}}' 2>/dev/null \
  || printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"[careful] destructive command — review before approving"}}\n'
