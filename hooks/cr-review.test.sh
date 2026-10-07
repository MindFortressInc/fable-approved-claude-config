#!/usr/bin/env bash
# Exercises cr-review.sh rotation against a stub `coderabbit` -- no real quota burned.
set -uo pipefail
SCRATCH="${TMPDIR:-/tmp}/cr-review-test.$$"
rm -rf "$SCRATCH"; mkdir -p "$SCRATCH/bin" "$SCRATCH/seats"
trap 'rm -rf "$SCRATCH"' EXIT
export CR_SEATS_DIR="$SCRATCH/seats"
export CR_SEAT_HOURLY_MAX=5
export CR_SEAT_COOLDOWN=900
# Overridable so the suite can be pointed at another copy of the wrapper, to
# confirm a new test actually fails against the version it was written for:
#
#   git show HEAD~1:hooks/cr-review.sh > /tmp/prev.sh && chmod +x /tmp/prev.sh
#   cr-review.test.sh /tmp/prev.sh
#
# It must be a real executable file -- process substitution gives you a pipe
# (`/dev/fd/63`), and exec'ing that fails with "Bad file descriptor".
# Default: the wrapper next to this file -- the copy under test -- never an
# installed ~/.claude one, which may be a different version (or absent in CI).
WRAP="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cr-review.sh}"

# stub: any seat named in $LIMITED answers "Review limit reached"
cat > "$SCRATCH/bin/coderabbit" <<'STUB'
#!/usr/bin/env bash
seat=$(basename "$HOME")
echo "STUB invoked as seat=$seat args=$*"
case " ${LIMITED:-} " in *" $seat "*) echo "✗ Review limit reached"; exit 1;; esac
echo "Review complete. 0 issues found."
STUB
chmod +x "$SCRATCH/bin/coderabbit"
export PATH="$SCRATCH/bin:$PATH"

for s in alpha bravo charlie; do
  mkdir -p "$SCRATCH/seats/$s/.coderabbit"
  printf '{"user":{"user_name":"%s"}}' "$s" > "$SCRATCH/seats/$s/.coderabbit/auth.json"
done

pass=0; fail=0
check() { # check <label> <expected-substring> <actual>
  if grep -qF -- "$2" <<<"$3"; then echo "  PASS: $1"; pass=$((pass+1));
  else echo "  FAIL: $1 -- expected to contain: $2"; echo "$3" | sed 's/^/        /'; fail=$((fail+1)); fi
}

