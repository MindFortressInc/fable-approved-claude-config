#!/bin/bash
# Tests for bash-stdin-guard.py -- run: bash hooks/bash-stdin-guard.test.sh
HOOK="$(cd "$(dirname "$0")" && pwd)/bash-stdin-guard.py"
pass=0; fail=0

# $1 name, $2 stdin json, $3 expect: GUARD or EMPTY
check() {
  out=$(printf '%s' "$2" | python3 "$HOOK")
  case "$3" in
    EMPTY) [ -z "$out" ] && { pass=$((pass+1)); return; } ;;
    GUARD) echo "$out" | python3 -c '
import json,sys
d=json.load(sys.stdin)
c=d["hookSpecificOutput"]["updatedInput"]["command"]
assert c.startswith("exec < /dev/null\n"), c[:60]
' 2>/dev/null && { pass=$((pass+1)); return; } ;;
  esac
  fail=$((fail+1)); echo "FAIL: $1"; echo "  got: ${out:-<empty>}"
}

j() { python3 -c 'import json,sys; print(json.dumps({"tool_name":sys.argv[1],"tool_input":json.loads(sys.argv[2])}))' "$1" "$2"; }

check "heredoc gets guard"        "$(j Bash '{"command":"cat <<EOF\nhi\nEOF\njq -r ."}')"            GUARD
check "quoted heredoc delimiter"  "$(j Bash '{"command":"python3 - <<'"'"'PY'"'"'\nprint(1)\nPY"}')"  GUARD
check "dash heredoc <<-"          "$(j Bash '{"command":"cat <<-EOF\n\thi\nEOF"}')"                   GUARD
check "backslash heredoc <<\\EOF" "$(j Bash '{"command":"cat <<\\EOF\nhi\nEOF"}')"                    GUARD
check "plain command untouched"   "$(j Bash '{"command":"ls -la /tmp"}')"                             EMPTY
check "herestring <<< untouched"  "$(j Bash '{"command":"jq . <<< \"{}\""}')"                         EMPTY
check "bit shift not a heredoc"   "$(j Bash '{"command":"echo $((1 << 3))"}')"                        EMPTY
# The CLI's xSt() also returns false here, so the CLI appends `< /dev/null` itself.
check "bit shift + heredoc: CLI guards" "$(j Bash '{"command":"echo $((1 << 3))\ncat <<EOF\nhi\nEOF"}')" EMPTY
check "already guarded is no-op"  "$(j Bash '{"command":"exec < /dev/null\ncat <<EOF\nhi\nEOF"}')"    EMPTY
check "non-Bash tool ignored"     "$(j Read '{"command":"cat <<EOF\nhi\nEOF"}')"                      EMPTY

# other input keys must survive into updatedInput
out=$(j Bash '{"command":"cat <<EOF\nx\nEOF","description":"d","timeout":5000,"run_in_background":true}' | python3 "$HOOK")
if echo "$out" | python3 -c '
import json,sys
u=json.load(sys.stdin)["hookSpecificOutput"]["updatedInput"]
assert u["description"]=="d" and u["timeout"]==5000 and u["run_in_background"] is True, u
'; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: preserves sibling input keys"; fi

# ---- `$VAR:<path>` under zsh is denied with the braced command ----

# The hook checks a shell is executable, so give it ones that exist on any runner.
SHELLS=$(mktemp -d); trap 'rm -rf "$SHELLS"' EXIT
mkdir "$SHELLS/zsh-fan"
for s in zsh bash zsh-fan/bash; do printf '#!/bin/sh\n' >"$SHELLS/$s"; chmod +x "$SHELLS/$s"; done
ZSH="$SHELLS/zsh"; BASH_="$SHELLS/bash"

command_json() { python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$1"; }

# $1 name, $2 command, $3 corrected command or EMPTY when it must be allowed, $4 tool shell
check_modifier() {
  out=$(command_json "$2" | env -u CLAUDE_CODE_SHELL SHELL="${4:-$ZSH}" python3 "$HOOK" 2>&1); rc=$?
  if [ "$rc" -ne 0 ]; then
    :
  elif [ "$3" = EMPTY ]; then
    case "$out" in *permissionDecision*) ;; *) pass=$((pass+1)); return ;; esac
  elif printf '%s' "$out" | EXPECT="$3" python3 -c '
