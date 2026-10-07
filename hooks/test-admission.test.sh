#!/bin/bash
# Tests for test-admission.py -- run: bash hooks/test-admission.test.sh
HOOK="$(cd "$(dirname "$0")" && pwd)/test-admission.py"
pass=0; fail=0
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export TEST_ADMISSION_DIR="$T/state" TEST_ADMISSION_LOG="$T/ledger.log"
export TEST_ADMISSION_PS_FILE="$T/ps.txt" TEST_MAX_WORKERS=2 TEST_MAX_FULL=1
unset TEST_ADMISSION_BYPASS
: > "$TEST_ADMISSION_PS_FILE"

PY=/opt/homebrew/Cellar/python@3.13/3.13.11/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python

j() { python3 -c 'import json,sys; print(json.dumps({"tool_name":sys.argv[1],"session_id":sys.argv[3] if len(sys.argv)>3 else "s1","cwd":"/w","tool_input":{"command":sys.argv[2]}}))' "$@"; }

# $1 name, $2 stdin json, $3 expect: ALLOW or DENY, $4 optional substring the deny reason must contain
check() {
  out=$(printf '%s' "$2" | python3 "$HOOK"); rc=$?
  if [ $rc -ne 0 ]; then fail=$((fail+1)); echo "FAIL: $1 (exit $rc)"; return; fi
  case "$3" in
    ALLOW) [ -z "$out" ] && { pass=$((pass+1)); return; } ;;
    DENY) echo "$out" | python3 -c '
import json,sys
d=json.load(sys.stdin)["hookSpecificOutput"]
assert d["hookEventName"]=="PreToolUse" and d["permissionDecision"]=="deny", d
assert sys.argv[1] in d["permissionDecisionReason"], d["permissionDecisionReason"]
' "${4:-}" 2>/dev/null && { pass=$((pass+1)); return; } ;;
  esac
  fail=$((fail+1)); echo "FAIL: $1"; echo "  got: ${out:-<empty>}"
}
ps_set() { printf '%s\n' "$@" > "$TEST_ADMISSION_PS_FILE"; }
reset() { : > "$TEST_ADMISSION_PS_FILE"; rm -rf "$TEST_ADMISSION_DIR"; }

# --- worker cap (a targeted path so the full-suite gate stays out of the way)
check "-n auto denied"             "$(j Bash 'python -m pytest tests/a/test_x.py -n auto --dist loadfile')" DENY "-n 2"
check "--numprocesses auto denied" "$(j Bash 'pytest tests/a --numprocesses auto')"                   DENY "-n 2"
check "--numprocesses=auto denied" "$(j Bash 'pytest tests/a --numprocesses=auto')"                   DENY
check "-n 6 denied"                "$(j Bash 'pytest tests/a -n 6 --dist loadfile')"                  DENY "--dist loadfile"
check "-n6 glued denied"           "$(j Bash 'pytest tests/a -n6')"                                   DENY
check "-n logical denied"          "$(j Bash 'pytest tests/a -n logical')"                            DENY
out=$(j Bash 'pytest tests/foo-name.py -n auto' | python3 "$HOOK")
echo "$out" | grep -q 'tests/foo-name.py -n 2' && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL: suggestion keeps -n lookalike paths intact"; echo "  got: $out"; }
check "-n 2 allowed"            "$(j Bash 'pytest tests/a -n 2 --dist loadfile')"                  ALLOW
check "no -n allowed"              "$(j Bash '.venv/bin/python -m pytest tests/a/test_x.py -q')"      ALLOW
check "wrapped -n auto denied"     "$(j Bash 'cd /w && RDS_DATABASE_URL= timeout 590 /w/.venv/bin/python -m pytest tests/a -n auto > /tmp/o.log 2>&1')" DENY
check "second segment caught"      "$(j Bash 'git status; pytest tests/a -n 8')"                      DENY
check "multi-line caught"          "$(j Bash $'echo hi\npytest tests/a -n auto')"                     DENY
check "uv run caught"              "$(j Bash 'uv run pytest tests/a -n auto')"                        DENY
check "echo of pytest not a run"   "$(j Bash 'echo "pytest -n auto"; grep -rn pytest docs')"          ALLOW
check "non-pytest -n allowed"      "$(j Bash 'head -n 20 file.txt')"                                  ALLOW
check "non-Bash tool ignored"      "$(j Read 'pytest -n auto')"                                       ALLOW

