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
# A VAR=val prefix on a check call reaches the hook process's environment
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
check "absolute shell path -c"              "/bin/bash -c 'git stash pop'"    "$WT" DENY
check "absolute env(1) path"                "/usr/bin/env git stash"          "$WT" DENY
check "unquoted \$(...) as -C fails closed" "git -C \$(git rev-parse --show-toplevel) stash pop" "$PLAIN" DENY "could not resolve"
check "unquoted \$(...) as cd fails closed" "cd \$(git rev-parse --show-toplevel) && git stash" "$PLAIN" DENY "could not resolve"
check "untracked \$1 as -C fails closed"    "git -C \$1 stash"                "$PLAIN" DENY "could not resolve"
check "stash inside unquoted \$(...) arg"   "echo \$(git stash pop) done"     "$WT" DENY
check "\$(...) arg does not hide later cmd" "echo \$(date) && git stash"      "$WT" DENY
check "\$(...) with a ) in quotes"          "echo \$(echo ')') && git stash list" "$WT" ALLOW
check "backslash-newline continuation"      $'git \\\n  stash pop'            "$WT" DENY
check "cd - is untracked, fails closed"     "cd $SOLO && cd - && git stash"  "$WT" DENY "could not resolve"
check "unresolvable -C fails closed"       "git -C \"\$NOPE_UNSET_VAR\" stash" "$PLAIN" DENY "could not resolve"