import json,os,sys
h=json.load(sys.stdin)["hookSpecificOutput"]
assert h["permissionDecision"]=="deny", h
assert h["permissionDecisionReason"].endswith("Corrected command:\n"+os.environ["EXPECT"]), h
assert "updatedInput" not in h, h
' 2>/dev/null; then pass=$((pass+1)); return; fi
  fail=$((fail+1)); echo "FAIL: $1"; echo "  rc=$rc got: ${out:-<empty>}"
}

# Denied, with the exact fix.
check_modifier "bare rev:path"        'git show $SHA:tests/x'           'git show ${SHA}:tests/x'
check_modifier "double quoted"        'git show "$S:api/x.py"'          'git show "${S}:api/x.py"'
check_modifier "every hit is fixed"   'diff <(git show $A:src/x) <(git show $B:tests/x)' 'diff <(git show ${A}:src/x) <(git show ${B}:tests/x)'
check_modifier "inside cmdsub"        'echo "$(git show $S:tests/x)"'   'echo "$(git show ${S}:tests/x)"'
check_modifier "positional"           'git show $1:tests/x'             'git show ${1}:tests/x'
check_modifier "not just git"         'chown $USER:staff f'             'chown ${USER}:staff f'
check_modifier "dashed path"          'git show $S:e-2/x'               'git show ${S}:e-2/x'
check_modifier "dotted path"          'git show $S:tests.py'            'git show ${S}:tests.py'
check_modifier "unquoted heredoc body expands" $'cat <<EOF\n$S:tests/x\nEOF'  $'cat <<EOF\n${S}:tests/x\nEOF'
check_modifier "code after a quoted heredoc"   $'cat <<\'EOF\'\nDon\'t\nEOF\ngit show $S:tests/x' $'cat <<\'EOF\'\nDon\'t\nEOF\ngit show ${S}:tests/x'
check_modifier "code after a commit body"      $'git commit -m "$(cat <<\'EOF\'\nDon\'t\nEOF\n)" && git show $S:hooks/y' $'git commit -m "$(cat <<\'EOF\'\nDon\'t\nEOF\n)" && git show ${S}:hooks/y'
check_modifier "code after <<- closes"         $'cat <<-EOF\n\tx\n\tEOF\ngit show $S:tests/x' $'cat <<-EOF\n\tx\n\tEOF\ngit show ${S}:tests/x'
check_modifier "code after a herestring"       $'jq . <<< "{}"\ngit show $S:tests/x' $'jq . <<< "{}"\ngit show ${S}:tests/x'
check_modifier "code after a closed quote"     'echo "a" && git show $S:tests/x'    'echo "a" && git show ${S}:tests/x'
check_modifier "code after an ansi-c escape"   "x=\$'a\\'b'; git show \$S:tests/x"  "x=\$'a\\'b'; git show \${S}:tests/x"
check_modifier "dq resumes after a cmdsub"     'echo "$(date) $S:tests/x"'          'echo "$(date) ${S}:tests/x"'
check_modifier "shift, then code"     $'echo "$((a << b))"\ngit show $S:tests/x' $'echo "$((a << b))"\ngit show ${S}:tests/x'

check_modifier "dq resumes after backticks"   'echo "`date` $S:tests/x"'           'echo "`date` ${S}:tests/x"'
check_modifier "a hash inside a word"          'echo a#b; git show $S:tests/x'      'echo a#b; git show ${S}:tests/x'
check_modifier "dollar hash is no comment"     'echo $#; git show $S:tests/x'       'echo $#; git show ${S}:tests/x'
check_modifier "code on the line after a comment" $'# note\ngit show $S:tests/x'    $'# note\ngit show ${S}:tests/x'
check_modifier "only code after a <<- body"    $'cat <<-\'EOF\'\n\t$S:tests/x\n\tEOF\ngit show $S:tests/y' $'cat <<-\'EOF\'\n\t$S:tests/x\n\tEOF\ngit show ${S}:tests/y'