echo "T1: all seats healthy -> one seat runs, exit 0"
o=$("$WRAP" --base main 2>&1); rc=$?
check "exit 0" "" ""; [[ $rc -eq 0 ]] && { echo "  PASS: exit code 0"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc"; fail=$((fail+1)); }
check "invoked a seat" "STUB invoked as seat=" "$o"
check "recorded a use" "1" "$(cat "$SCRATCH/seats"/*/.uses 2>/dev/null | wc -l | tr -d ' ')"

echo "T2: first-choice seat limited -> rotates to another and succeeds"
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
LIMITED="charlie" o=$("$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 0 ]] && { echo "  PASS: exit code 0"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc"; fail=$((fail+1)); }
check "review completed" "Review complete" "$o"

echo "T3: every seat limited -> exit 75, all seats put on cooldown"
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
o=$(LIMITED="alpha bravo charlie" "$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 75 ]] && { echo "  PASS: exit code 75"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 75)"; fail=$((fail+1)); }
check "reports exhaustion" "ALL SEATS EXHAUSTED" "$o"
check "3 seats on cooldown" "3" "$(ls "$SCRATCH/seats"/*/.limited_until 2>/dev/null | wc -l | tr -d ' ')"

echo "T4: cooldown honored -> next run exits 75 WITHOUT invoking the CLI"
o=$("$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 75 ]] && { echo "  PASS: exit code 75"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc"; fail=$((fail+1)); }
if grep -qF "STUB invoked" <<<"$o"; then echo "  FAIL: burned a call while cooling"; fail=$((fail+1));
else echo "  PASS: no CLI call while cooling"; pass=$((pass+1)); fi
check "all cooling" "cooling" "$o"

echo "T5: at-cap seat is skipped (5 uses in the last hour)"
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
n=$(date +%s); for i in 1 2 3 4 5; do echo $((n-60)) >> "$SCRATCH/seats/charlie/.uses"; done
o=$("$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 0 ]] && { echo "  PASS: exit code 0"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc"; fail=$((fail+1)); }
if grep -qF "seat=charlie" <<<"$o"; then echo "  FAIL: used the at-cap seat"; fail=$((fail+1));
else echo "  PASS: at-cap seat skipped"; pass=$((pass+1)); fi

echo "T6: a busy (locked) seat is skipped, live holder respected"
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
sleep 300 & holder=$!
for s in alpha bravo; do mkdir -p "$SCRATCH/seats/$s/.lock"; echo $holder > "$SCRATCH/seats/$s/.lock/pid"; done
o=$("$WRAP" --base main 2>&1); rc=$?
kill $holder 2>/dev/null
check "fell through to the free seat" "seat=charlie" "$o"
if grep -qE "seat=(alpha|bravo)" <<<"$o"; then echo "  FAIL: stole a locked seat"; fail=$((fail+1));
else echo "  PASS: locked seats not stolen"; pass=$((pass+1)); fi
rm -rf "$SCRATCH/seats"/*/.lock

echo "T7: no seats registered -> falls back to the default identity"
o=$(CR_SEATS_DIR="$SCRATCH/empty" "$WRAP" --base main 2>&1); rc=$?
check "fallback announced" "falling back to the default identity" "$o"
check "still ran a review" "Review complete" "$o"

echo "T8: --agent JSON stays on clean stdout, wrapper chatter on stderr"
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
cat > "$SCRATCH/bin/coderabbit" <<'STUB2'
#!/usr/bin/env bash
echo '{"type":"finding","file":"a.py"}'
echo 'warning: some CLI noise' >&2
echo '{"type":"complete"}'
STUB2
chmod +x "$SCRATCH/bin/coderabbit"
so=$("$WRAP" --agent 2>/dev/null); se=$("$WRAP" --agent 2>&1 >/dev/null)
if [[ "$so" == '{"type":"finding","file":"a.py"}'$'\n''{"type":"complete"}' ]]; then
  echo "  PASS: stdout is pure JSON"; pass=$((pass+1));
else echo "  FAIL: stdout polluted:"; echo "$so" | sed 's/^/        /'; fail=$((fail+1)); fi
check "wrapper log on stderr" "using seat" "$se"
check "CLI stderr preserved on stderr" "some CLI noise" "$se"

echo "T9: corrupt seat state must not crash the wrapper"
# A non-numeric token in .uses used to be read into `(( ${newest:-0} > ${last:-0} ))`
# as a VARIABLE NAME. Under `set -u` that aborts the rank_seats subshell, so the
# ranking emits nothing, ORDER stays empty, and `"${ORDER[@]}"` then aborts the
# WRAPPER on bash 3.2 (macOS). Net effect: empty stdout, no review, exit 1 -- a
# review that never ran, reported as nothing to see.
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
cat > "$SCRATCH/bin/coderabbit" <<'STUB3'
#!/usr/bin/env bash
echo "STUB invoked as seat=$(basename "$HOME")"
echo "Review complete. 0 issues found."
STUB3
chmod +x "$SCRATCH/bin/coderabbit"
printf 'notanumber\n' > "$SCRATCH/seats/charlie/.uses"
o=$("$WRAP" --base main 2>&1); rc=$?
if grep -qF "unbound variable" <<<"$o"; then echo "  FAIL: died on an unbound variable"; fail=$((fail+1));
else echo "  PASS: survived corrupt .uses"; pass=$((pass+1)); fi
check "still ran a review" "Review complete" "$o"
[[ $rc -eq 0 ]] && { echo "  PASS: exit code 0"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 0)"; fail=$((fail+1)); }

echo "T9b: corrupt .limited_until is treated as 'not cooling', not a crash"
rm -f "$SCRATCH/seats"/*/.uses
: > "$SCRATCH/seats/bravo/.limited_until"          # zero-byte: a truncated write
printf 'soon\n' > "$SCRATCH/seats/charlie/.limited_until"
o=$(CR_SEAT_WAIT_REFRESH=60 "$WRAP" --base main 2>&1); rc=$?
if grep -qF "unbound variable" <<<"$o"; then echo "  FAIL: died on an unbound variable"; fail=$((fail+1));
else echo "  PASS: survived corrupt .limited_until"; pass=$((pass+1)); fi
[[ $rc -eq 0 ]] && { echo "  PASS: exit code 0"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 0)"; fail=$((fail+1)); }
rm -f "$SCRATCH/seats"/*/.limited_until

echo "T10: the wrapper NEVER exits 0 or 75 without having produced a review"
# The whole point of the gate: a caller may only record cr_cli as clean on exit 0,
# and as an AUTHORIZED SKIP on exit 75. Any internal wrapper failure must land on
# neither -- otherwise a review that never ran is recorded as one that did.
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
# Force ranking to produce nothing at all, the state T9's crash used to reach.
o=$(CR_SEAT_FORCE_RANK_EMPTY=1 "$WRAP" --base main 2>&1); rc=$?
if [[ $rc -eq 0 || $rc -eq 75 ]]; then
  echo "  FAIL: exit $rc claims a clean review/authorized skip with no review"; fail=$((fail+1));
else echo "  PASS: exit $rc is neither 0 nor 75"; pass=$((pass+1)); fi
check "says so loudly" "INTERNAL ERROR" "$o"
if grep -qF "STUB invoked" <<<"$o"; then echo "  FAIL: claimed a review it never ran"; fail=$((fail+1));
else echo "  PASS: no review claimed"; pass=$((pass+1)); fi

echo "T11: a wrapper KILLED mid-review exits 70, never 0 (the real fail-open)"
# This is the one that actually shipped bad gates. Before the fail-closed trap a
# SIGTERM'd wrapper -- the tmux reaper, a killed parent, an OOM under fan-out --
# ran the old EXIT trap, which returned cleanly, so bash exited **0** with an
# EMPTY stdout. Exit 0 plus no findings is indistinguishable from "reviewed,
# found nothing", so the caller recorded a clean cr_cli gate for a review that
# was never produced. Measured against the pre-fix script: rc=0, 0 bytes out.
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
cat > "$SCRATCH/bin/coderabbit" <<'STUB4'
#!/usr/bin/env bash
sleep 30
STUB4
chmod +x "$SCRATCH/bin/coderabbit"
( "$WRAP" --agent >"$SCRATCH/kill.out" 2>"$SCRATCH/kill.err" ) & victim=$!
sleep 2; kill -TERM $victim 2>/dev/null; wait $victim; rc=$?
[[ $rc -ne 0 ]] && { echo "  PASS: not exit 0"; pass=$((pass+1)); } || { echo "  FAIL: exit 0 on a killed review"; fail=$((fail+1)); }
[[ $rc -eq 70 ]] && { echo "  PASS: exit code 70"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 70)"; fail=$((fail+1)); }
check "announced the internal error" "INTERNAL ERROR" "$(cat "$SCRATCH/kill.err")"
if [[ -n "$(ls -d "$SCRATCH/seats"/*/.lock 2>/dev/null)" ]]; then
  echo "  FAIL: left a seat lock held"; fail=$((fail+1));
