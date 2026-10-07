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

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