# Allowed: literal text.
check_modifier "already braced"       'git show ${SHA}:tests/x'         EMPTY
check_modifier "single quotes"        "grep '\$S:tests/x' f"            EMPTY
check_modifier "cmdsub single quotes" "echo \"\$(echo '\$S:tests/x')\"" EMPTY
check_modifier "literal after a cmdsub" "echo \"\$(date)\" '\$S:tests/x'" EMPTY
check_modifier "ansi-c quotes"        "x=\$'\$S:tests/x'"               EMPTY
check_modifier "escaped dollar"       'echo \$S:tests/x'                EMPTY
check_modifier "comment"              '# git show $S:tests/x'           EMPTY
check_modifier "trailing comment"     'ls # git show $S:tests/x'        EMPTY
check_modifier "quoted heredoc body"  $'python3 - <<\'PY\'\nprint("$S:tests/x")\nPY' EMPTY
check_modifier "dquoted delimiter"    $'cat <<"EOF"\n$S:tests/x\nEOF'   EMPTY
check_modifier "escaped delimiter"    $'cat <<\\EOF\n$S:tests/x\nEOF'   EMPTY
check_modifier "hyphenated delimiter" $'cat <<\'A-B\'\n$S:tests/x\nA-B' EMPTY
check_modifier "two heredocs"         $'cat <<\'A\' <<\'B\'\n$S:tests/x\nA\n$S:tests/y\nB' EMPTY
check_modifier "<<- quoted body"      $'cat <<-\'EOF\'\n\t$S:tests/x\n\tEOF' EMPTY
check_modifier "a tabbed line does not close <<" $'cat <<\'EOF\'\n\tEOF\n$S:tests/x\nEOF' EMPTY
check_modifier "dq shift, then quoted heredoc" $'echo "$((a << b))"\ncat <<\'EOF\'\n$S:tests/x\nEOF' EMPTY
check_modifier "backticks in double quotes"  "echo \"\`printf %s '\$S:tests/x'\`\"" EMPTY
check_modifier "comment on the heredoc line" $'cat <<\'EOF\' # note\n$S:tests/x\nEOF' EMPTY
check_modifier "escape in unquoted body" $'cat <<EOF\n\\$S:tests/x\nEOF' EMPTY
check_modifier "shift, then quoted heredoc"  $'echo $((a << b))\ncat <<\'EOF\'\n$S:tests/x\nEOF' EMPTY
check_modifier "arithmetic command shift"    $'(( c = a << b ))\necho \'$S:tests/x\'' EMPTY
check_modifier "pid then text"        'echo $$S:tests/x'                EMPTY

# Allowed: a real modifier, which bracing would change.
check_modifier "real modifier"        'echo $f:t'                       EMPTY
check_modifier "modifier chain"       'echo "$f:t:r"'                   EMPTY
check_modifier "modifier then dir"    'echo $f:h/other'                 EMPTY
check_modifier "modifier then ext"    'echo $f:r.jpg'                   EMPTY
check_modifier "substitution"         'echo $f:s/a/b/'                  EMPTY
check_modifier "global substitution"  'echo $f:gs/a/b/'                 EMPTY

# Allowed: not a modifier at all.
check_modifier "non-modifier letter"  'git show $S:docs/x'              EMPTY
check_modifier "PATH append"          'PATH=$PATH:/usr/bin ls'          EMPTY

# Which shell the CLI would pick.
check_modifier "bash tool shell"      'git show $SHA:tests/x'           EMPTY "$BASH_"
check_modifier "bash under a zsh-named dir" 'git show $SHA:tests/x'     EMPTY "$SHELLS/zsh-fan/bash"