# --- full-suite admission
reset
check "first full suite admitted"  "$(j Bash 'python -m pytest -n 2 --dist loadfile -q' s1)"          ALLOW
check "fresh claim blocks other session" "$(j Bash 'pytest -n 2 --dist loadfile -q' s2)"               DENY "already running"
check "own fresh claim not self-blocking" "$(j Bash 'pytest -n 2 --dist loadfile -q' s1)"              ALLOW
reset
ps_set "4242 1 01:02:03 $PY -m pytest -n 2 --dist loadfile -q"
check "live full suite blocks"     "$(j Bash 'pytest -q' s9)"                                         DENY "4242"
check "tests/ counts as full"      "$(j Bash 'pytest tests/ -q' s9)"                                  DENY
check "targeted run still allowed" "$(j Bash 'pytest tests/sign/test_a.py -n 2' s9)"                  ALLOW
check "--collect-only exempt"      "$(j Bash 'pytest --collect-only -q' s9)"                          ALLOW
check "--co exempt"                "$(j Bash 'python -m pytest --co -q' s9)"                          ALLOW
check "--version exempt"           "$(j Bash 'pytest --version' s9)"                                  ALLOW
check "timeout -s KILL still caught" "$(j Bash 'timeout -s KILL 590 python -m pytest -q' s9)"         DENY
check "python -X dev still caught" "$(j Bash 'python -X dev -m pytest -q' s9)"                        DENY
check "bypass inline allowed"      "$(j Bash 'TEST_ADMISSION_BYPASS=1 pytest -q' s9)"                 ALLOW
out=$(j Bash 'pytest -q' s9 | TEST_ADMISSION_BYPASS=1 python3 "$HOOK")
[ -z "$out" ] && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL: bypass env allowed"; }
reset
ps_set "100 1 00:10 /bin/zsh -c source snap.sh && eval 'pytest -n 2 -q'" \
       "101 1 00:10 timeout 590 /w/.venv/bin/python -m pytest tests/a -q" \
       "102 101 00:10 $PY -m pytest tests/a -q" \
       "103 102 00:09 $PY -u -c import sys;exec(eval(sys.stdin.readline()))"
check "shell/timeout/worker/targeted not counted" "$(j Bash 'pytest -q' s9)"                          ALLOW
reset
ps_set "200 1 00:10 timeout 590 /w/.venv/bin/python -m pytest -q" "201 200 00:10 $PY -m pytest -q"
check "timeout-wrapped full counted once" "$(j Bash 'pytest -q' s9)"                                  DENY "201"
reset
ps_set "400 1 00:10 $PY /w/.venv/bin/pytest -q"
check "console-script pytest counted" "$(j Bash 'pytest -q' s9)"                                      DENY "400"
reset
mkdir -p "$TEST_ADMISSION_DIR"
python3 -c 'import json,sys,time; json.dump({"ts":time.time()-600,"session_id":"old","cwd":"/x","command":"pytest"},open(sys.argv[1],"w"))' "$TEST_ADMISSION_DIR/claim-stale.json"
check "stale claim ignored"        "$(j Bash 'pytest -q' s9)"                                         ALLOW
reset
ps_set "300 1 00:10 $PY -m pytest -q"
out=$(j Bash 'pytest -q' s9 | TEST_MAX_FULL=2 python3 "$HOOK")
[ -z "$out" ] && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL: TEST_MAX_FULL=2 admits a second"; }

# --- robustness + ledger
reset
out=$(printf 'not json' | python3 "$HOOK"); rc=$?
[ $rc -eq 0 ] && [ -z "$out" ] && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL: malformed input fails open"; }
check "unbalanced quote still parsed" "$(j Bash 'pytest tests/a -n auto -k "foo')"                    DENY
grep -q '"decision": "deny"' "$TEST_ADMISSION_LOG" && grep -q '"decision": "bypass"' "$TEST_ADMISSION_LOG" \
  && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL: ledger records deny + bypass"; }

# --- coverage flags take a value; an inline token never reaches the ledger
reset
ps_set "500 1 00:10 $PY -m pytest -q"
check "--cov <pkg> is still a full suite" "$(j Bash 'pytest --cov app -q' s9)"                   DENY
reset
: > "$TEST_ADMISSION_LOG"
check "deny with inline token"     "$(j Bash 'GH_TOKEN=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA pytest tests/a -n 8')" DENY
if grep -q 'ghp_AAAA' "$TEST_ADMISSION_LOG"; then fail=$((fail+1)); echo "FAIL: ledger stores raw token"
else grep -q 'REDACTED' "$TEST_ADMISSION_LOG" && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL: ledger row not redacted"; }; fi

echo "test-admission: $pass passed, $fail failed"
[ $fail -eq 0 ]