else echo "  PASS: released the seat lock"; pass=$((pass+1)); fi

echo "T11b: a killed wrapper takes the CLI down WITH it, on both paths"
# T11 proves the exit CODE is 70. It does not prove the wrapper actually STOPS.
# A signal aimed at the wrapper's PID never reaches a foreground child, and bash
# defers the trap until that child returns -- so the wrapper stayed alive for the
# rest of the review, holding the seat lock, which is precisely the
# scenario the fail-closed trap was written for. Measured before the fix: TERM at
# 2s into a 25s review -> the wrapper died at 25s. run_cli() backgrounds the CLI
# and waits on it (interruptible), so cleanup() can kill it.
#
# The threshold is deliberately loose (<10s of a 25s review): this asserts "the
# trap ran promptly", not a precise latency, so it will not flake on a busy box.
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
# The stub `exec`s its sleep on purpose, so the process the wrapper tracks IS
# the long-running work -- which is what the real CLI is, a single binary. A stub
# that ran `sleep 25` as a CHILD would model something else entirely: killing the
# stub shell leaves the sleep orphaned, so a `pgrep` for the stub would report a
# clean kill while the work carried on. Verified: a TERM'd parent shell does NOT
# take its sleep child with it. Recording the PID and probing it with `kill -0`
# is the assertion that cannot be fooled that way.
export CLI_PIDFILE="$SCRATCH/cli.pid"
cat > "$SCRATCH/bin/coderabbit" <<'STUB4B'
#!/usr/bin/env bash
[[ -n "${CLI_PIDFILE:-}" ]] && echo $$ > "$CLI_PIDFILE"
exec sleep 25
STUB4B
chmod +x "$SCRATCH/bin/coderabbit"
for path in seat fallback; do
  if [[ $path == seat ]]; then seats="$CR_SEATS_DIR"; else seats="$SCRATCH/empty"; fi
  rm -f "$CLI_PIDFILE"
  t0=$(date +%s)
  ( CR_SEATS_DIR="$seats" "$WRAP" --base main >/dev/null 2>&1 ) & victim=$!
  sleep 2; kill -TERM $victim 2>/dev/null; wait $victim 2>/dev/null; rc=$?
  elapsed=$(( $(date +%s) - t0 ))
  if (( elapsed < 10 )); then echo "  PASS: $path path stopped promptly (${elapsed}s)"; pass=$((pass+1));
  else echo "  FAIL: $path path outlived the signal by the whole review (${elapsed}s of 25s)"; fail=$((fail+1)); fi
  [[ $rc -eq 70 ]] && { echo "  PASS: $path path exit 70"; pass=$((pass+1)); } || { echo "  FAIL: $path path exit $rc (want 70)"; fail=$((fail+1)); }
  # And the CLI itself must be dead, not orphaned and still burning the seat.
  cli_pid=$(cat "$CLI_PIDFILE" 2>/dev/null)
  if [[ -z "$cli_pid" ]]; then
    echo "  FAIL: $path path never launched the CLI -- the assertion would be vacuous"; fail=$((fail+1));
  elif kill -0 "$cli_pid" 2>/dev/null; then
    echo "  FAIL: $path path orphaned the CLI (pid $cli_pid still alive)"; fail=$((fail+1)); kill -9 "$cli_pid" 2>/dev/null;
  else echo "  PASS: $path path left no orphaned CLI (pid $cli_pid is gone)"; pass=$((pass+1)); fi
