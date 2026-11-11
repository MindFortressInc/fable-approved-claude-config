#!/bin/bash
# Tests for stash-guard.py -- run: bash hooks/stash-guard.test.sh
# Builds throwaway repos (a main checkout with one linked worktree, a solo repo
# with none, and a plain directory) and feeds the hook real PreToolUse payloads.
# Nothing here ever runs `git stash`; the hook is only asked to judge it.
HOOK="$(cd "$(dirname "$0")" && pwd)/stash-guard.py"
pass=0; fail=0
T=$(cd "$(mktemp -d)" && pwd -P); trap 'rm -rf "$T"' EXIT
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE

g() { git -c user.email=t@t -c user.name=t -c init.defaultBranch=main "$@" >/dev/null 2>&1; }
MAIN="$T/main"; WT="$T/main.wt"; SOLO="$T/solo"; PLAIN="$T/plain"
mkdir -p "$MAIN" "$SOLO" "$PLAIN"
for r in "$MAIN" "$SOLO"; do g -C "$r" init && echo x > "$r/f" && g -C "$r" add f && g -C "$r" commit -m init; done
g -C "$MAIN" worktree add "$WT" -b wt-branch
[ -f "$WT/.git" ] || { echo "setup failed: no linked worktree at $WT"; exit 1; }

# $1 tool_name, $2 command, $3 cwd -> a Claude Code PreToolUse payload
j() { python3 -c 'import json,sys; print(json.dumps({"session_id":"s1","transcript_path":"/x.jsonl","cwd":sys.argv[3],"hook_event_name":"PreToolUse","tool_name":sys.argv[1],"tool_input":{"command":sys.argv[2],"description":"d"}}))' "$@"; }

# $1 name, $2 command, $3 cwd, $4 expect ALLOW|DENY, $5 optional substring the deny reason must contain
check() {
  out=$(j Bash "$2" "$3" | python3 "$HOOK" 2>/dev/null); rc=$?
  if [ $rc -ne 0 ]; then fail=$((fail+1)); echo "FAIL: $1 (exit $rc)"; return; fi
  case "$4" in
    ALLOW) [ -z "$out" ] && { pass=$((pass+1)); echo "ok   ALLOW  $1"; return; } ;;
    DENY) echo "$out" | python3 -c '
import json,sys
d=json.load(sys.stdin)["hookSpecificOutput"]
assert d["hookEventName"]=="PreToolUse" and d["permissionDecision"]=="deny", d
assert sys.argv[1] in d["permissionDecisionReason"], d["permissionDecisionReason"]
' "${5:-}" 2>/dev/null && { pass=$((pass+1)); echo "ok   DENY   $1"; return; } ;;
  esac
  fail=$((fail+1)); echo "FAIL: $1 (expected $4)"; echo "  got: ${out:-<empty>}"
}

# --- mutating stash inside a linked worktree: denied
check "bare git stash in linked wt"         "git stash"                       "$WT" DENY "linked worktree"
check "git stash pop in linked wt"          "git stash pop"                   "$WT" DENY "git worktree add"
check "git -C <wt> stash push -- f"         "git -C $WT stash push -- f"      "$PLAIN" DENY "linked worktree"
check "stash apply"                         "git stash apply stash@{1}"       "$WT" DENY
check "stash drop"                          "git stash drop"                  "$WT" DENY
check "stash save"                          "git stash save 'wip'"            "$WT" DENY
check "stash clear"                         "git stash clear"                 "$WT" DENY
check "stash -u (implicit push)"            "git stash -u"                    "$WT" DENY
check "stash -- path (implicit push)"       "git stash -- f"                  "$WT" DENY
check "stash branch"                        "git stash branch x"              "$WT" DENY
check "reason names safe alternatives"      "git stash"                       "$WT" DENY "git show <sha>:<path>"