pick() { command_json 'git show $S:tests/x' | env -u CLAUDE_CODE_SHELL "$@" python3 "$HOOK"; }
assert_pick() {
  case "$2:$(pick "${@:3}")" in
    deny:*'"deny"'*|allow:) pass=$((pass+1)) ;;
    *) fail=$((fail+1)); echo "FAIL: shell pick: $1" ;;
  esac
}
assert_pick "override beats SHELL (bash)" allow CLAUDE_CODE_SHELL="$BASH_" SHELL="$ZSH"
assert_pick "override beats SHELL (zsh)"  deny  CLAUDE_CODE_SHELL="$ZSH" SHELL="$BASH_"
assert_pick "a dead override is skipped"  allow CLAUDE_CODE_SHELL=/nonexistent/zsh SHELL="$BASH_"
assert_pick "other shells are skipped"    allow CLAUDE_CODE_SHELL=/bin/fish SHELL="$BASH_"
assert_pick "fallback finds zsh on PATH"  deny  SHELL=/bin/sh PATH="$SHELLS:$(dirname "$(command -v python3)")"
# Needs a PATH holding python3 but not zsh, which a runner with both in one directory can't give.
if [ ! -x "$(dirname "$(command -v python3)")/zsh" ]; then
  assert_pick "fallback without zsh"      allow SHELL=/bin/sh PATH="$(dirname "$(command -v python3)")"
fi

# The stdin rewrite still fires on a heredoc that the scan clears, `$` and all.
out=$(command_json $'cat <<\'EOF\'\n$S:tests/x\nEOF' | env -u CLAUDE_CODE_SHELL SHELL="$ZSH" python3 "$HOOK")
case "$out" in *'exec < /dev/null'*) pass=$((pass+1)) ;; *) fail=$((fail+1)); echo "FAIL: stdin guard lost on a clean heredoc"; echo "  got: ${out:-<empty>}" ;; esac

# A deny must not be shadowed by the stdin rewrite on a heredoc command.
check_modifier "deny beats the rewrite" $'cat <<EOF\nhi\nEOF\ngit show $S:tests/x' $'cat <<EOF\nhi\nEOF\ngit show ${S}:tests/x'

# What the session reads: one deny per variable shape, each fix named once.
reason() { command_json "$1" | env -u CLAUDE_CODE_SHELL SHELL="$ZSH" python3 "$HOOK" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["permissionDecisionReason"])'; }
r=$(reason 'git show $A:src/x; git show $A:src/x; git show $B:tests/y')
if [ "$(printf '%s\n' "$r" | head -1 | grep -o '${A}:src/x' | wc -l | tr -d ' ')" = 1 ] && printf '%s' "$r" | grep -qF '${B}:tests/y' && printf '%s' "$r" | grep -qF '`${A:h}`'; then
  pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: deny wording"; echo "  got: $r"; fi

r=$(reason 'git show $S:te-st.py')
if printf '%s' "$r" | grep -qF 'Brace it: ${S}:te-st.py.'; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: fix names the whole path"; echo "  got: $r"; fi

# A long command drops the full echo but still names every fix.
r=$(reason "$(python3 -c 'print("git show $A:src/x; git show $B:tests/y; echo " + "z" * 2100)')")
if printf '%s' "$r" | grep -qF '${A}:src/x' && printf '%s' "$r" | grep -qF '${B}:tests/y' && ! printf '%s' "$r" | grep -q 'Corrected command'; then
  pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: long command fixes"; fi

# Keep the table honest: the verdict must match what real zsh does to the value,
# and the corrected form must be what restores it.  No `:W` token whose expansion
# varies between runs belongs here: real zsh printed either the literal or stray
# bytes for `$B:wWeb/x`, and `$B:Web/x` is stray bytes nearly every run.
if command -v zsh >/dev/null 2>&1; then
  for path in tests/x src/x api/x hooks/x lib/x e2e/x run/x util/x cmd/x query/x AGENTS.md Pods/x Queries/x \
              frontend/x features/x web/x \
              docs/x packages/x bin/x github/x workflows/x jobs/x models/x .github/x README.md \
              fWork/x gWork/x; do
    if [ "$(zsh -f -c "B=abc; print -r -- \"\$B:$path\"" 2>/dev/null)" = "abc:$path" ]; then want=EMPTY; else want="git show \${B}:$path"; fi
    check_modifier "matches zsh: $path" "git show \$B:$path" "$want"
    got=$(zsh -f -c "B=abc; print -r -- \"\${B}:$path\"")
    if [ "$got" = "abc:$path" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: braced $path got $got"; fi
  done
else
  echo "note: zsh not installed, the table check did not run"
fi

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