done
unset CLI_PIDFILE
if [[ -n "$(ls -d "$SCRATCH/seats"/*/.lock 2>/dev/null)" ]]; then
  echo "  FAIL: left a seat lock held"; fail=$((fail+1)); rm -rf "$SCRATCH/seats"/*/.lock
else echo "  PASS: released the seat lock"; pass=$((pass+1)); fi

echo "T12: LEADING-ZERO seat state is read as base 10, not octal (review)"
# Validating "all digits" is not enough. bash reads a leading-zero literal as
# OCTAL, so "08" reaches (( )) and dies with "value too great for base" -- the
# exact arithmetic surprise the sanitizer exists to remove. .hourly_max is the
# reachable case: cr-seats.sh writes it, and tr -dc strips junk without
# normalizing. 08 must mean eight, and must not error.
#
# Must be a ONE-SEAT pool. With siblings available the capped seat sorts last and
# the loop exits on a free seat before its ceiling is ever evaluated -- the first
# cut of this test passed against the buggy wrapper for exactly that reason.
cat > "$SCRATCH/bin/coderabbit" <<'STUB5'
#!/usr/bin/env bash
echo "STUB invoked as seat=$(basename "$HOME")"
echo "Review complete. 0 issues found."
STUB5
chmod +x "$SCRATCH/bin/coderabbit"
mkdir -p "$SCRATCH/solo/charlie/.coderabbit"
printf '{"user":{"user_name":"charlie"}}' > "$SCRATCH/solo/charlie/.coderabbit/auth.json"
printf '08\n' > "$SCRATCH/solo/charlie/.hourly_max"
n=$(date +%s); for i in 1 2 3 4 5 6 7 8; do echo $((n-60)) >> "$SCRATCH/solo/charlie/.uses"; done
o=$(CR_SEATS_DIR="$SCRATCH/solo" CR_SEAT_WAIT_MAX=0 "$WRAP" --base main 2>&1); rc=$?
if grep -qF "value too great for base" <<<"$o"; then echo "  FAIL: read 08 as octal"; fail=$((fail+1));
else echo "  PASS: no octal arithmetic error"; pass=$((pass+1)); fi
# 8 uses against a ceiling of 8 = at cap. Only reached if "08" meant eight: the
# octal read errors, (( )) returns false, and the seat is used as if it were free.
if grep -qF "STUB invoked" <<<"$o"; then echo "  FAIL: burned an at-cap seat (ceiling misread)"; fail=$((fail+1));
else echo "  PASS: ceiling 08 honored as 8 (at cap, not used)"; pass=$((pass+1)); fi
[[ $rc -eq 75 ]] && { echo "  PASS: exit code 75"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 75)"; fail=$((fail+1)); }

echo "T13: 70 is RESERVED -- a CLI that exits 70 is remapped, not passed through"
# babysit records only the numeric exit code to /tmp/cli-<id>.rc and never reads
# the wrapper's stderr, so "70 plus an INTERNAL ERROR marker" is not a signal it
# can act on. If the CLI's own 70 reached a caller it would be classified as a
# wrapper bug and retried as though no review had been attempted.
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
cat > "$SCRATCH/bin/coderabbit" <<'STUB6'
#!/usr/bin/env bash
echo "STUB invoked as seat=$(basename "$HOME")"
echo "some CLI failure" >&2
exit 70
STUB6
chmod +x "$SCRATCH/bin/coderabbit"
o=$("$WRAP" --base main 2>&1); rc=$?
[[ $rc -ne 70 ]] && { echo "  PASS: CLI's 70 not passed through"; pass=$((pass+1)); } || { echo "  FAIL: exit 70 collides with the wrapper's own code"; fail=$((fail+1)); }
[[ $rc -eq 1 ]] && { echo "  PASS: remapped to 1"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 1)"; fail=$((fail+1)); }
if grep -qF "INTERNAL ERROR" <<<"$o"; then echo "  FAIL: claimed a wrapper-internal failure for a CLI failure"; fail=$((fail+1));
else echo "  PASS: not reported as wrapper-internal"; pass=$((pass+1)); fi
check "CLI stderr still surfaced" "some CLI failure" "$o"

echo "T14: junk .uses records must not MANUFACTURE an unearned exit 75"
# The nastiest variant of this PR's bug class. awk's `>=` is LEXICAL when $1 is
# not a number, and a word outranks any digit string -- so every junk line
# satisfies the rolling-hour cutoff and is counted as a use. Ten of them make a
# 10/hr seat read as spent, and the wrapper reports 75: an AUTHORIZED SKIP that
# was never earned, on a seat with its full quota free. num() cannot catch this
# one -- it guards what reaches (( )), and this miscount happens before that.
# One-seat pool, for the same reason T12 needs one.
cat > "$SCRATCH/bin/coderabbit" <<'STUB7'
#!/usr/bin/env bash
echo "STUB invoked as seat=$(basename "$HOME")"
echo "Review complete. 0 issues found."
STUB7
chmod +x "$SCRATCH/bin/coderabbit"
mkdir -p "$SCRATCH/junk/charlie/.coderabbit"
printf '{"user":{"user_name":"charlie"}}' > "$SCRATCH/junk/charlie/.coderabbit/auth.json"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do echo "notanumber" >> "$SCRATCH/junk/charlie/.uses"; done
o=$(CR_SEATS_DIR="$SCRATCH/junk" CR_SEAT_WAIT_MAX=0 "$WRAP" --base main 2>&1); rc=$?
[[ $rc -ne 75 ]] && { echo "  PASS: no unearned authorized skip"; pass=$((pass+1)); } || { echo "  FAIL: exit 75 on a seat with full quota"; fail=$((fail+1)); }
[[ $rc -eq 0 ]] && { echo "  PASS: exit code 0"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 0)"; fail=$((fail+1)); }
check "actually reviewed" "Review complete" "$o"
# prune_uses must also SELF-HEAL: an unguarded cutoff keeps junk forever, because
# a word never ages out of the window.
if grep -qF "notanumber" "$SCRATCH/junk/charlie/.uses" 2>/dev/null; then
  echo "  FAIL: junk survived prune_uses (permanent corruption)"; fail=$((fail+1));
else echo "  PASS: prune_uses evicted the junk"; pass=$((pass+1)); fi

echo "T15: ranking must be READ-ONLY -- no prune of a seat we hold no lock for"
# prune_uses is a read-modify-write (awk to a temp, mv over `.uses`). rank_seats
# used to run it on EVERY seat on EVERY attempt while holding no lock, so a
# scanning process could mv its stale copy over a line the lock-holder had just
# appended, silently undercounting that seat's launches. The prune now lives in
# acquire(), under the seat's own lock.
#
# Rig the exact interleaving: seat `held` is locked by a LIVE pid (this test) and
# carries an aged-out `.uses` line. The wrapper must rank both seats, skip `held`
# as busy, review on `free` -- and leave `held/.uses` byte-for-byte untouched.
# Against the old wrapper the ranking pass prunes the aged-out line away.
cat > "$SCRATCH/bin/coderabbit" <<'STUB8'
#!/usr/bin/env bash
echo "STUB invoked as seat=$(basename "$HOME")"
echo "Review complete. 0 issues found."
STUB8
chmod +x "$SCRATCH/bin/coderabbit"
mkdir -p "$SCRATCH/ro/held/.coderabbit" "$SCRATCH/ro/free/.coderabbit"
printf '{"user":{"user_name":"held"}}' > "$SCRATCH/ro/held/.coderabbit/auth.json"
printf '{"user":{"user_name":"free"}}' > "$SCRATCH/ro/free/.coderabbit/auth.json"
# Aged out of the rolling hour, so `held` is NOT at-cap -- it is reached and
# rejected purely as busy, which is the path that used to prune it anyway.
stale=$(( $(date +%s) - 7200 ))
printf '%s\n' "$stale" > "$SCRATCH/ro/held/.uses"
before=$(cat "$SCRATCH/ro/held/.uses")
# A live owner (this shell) and a fresh mtime, so acquire() cannot break the lock.
mkdir -p "$SCRATCH/ro/held/.lock"; printf '%s' "$$" > "$SCRATCH/ro/held/.lock/pid"
o=$(CR_SEATS_DIR="$SCRATCH/ro" CR_SEAT_WAIT_MAX=0 "$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 0 ]] && { echo "  PASS: exit code 0"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 0)"; fail=$((fail+1)); }
check "fell through to the free seat" "STUB invoked as seat=free" "$o"
after=$(cat "$SCRATCH/ro/held/.uses" 2>/dev/null)
if [[ "$after" == "$before" ]]; then echo "  PASS: ranking left the locked seat's .uses untouched"; pass=$((pass+1));
else echo "  FAIL: ranking rewrote a seat it held no lock for -- was '$before', now '$after'"; fail=$((fail+1)); fi
# ...and the seat it DID hold still gets pruned + its use recorded, so the
# housekeeping did not simply disappear with the race.
if [[ "$(wc -l < "$SCRATCH/ro/free/.uses" | tr -d ' ')" == "1" ]]; then
  echo "  PASS: the acquired seat still recorded its use"; pass=$((pass+1));
else echo "  FAIL: acquired seat's .uses is $(cat "$SCRATCH/ro/free/.uses" 2>/dev/null)"; fail=$((fail+1)); fi

echo "T16: the CLI rejecting our ARGUMENTS exits 71 -- not a verdict, not a skip"
# CodeRabbit removed `--plain`, so the documented invocation started failing with
# `error: unknown option` and exit 1 -- which PRlaunch reads as "a real CR
# failure", i.e. an adverse review verdict. It is not: no review ran, no quota
# was spent, and no retry can ever help. 71 says exactly that.
#
# The no-rotation assertion is the load-bearing half. A parse error is a property
# of OUR command line, not of a seat, so retrying it across the pool would take
# and release all three locks for nothing -- and if a later seat answered
# rate-limited, the wrapper would report 75: an AUTHORIZED SKIP for a review that
# was never once attempted.
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
cat > "$SCRATCH/bin/coderabbit" <<'STUB9'
#!/usr/bin/env bash
echo "STUB invoked as seat=$(basename "$HOME")"
echo "error: unknown option '--bogus-flag'" >&2
echo "Notes:" >&2
echo "  Plain text is the default review mode." >&2
exit 1
STUB9
chmod +x "$SCRATCH/bin/coderabbit"
o=$("$WRAP" --base main --bogus-flag 2>&1); rc=$?
[[ $rc -eq 71 ]] && { echo "  PASS: exit code 71"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 71)"; fail=$((fail+1)); }
if [[ $rc -eq 0 || $rc -eq 75 ]]; then echo "  FAIL: claimed a clean review/authorized skip"; fail=$((fail+1));
else echo "  PASS: neither 0 nor 75"; pass=$((pass+1)); fi
check "names the misconfiguration" "MISCONFIGURED" "$o"
check "replays the CLI's own diagnostic" "error: unknown option '--bogus-flag'" "$o"
if grep -qF "INTERNAL ERROR" <<<"$o"; then echo "  FAIL: reported as a wrapper-internal failure (that is 70's job)"; fail=$((fail+1));
else echo "  PASS: not confused with the wrapper's own 70"; pass=$((pass+1)); fi
n=$(grep -c "STUB invoked" <<<"$o")
[[ "$n" == "1" ]] && { echo "  PASS: did not rotate (1 invocation)"; pass=$((pass+1)); } || { echo "  FAIL: rotated across the pool -- $n invocations"; fail=$((fail+1)); }
u=$(cat "$SCRATCH/seats"/*/.uses 2>/dev/null | wc -l | tr -d ' ')
[[ "$u" == "0" ]] && { echo "  PASS: recorded no use (no quota was spent)"; pass=$((pass+1)); } || { echo "  FAIL: recorded $u use(s) for a review that never ran"; fail=$((fail+1)); }
if [[ -n "$(ls "$SCRATCH/seats"/*/.limited_until 2>/dev/null)" ]]; then
  echo "  FAIL: blamed a seat with a cooldown"; fail=$((fail+1));