# --- chains, wrappers, prefixes
check "after && in chain"                   "git status && git stash"         "$WT" DENY
check "after ; in chain"                    "git status; git stash pop"       "$WT" DENY
check "after || in chain"                   "false || git stash"              "$WT" DENY
check "after pipe"                          "true | git stash"                "$WT" DENY
check "on a later line"                     $'ls\ngit stash pop'              "$WT" DENY
check "list first does not launder pop"     "git stash list && git stash pop" "$WT" DENY
check "cd into wt, then stash"              "cd $WT && git stash"             "$PLAIN" DENY
check "cd relative, then stash"             "cd main.wt; git stash pop"       "$T" DENY
check "subshell cd + stash"                 "(cd $WT && git stash)"           "$PLAIN" DENY
check "env assignment prefix"               "FOO=1 git stash"                 "$WT" DENY
check "env(1) prefix with option"           "env -u X FOO=1 git stash pop"    "$WT" DENY
check "-c option before stash"              "GIT_PAGER=cat git -c core.x=y --no-pager stash" "$WT" DENY
check "absolute git binary"                 "/usr/bin/git stash pop"          "$WT" DENY
check "relative -C from parent"             "git -C main.wt stash"            "$T" DENY
check "-C via shell variable"               "W=$WT; git -C \"\$W\" stash pop" "$PLAIN" DENY
check "-C via exported variable"            "export W=$WT && git -C \${W} stash" "$PLAIN" DENY
check "bash -c wrapper"                     "bash -c 'git stash pop'"         "$WT" DENY
check "bash -lc combined flags"             "bash -lc 'git stash pop'"        "$WT" DENY
check "sh -ec combined flags"               "sh -ec 'git stash'"              "$WT" DENY
check "bash -o opt then -c"                 "bash -o pipefail -c 'git stash'" "$WT" DENY
check "command substitution"                "x=\$(git stash create); echo \$x" "$WT" DENY
check "backslash-newline continuation"      $'git \\\n  stash pop'            "$WT" DENY
check "cd - is untracked, fails closed"     "cd $SOLO && cd - && git stash"  "$WT" DENY "could not resolve"
check "unresolvable -C fails closed"       "git -C \"\$NOPE_UNSET_VAR\" stash" "$PLAIN" DENY "could not resolve"

# --- the main checkout is guarded too while other worktrees exist
check "main tree with a linked wt attached" "git stash"                       "$MAIN" DENY "other worktree"
check "-C main from elsewhere"              "git -C $MAIN stash pop"          "$PLAIN" DENY

# --- read-only and unrelated: allowed
check "git stash list"                      "git stash list"                  "$WT" ALLOW
check "git stash show -p"                   "git stash show -p stash@{0}"     "$WT" ALLOW
check "git status"                          "git status"                      "$WT" ALLOW
check "quoted git stash in echo"            "echo \"git stash pop\""          "$WT" ALLOW
check "single-quoted in grep"               "grep -rn 'git stash' ."          "$WT" ALLOW
check "commit message mentions it"          "git commit -m \"never git stash\"" "$WT" ALLOW
check "log --grep stash"                    "git log --grep stash"            "$WT" ALLOW
check "comment only"                        "ls # then git stash pop"         "$WT" ALLOW
check "heredoc body"                        $'cat > n.txt <<\'EOF\'\ngit stash pop\nEOF\nls' "$WT" ALLOW
check "solo repo (no worktrees)"            "git stash"                       "$SOLO" ALLOW
check "-C solo from inside a wt"            "git -C $SOLO stash pop"          "$WT" ALLOW
check "not a git repo"                      "git stash"                       "$PLAIN" ALLOW
check "stash word as a branch name"         "git checkout stash"              "$WT" ALLOW
check "empty command"                       ""                                "$WT" ALLOW

# --- non-Bash tool ignored
out=$(j Read "git stash pop" "$WT" | python3 "$HOOK" 2>/dev/null)
if [ -z "$out" ]; then pass=$((pass+1)); echo "ok   ALLOW  non-Bash tool ignored"; else fail=$((fail+1)); echo "FAIL: non-Bash tool ignored"; fi

# --- garbage stdin never crashes the hook
out=$(printf 'not json' | python3 "$HOOK" 2>/dev/null); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then pass=$((pass+1)); echo "ok   ALLOW  garbage stdin"; else fail=$((fail+1)); echo "FAIL: garbage stdin (rc=$rc)"; fi

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