check "nesting past the recursion bound"     "eval eval eval eval git stash pop" "$WT" DENY "nested too deeply"
check "scoped VAR= does not retarget -C \$VAR" "W=$WT; W=$SOLO git -C \"\$W\" stash pop" "$PLAIN" DENY "linked worktree"
check "subshell cd does not leak out"       "(cd $SOLO); git stash pop"       "$WT" DENY "linked worktree"
check "\$(...) cd does not leak out"        "echo \$(cd $SOLO) && git stash pop" "$WT" DENY "linked worktree"
check "subshell cd into wt stays inside"    "(cd $WT); git stash pop"         "$SOLO" ALLOW
check "env -C retargets git"                "env -C $WT git stash pop"        "$SOLO" DENY "linked worktree"
check "env --chdir= retargets git"          "env --chdir=$WT git stash"       "$SOLO" DENY "linked worktree"
check "failed cd leaves cwd unknown"        "cd $T/nope || true; git stash pop" "$WT" DENY "could not resolve"
check "\$(...) inside double quotes"        "echo \"\$(git stash pop)\""     "$WT" DENY
check "escaped \\\$( in double quotes is text" "echo \"\\\$(git stash pop)\"" "$WT" ALLOW
GIT_DIR="$MAIN/.git/worktrees/main.wt" GIT_WORK_TREE="$WT" \
check "inherited GIT_DIR names a linked wt" "git stash pop"                   "$PLAIN" DENY "linked worktree"
check "comment ) inside \$(...) does not close it" $'echo "$( # )\ngit stash pop\n)"' "$WT" DENY
check "\$# inside \$(...) is not a comment"   "echo \$(echo \$#) && git stash list" "$WT" ALLOW
check "nested env -C applies in order"       "env -C $MAIN env -C . git stash clear" "$SOLO" DENY "other worktree"
check "nested env -C relative to the last"   "env -C $T env -C main.wt git stash"  "$SOLO" DENY "linked worktree"
GIT_DIR="$SOLO/.git" \
check "env -u drops an inherited GIT_DIR"    "env -u GIT_DIR git stash clear"      "$WT" DENY "linked worktree"
GIT_DIR="$SOLO/.git" \
check "env --unset=GIT_DIR"                  "env --unset=GIT_DIR git stash"       "$WT" DENY "linked worktree"
GIT_DIR="$SOLO/.git" \
check "env -i drops an inherited GIT_DIR"    "env -i git stash clear"              "$WT" DENY "linked worktree"
GIT_DIR="$SOLO/.git" \
check "env -u GIT_DIR then reassigned"       "env -u GIT_DIR GIT_DIR=$SOLO/.git git stash" "$WT" ALLOW
check "scoped GIT_DIR reaches bash -c"       "GIT_DIR=$MAIN/.git bash -c 'git stash clear'" "$SOLO" DENY "other worktree"
check "unresolvable scoped GIT_DIR, bash -c" "GIT_DIR=\$NOPE_UNSET_VAR bash -c 'git stash'" "$SOLO" DENY "could not resolve"
check "eval cd carries to the next segment"  "eval 'cd $MAIN'; git stash clear"   "$SOLO" DENY "other worktree"
check "eval export carries on"               "eval 'export W=$WT'; git -C \"\$W\" stash" "$PLAIN" DENY "linked worktree"
check "eval in a subshell does not leak"     "(eval 'cd $WT'); git stash pop"     "$SOLO" ALLOW
check "A=b eval scopes A to the eval only"   "GIT_DIR=$SOLO/.git eval true; git stash pop" "$WT" DENY "linked worktree"
check "bash -c cd does not leak out"         "bash -c 'cd $SOLO'; git stash pop"  "$WT" DENY "linked worktree"
check "command -p wrapper option"            "command -p git stash pop"        "$WT" DENY "linked worktree"
check "time -p wrapper option"               "time -p git stash pop"           "$WT" DENY "linked worktree"
check "sudo -u USER wrapper option"          "sudo -u nobody git stash pop"    "$WT" DENY "linked worktree"
check "exec -a NAME wrapper option"          "exec -a x git stash"             "$WT" DENY "linked worktree"
check "sudo -D retargets git"                "sudo -D $WT git stash pop"       "$SOLO" DENY "linked worktree"
check "conditional cd may not have run"      "false && cd $SOLO; git stash pop" "$WT" DENY "could not resolve"
check "cd before || may have failed"         "cd $SOLO || git stash pop"       "$WT" DENY "could not resolve"
check "cd && stash keeps the cd target"      "cd $SOLO && git stash pop"       "$WT" ALLOW
check "2>&1 is not a list separator"         "cd $SOLO 2>&1 && git stash pop"  "$WT" ALLOW
check "backquote cd does not leak out"       "echo \`cd $SOLO\`; git stash pop" "$WT" DENY "linked worktree"
check "stash inside backquotes"              "echo \`git stash pop\`"          "$WT" DENY
check "stash in backquotes in double quotes" "echo \"\`git stash pop\`\""      "$WT" DENY
check "heredoc fed to bash is scanned"       $'bash <<\'EOF\'\ngit stash pop\nEOF' "$WT" DENY "linked worktree"
check "heredoc fed to sh -s is scanned"      $'sh -s <<EOF\ncd /\ngit -C '"$WT"$' stash\nEOF' "$PLAIN" DENY "linked worktree"
check "heredoc to bash -c stays data"        $'bash -c cat <<\'EOF\'\ngit stash pop\nEOF' "$WT" ALLOW
check "heredoc delimiter with a dash"        $'cat <<EOF-1\nx\nEOF-1\ngit stash pop' "$WT" DENY "linked worktree"
check "heredoc delimiter partly quoted"      $'cat <<"EO"F\nx\nEOF\ngit stash pop' "$WT" DENY "linked worktree"
check "eval of a tracked variable"           "CMD='git stash pop'; eval \"\$CMD\"" "$WT" DENY "linked worktree"
check "bash -c of a tracked variable"        "S='git stash pop'; bash -c \"\$S\"" "$WT" DENY "linked worktree"
check "quote-joined stash word"              "git sta\"\"sh pop"               "$WT" DENY "linked worktree"
check "partly quoted stash word"             "git sta\"sh\" pop"               "$WT" DENY "linked worktree"
check "quote-joined stash in bash -c body"   "bash -c 'git sta\"\"sh pop'"     "$WT" DENY "linked worktree"
mkdir -p "$T/bin"; REAL_GIT=$(command -v git)
printf '#!/bin/sh\nfor a; do [ "$a" = worktree ] && exit 1; done\nexec %s "$@"\n' "$REAL_GIT" > "$T/bin/git"; chmod +x "$T/bin/git"
PATH="$T/bin:$PATH" \
check "failed worktree probe fails closed"  "git stash"                       "$MAIN" DENY "could not list"

printf '#!/bin/sh\nfor a; do [ "$a" = rev-parse ] && exit 1; done\nexec %s "$@"\n' "$REAL_GIT" > "$T/bin/git"
PATH="$T/bin:$PATH" \
check "failed repo probe fails closed"      "git stash"                       "$SOLO" DENY "could not probe"

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