else echo "  PASS: no seat put on cooldown"; pass=$((pass+1)); fi
if [[ -n "$(ls -d "$SCRATCH/seats"/*/.lock 2>/dev/null)" ]]; then
  echo "  FAIL: left a seat lock held"; fail=$((fail+1));
else echo "  PASS: released the seat lock"; pass=$((pass+1)); fi

echo "T16b: EVERY rejected-command-line shape the shipped CLI can emit maps to 71"
# A typo'd flag is only the shape we happened to hit first. `-t bogus` (an
# invalid option VALUE), a missing required option, and two conflicting flags
# are all equally "the command line is wrong, nothing ran, retrying cannot
# help" -- and an incomplete regex silently downgrades them to exit 1, i.e. an
# adverse review verdict, which is the whole bug. The list is read off the
# binary; see ARGPARSE_RE's comment for the command that re-derives it.
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
while IFS='|' read -r label msg; do
  [[ -n "$label" ]] || continue
  cat > "$SCRATCH/bin/coderabbit" <<STUB9B
#!/usr/bin/env bash
echo "$msg" >&2
exit 1
STUB9B
  chmod +x "$SCRATCH/bin/coderabbit"
  rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
  o=$("$WRAP" --base main 2>&1); rc=$?
  if [[ $rc -eq 71 ]]; then echo "  PASS: $label -> 71"; pass=$((pass+1));
  else echo "  FAIL: $label -> exit $rc (want 71)"; fail=$((fail+1)); fi
done <<'SHAPES'
invalid option value|error: option '-t, --type <type>' argument 'bogus' is invalid. Allowed choices are all, committed, uncommitted.
required option omitted|error: required option '--show-prompts' not specified
option argument missing|error: option '--base <branch>' argument missing
conflicting options|error: option '--committed' cannot be used with option '--uncommitted'
invalid value from env|error: option '--base <branch>' value 'x' from env 'CR_BASE' is invalid.
excess positional args|error: too many arguments. Expected 0 arguments but got 2: a, b.
unknown subcommand|error: unknown command 'reviwe'
missing required argument|error: missing required argument 'number-or-url'
bad command-argument|error: command-argument value 'x' is invalid for argument 'number-or-url'.
coderabbit own conflict|Option --include-untracked cannot be used with committed reviews.
coderabbit own requires|Option --region requires --api-key <key> for reviews.
SHAPES

echo "T17: a FINDING that quotes 'error: unknown option' is still a verdict, not a 71"
# The detection must read STDERR only. Review findings land on stdout and quote
# code verbatim, so a diff containing commander's own error string would
# otherwise turn a genuine adverse review into "your flags are broken" -- the
# exact fail-open this ticket exists to close, pointed the other way.
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
cat > "$SCRATCH/bin/coderabbit" <<'STUB10'
#!/usr/bin/env bash
echo "STUB invoked as seat=$(basename "$HOME")"
echo "error: unknown option '--plain'"
echo "Review complete. 1 issue found."
exit 1
STUB10
chmod +x "$SCRATCH/bin/coderabbit"
o=$("$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 1 ]] && { echo "  PASS: exit code 1 (a real review verdict)"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 1)"; fail=$((fail+1)); }
[[ $rc -ne 71 ]] && { echo "  PASS: stdout text did not trip the parse detector"; pass=$((pass+1)); } || { echo "  FAIL: a quoted finding was read as a parse error"; fail=$((fail+1)); }
check "the review output survived" "Review complete. 1 issue found." "$o"

echo "T18: 71 is RESERVED -- a CLI that exits 71 is remapped, not passed through"
# Same reasoning as T13 for 70. babysit records only the number to
# /tmp/cli-<id>.rc, so a CLI's own 71 reaching a caller would be read as "stop
# retrying, the command line is wrong" about a perfectly good command line.
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
cat > "$SCRATCH/bin/coderabbit" <<'STUB11'
#!/usr/bin/env bash
echo "STUB invoked as seat=$(basename "$HOME")"
echo "some CLI failure" >&2
exit 71
STUB11
chmod +x "$SCRATCH/bin/coderabbit"
o=$("$WRAP" --base main 2>&1); rc=$?
[[ $rc -ne 71 ]] && { echo "  PASS: CLI's 71 not passed through"; pass=$((pass+1)); } || { echo "  FAIL: exit 71 collides with the wrapper's own code"; fail=$((fail+1)); }
[[ $rc -eq 1 ]] && { echo "  PASS: remapped to 1"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 1)"; fail=$((fail+1)); }
if grep -qF "MISCONFIGURED" <<<"$o"; then echo "  FAIL: claimed a parse error for a CLI failure"; fail=$((fail+1));
else echo "  PASS: not reported as a misconfiguration"; pass=$((pass+1)); fi
check "CLI stderr still surfaced" "some CLI failure" "$o"

echo "T19: the NO-SEAT fallback path maps a parse error to 71 too"
# This path used to `exec` the CLI, which made its bare exit 1 the verdict
# verbatim -- so the one path a FRESH box takes, before any seat pool is
# registered, was also the one path that still conflated a broken command line
# with an adverse review. That is exactly where a stale flag is most likely.
cat > "$SCRATCH/bin/coderabbit" <<'STUB12'
#!/usr/bin/env bash
echo "error: unknown option '--bogus-flag'" >&2
exit 1
STUB12
chmod +x "$SCRATCH/bin/coderabbit"
o=$(CR_SEATS_DIR="$SCRATCH/empty" "$WRAP" --base main --bogus-flag 2>&1); rc=$?
check "took the fallback path" "falling back to the default identity" "$o"
[[ $rc -eq 71 ]] && { echo "  PASS: exit code 71"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 71)"; fail=$((fail+1)); }
check "names the misconfiguration" "MISCONFIGURED" "$o"

echo "T20: NO-SEAT fallback -- a rate-limited default account is an authorized skip (75)"
# The pool is opt-in, so the default path must still earn PRlaunch's skip on a
# limit: with nothing to rotate to, a limit refusal IS "every seat is spent".
cat > "$SCRATCH/bin/coderabbit" <<'STUB20'
#!/usr/bin/env bash
echo "✗ Review limit reached"
exit 1
STUB20
chmod +x "$SCRATCH/bin/coderabbit"
o=$(CR_SEATS_DIR="$SCRATCH/empty" "$WRAP" --base main 2>&1); rc=$?
check "took the fallback path" "falling back to the default identity" "$o"
[[ $rc -eq 75 ]] && { echo "  PASS: exit code 75"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 75)"; fail=$((fail+1)); }
check "says it is the authorized skip" "record the authorized cr_cli skip" "$o"

echo "T21: NO-SEAT fallback -- a non-limit CLI failure stays a real failure (not 75)"
cat > "$SCRATCH/bin/coderabbit" <<'STUB21'
#!/usr/bin/env bash
echo "network unreachable" >&2
exit 3
STUB21
chmod +x "$SCRATCH/bin/coderabbit"
o=$(CR_SEATS_DIR="$SCRATCH/empty" "$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 3 ]] && { echo "  PASS: CLI's own exit 3 passed through"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 3)"; fail=$((fail+1)); }

echo "T22: UNCONFIGURED default (no CR_SEATS_DIR, no ~/.claude/cr-seats) runs once as you"
cat > "$SCRATCH/bin/coderabbit" <<'STUB22'
#!/usr/bin/env bash
echo "STUB invoked with HOME=$HOME args=$*"
echo "Review complete. 0 issues found."
STUB22
chmod +x "$SCRATCH/bin/coderabbit"
mkdir -p "$SCRATCH/plainhome"
o=$(env -u CR_SEATS_DIR HOME="$SCRATCH/plainhome" "$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 0 ]] && { echo "  PASS: exit code 0"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc"; fail=$((fail+1)); }
check "fallback announced" "falling back to the default identity" "$o"
check "ran under the caller's own HOME" "HOME=$SCRATCH/plainhome args=review --base main" "$o"
check "ran exactly once" "1" "$(grep -c 'STUB invoked' <<<"$o")"
[[ ! -e "$SCRATCH/plainhome/.claude/cr-seats" ]] && { echo "  PASS: no seat pool created"; pass=$((pass+1)); } || { echo "  FAIL: created a seat pool dir"; fail=$((fail+1)); }

echo "T23: an ADVERSE review whose findings mention rate limits / line 429 is a verdict, not a skip"
cat > "$SCRATCH/bin/coderabbit" <<'STUB23'
#!/usr/bin/env bash
echo "Review: src/limiter.py:429 -- the rate limit check returns too early (CRITICAL)"
echo "Too many requests are retried without backoff"
exit 1
STUB23
chmod +x "$SCRATCH/bin/coderabbit"
o=$(CR_SEATS_DIR="$SCRATCH/empty" "$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 1 ]] && { echo "  PASS: no-pool path keeps the CLI's exit 1"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 1)"; fail=$((fail+1)); }
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
o=$(CR_SEAT_WAIT_MAX=0 "$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 1 ]] && { echo "  PASS: seat path keeps the CLI's exit 1"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 1)"; fail=$((fail+1)); }
check "findings surfaced" "rate limit check returns too early" "$o"

echo "T24: a SUCCESSFUL review that mentions limits is returned, never discarded as 75"
cat > "$SCRATCH/bin/coderabbit" <<'STUB24'
#!/usr/bin/env bash
echo "✗ Review limit reached is handled in api/limits.py -- looks fine"
exit 0
STUB24
chmod +x "$SCRATCH/bin/coderabbit"
rm -f "$SCRATCH/seats"/*/.uses "$SCRATCH/seats"/*/.limited_until
o=$(CR_SEAT_WAIT_MAX=0 "$WRAP" --base main 2>&1); rc=$?
[[ $rc -eq 0 ]] && { echo "  PASS: exit 0"; pass=$((pass+1)); } || { echo "  FAIL: exit $rc (want 0)"; fail=$((fail+1)); }
check "review output kept" "handled in api/limits.py" "$o"
check "no seat put on cooldown" "0" "$(ls "$SCRATCH/seats"/*/.limited_until 2>/dev/null | wc -l | tr -d ' ')"

echo; echo "RESULT: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
