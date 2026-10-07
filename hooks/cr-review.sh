#!/usr/bin/env bash
# cr-review.sh -- drop-in replacement for `coderabbit review ...` that gives
# every caller one exit-code contract (below), and OPTIONALLY rotates across a
# CodeRabbit seat pool in ~/.claude/cr-seats (see cr-seats.sh).
#
#   ~/.claude/hooks/cr-review.sh --base main
#
# THE SEAT POOL IS OFF BY DEFAULT. With no seats registered (the default -- no
# ~/.claude/cr-seats, or CR_SEATS_DIR pointing at an empty dir) this runs the
# CLI ONCE under your own HOME/login, exactly like bare `coderabbit review`,
# and only adds the exit-code contract: 70/71 below, and 75 when that single
# account reports a rate limit (so PRlaunch can record its authorized skip).
#
# Why a pool exists at all: CodeRabbit rate-limits per developer. Parallel agent
# workers on one box all authenticate as one identity, so they burn one seat's
# hourly bucket and most cr_cli gates get recorded as limit-skips. Registering N
# seats (one per teammate's key, `cr-seats.sh add`) multiplies the ceiling by N
# and stops parallel workers from colliding. Only do this if your CodeRabbit
# plan and terms allow it for your team.
#
# (No --plain: CodeRabbit REMOVED that flag, plain text is now the default review
# mode, and passing it is an argument-parse failure -> exit 71 below.)
#
# A seat that is merely BUSY (a live review holds its lock) is waited out rather
# than skipped -- see the WAIT_MAX block below. Quota exhaustion still fails fast.
#
# Exit codes:
#   0   a seat produced a review (output is on stdout, verbatim)
#   70  INTERNAL ERROR -- the wrapper itself failed (unreadable seat state, a
#       broken ranking, a signal mid-run). No review ran AND no skip was earned.
#       Deliberately neither 0 nor 75: callers must treat it as a hard gate
#       failure and retry, never as a clean review or an authorized skip.
#       Always accompanied by an `INTERNAL ERROR` line on stderr. 70 is RESERVED:
#       a CLI that exits 70 itself is remapped to 1 rather than passed through,
#       so the number alone is a sufficient signal for a caller that only records
#       the exit code (e.g. a sweep that writes it to an .rc file and never
#       sees our stderr).
#   71  MISCONFIGURED INVOCATION -- the underlying CLI rejected our ARGUMENTS and
#       exited during option parsing. No review ran, no quota was spent, and no
#       seat is at fault, so this is NOT a skip, NOT a review verdict, and
#       retrying it UNCHANGED can never succeed: a human has to fix the flags.
#       Distinguished from 70 because 70 says "the wrapper broke, retry it" and
#       this says "the command line is wrong, stop retrying it". 71 is RESERVED
#       exactly like 70 -- a CLI that exits 71 itself is remapped to 1 rather
#       than passed through, so the number alone remains a sufficient signal for
#       a caller that only records the exit code.
#       Why this earned its own code: CodeRabbit removed `--plain`, so the
#       documented invocation began failing with `error: unknown option` and
#       exit 1 -- indistinguishable from a genuine adverse review verdict, on
#       every unit, every run, forever. Unattended sweeps read permanent
#       misconfiguration as a review failure. The next flag CodeRabbit removes
#       reproduces that exactly without this code.
#   75  every seat is rate-limited/at cap, or still busy after CR_SEAT_WAIT_MAX
#       -- or, with no pool registered, the one default account reported a rate
#       limit -> PRlaunch records the authorized skip
#   *   the underlying coderabbit exit code for a non-limit failure
#
# 0 and 75 are the ONLY two codes that satisfy the cr_cli gate, so every path
# that reaches neither MUST be loud and non-zero. A wrapper that dies quietly
# hands the caller empty stdout, which reads exactly like "reviewed, found
# nothing". "Could not check" must never pass as "checked and clear". Hence
# the fail-closed trap below -- see `cleanup`/`die_internal`.
#
# Env knobs: CR_SEAT_WAIT_MAX (default 1200s, 0 = fail fast), CR_SEAT_POLL (20s)
# CR_RUN_ID: pin the run id surfaced on stderr (default: auto-generated) so a
# caller can correlate this run with prlaunch-gate.sh's `record cr_cli --run-id`
#. The seat name and run id are logged together on stderr as
# `using seat '<name>' (...) run=<id>` -- never on stdout, which is the
# review's own output.
# Testing only: CR_SEAT_FORCE_RANK_EMPTY=1 forces the empty-ranking branch so the
# fail-closed exit stays covered without corrupting a real seat.
set -uo pipefail

# Every number read back off disk or out of the environment goes through here.
#
# Seat state (.uses, .limited_until) is written by concurrent wrappers and can be
# truncated mid-write, hand-edited, or left over from an older format. A stray
# non-numeric token reaching (( )) is NOT a harmless bad compare: bash treats a
# bare word inside arithmetic as a VARIABLE NAME, and under `set -u` an unset one
# ABORTS THE SHELL. One junk line in .uses used to kill the rank_seats subshell,
# which emptied the seat ranking, which then aborted the whole wrapper on the
# empty-array expansion (bash 3.2). Sanitize at the boundary instead.
# The `10#` is not redundant: bash reads a LEADING-ZERO literal as OCTAL, so a
# validated-all-digits "08" still dies with "value too great for base". Forcing
# base 10 keeps the sanitizer from reintroducing the arithmetic surprise it
# exists to remove.
num_or() {
  case "${1:-}" in ''|*[!0-9]*) printf '%s' "$2"; return ;; esac
  printf '%s' "$(( 10#$1 ))"
}
num() { num_or "${1:-}" 0; }

SEATS_DIR="${CR_SEATS_DIR:-$HOME/.claude/cr-seats}"
HOURLY_MAX=$(num_or "${CR_SEAT_HOURLY_MAX:-}" 10)  # top-of-plan CLI reviews/hr/developer.
# NOT lowered to a guessed Fair Usage tier: the real hourly
# rate degrades with trailing-7-day volume and is unobservable from here, so a
# hardcoded floor would throttle us on days the allowance is genuinely there.
# Ask for the full rate and let CodeRabbit refuse -- a refusal is free, and the
# per-seat cooldown below IS the backoff. Seats being refused at "3/10" is that
# working, not a miscount.
COOLDOWN=$(num_or "${CR_SEAT_COOLDOWN:-}" 900)     # secs a seat rests after a limit hit
LOCK_STALE=$(num_or "${CR_SEAT_LOCK_STALE:-}" 1800)
# A limit refusal is only ever a FAILED run (rc != 0) -- a review that exited 0
# is a verdict, whatever its findings say. On a failed run, stderr (CLI
# diagnostics, never findings) may match anywhere; stdout is matched only on a
# STATUS-shaped line (optional leading symbol / `error:`, the limit phrase, then
# end of line or punctuation -- "Review limit reached", "Rate limit exceeded.
# Try again in 15 minutes" -- never the phrase inside a sentence),
# because an adverse review exits non-zero too and its findings routinely
# mention rate limits or a line 429 -- that text must never read as an
# authorized skip.
LIMIT_RE='review limit|rate.?limit|limit reached|too many requests|(^|[^0-9:])429([^0-9]|$)'
LIMIT_LINE_RE='^[^[:alnum:]]*(error:?[[:space:]]*)?((review|rate.?)[[:space:]]?limit(ed)?([[:space:]]+(reached|exceeded|hit))?|limit reached|too many requests)[[:space:]]*([.!:(,-].*)?$'
limited() { # limited <rc> <stdout-file> <stderr-file>
  (( $1 != 0 )) || return 1
  grep -qiE "$LIMIT_RE" "$3" 2>/dev/null && return 0
  grep -qiE "$LIMIT_LINE_RE" "$2" 2>/dev/null
}

# The CLI rejected our COMMAND LINE and exited before attempting a review.
#
# CodeRabbit's CLI parses options with commander.js, which writes one of these
# lines to STDERR and exits non-zero without touching the network. Matched
# against stderr ONLY, and anchored at the start of a line, on purpose: review
# FINDINGS land on stdout and routinely quote code, so a diff that happens to
# contain the words "unknown option" must never be read as a parse failure.
# Anchoring also keeps a CLI that merely *mentions* one of these mid-sentence
# from tripping it.
#
# The list is not guesswork and it is not a sample -- it is every message the
# shipped binary can emit for a rejected command line, read off the bundle:
#
#   strings -a "$(readlink -f "$(which coderabbit)")" \
#     | grep -oaE '`error: [^`]{0,80}|t\.error\("[^"]{0,70}'
#
# Two groups, because the CLI rejects arguments in two different places:
#   * commander's own diagnostics, all prefixed `error: ` -- unknown/typo'd
#     flags, a missing or invalid option VALUE (`-t bogus`), a required option
#     left out, mutually exclusive flags (--committed vs --uncommitted), an
#     invalid value arriving from an env var, and excess positional arguments.
#   * CodeRabbit's OWN option validation, which calls commander's .error() with
#     a bare sentence and so lands WITHOUT the `error: ` prefix -- currently
#     `--include-untracked` with a committed review, and `--region` without
#     `--api-key`. Same class exactly: the command line is wrong, nothing ran.
# Re-derive with the command above when the CLI is upgraded.
ARGPARSE_RE='^error: (unknown option|unknown command|too many arguments|missing required argument|required option .* not specified|command-argument value .* is invalid|environment variable .* cannot be used with|option .* (argument missing|argument .* is invalid|value .* from env .* is invalid|cannot be used with))|^Option .* (cannot be used with|requires) '

# True when $1 (a stderr capture) shows the CLI died parsing arguments.
# Requires a non-zero rc as well -- commander always exits non-zero on a parse
# error, so a zero exit with a matching line is something else entirely.
argparse_failed() { # argparse_failed <stderr-file> <rc>
  (( $2 != 0 )) && grep -qE "$ARGPARSE_RE" "$1" 2>/dev/null
}

log() { printf 'cr-review: %s\n' "$1" >&2; }
now() { date +%s; }
# File mtime in epoch seconds on GNU (`stat -c`) or BSD/macOS (`stat -f`); 0 if
# unreadable. GNU first: GNU `stat -f` means "filesystem status", not mtime.
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }

# A per-invocation id so a caller (prlaunch-gate.sh's `record cr_cli --run-id`)
# can correlate its ledger entry back to the exact wrapper run that
# produced it, without this wrapper having to write anywhere new. Surfaced on
# stderr alongside the seat name below -- never on stdout, which callers parse
# as the review's own output. Overridable so a caller that already has its own
# run identifier (e.g. a PRlaunch session id) can pin it instead of the default.
CR_RUN_ID="${CR_RUN_ID:-$(now)-$$-$RANDOM}"

# --- fail-closed guard -------------------------------------------------------
# Installed before any real work, so no failure path can slip out quietly.
#
# `finished` is set ONLY by the paths that reached a genuine verdict: a review
# ran (exit = the CLI's own code), or the pool really was spent (exit 75). Every
# other way out of this script -- an unbound variable, the reaper's SIGTERM, an
# OOM kill mid-scan -- arrives here with finished=0 and leaves as 70 rather than
# as whatever bash happened to pick. Under parallel load the wrapper
# was dying mid-run and returning a bare non-zero that callers could not tell
# from a CodeRabbit failure, or worse, empty output that read as a clean review.
held=""
finished=0
in_cleanup=0
CLI_PID=""
CLI_RC=0
cleanup() {
  local rc=$?
  (( in_cleanup )) && return
  in_cleanup=1
  # Take the CLI down with us. Measured: a TERM aimed at the wrapper
  # PID alone left the wrapper blocked for the REST OF THE REVIEW -- 25s of a 25s
  # stub -- still holding the seat's lock, because bash defers a trap until the
  # running foreground command finishes. run_cli() backgrounds the CLI and waits
  # on it instead, which IS interruptible, so this line is what actually frees
  # the seat when a reaper fires under load (the scenario above).
  [[ -n "$CLI_PID" ]] && kill -TERM "$CLI_PID" 2>/dev/null
  [[ -n "$held" ]] && release "$held"
  if (( finished == 0 )); then
    log "INTERNAL ERROR: aborted before running a review or reaching a verdict"
    log "INTERNAL ERROR: bash was about to exit $rc -- overriding to 70, because this"
    log "INTERNAL ERROR: is NOT a clean review and NOT an authorized skip. Retry it."
    exit 70
  fi
}
trap cleanup EXIT INT TERM

# A deliberate, diagnosed internal failure. Sets `finished` so the EXIT trap
# releases the seat without re-announcing; the message here is the specific one.
die_internal() {
  log "INTERNAL ERROR: $1"
  log "INTERNAL ERROR: NOT a clean review and NOT an authorized skip -- exit 70. Retry it."
  finished=1
  exit 70
}

# The CLI rejected our command line. No review ran and no quota was
# spent, so the seat is left exactly as it was found -- no `.uses` line, no
# cooldown stamp. Sets `finished` for the same reason die_internal does: this IS
# a diagnosed verdict, just not a reviewable one.
die_argparse() {
  log "MISCONFIGURED: the CodeRabbit CLI rejected our arguments and exited during"
  log "MISCONFIGURED: option parsing -- see the 'error:' line above. NO review ran,"
  log "MISCONFIGURED: no quota was spent, and no seat is at fault. This is NOT a"
  log "MISCONFIGURED: limit skip and NOT a review verdict -- exit 71. Retrying it"
  log "MISCONFIGURED: unchanged can never succeed; fix the flags."
  finished=1
  exit 71
}

# Both wrapper-owned codes are RESERVED. If the CLI itself exits 70 or 71 we must
# not pass it through, or a caller reading only the number -- babysit records it
# to /tmp/cli-<id>.rc and never sees our stderr -- would classify a genuine CLI
# failure as a wrapper bug (70) or as a misconfigured command line (71) and act
# on it wrongly. Remap to 1: callers treat every non-0/70/71/75 code identically
# ("a real CR failure"), so nothing is lost, and both codes stay unambiguous.
# Run the CodeRabbit CLI as a TRACKED BACKGROUND CHILD and wait on it.
#
# Not a style choice. A signal sent to the wrapper's PID -- the tmux reaper, a
# killed parent, a caller's timeout -- does NOT reach a foreground child, and
# bash will not run a trap until that foreground command returns. Measured: TERM
# at 2s into a 25s review left the wrapper alive the full 25s, holding the seat
# lock the whole time. `wait` on a background child IS interruptible, so the
# EXIT trap fires immediately and kills the CLI (see cleanup()).
#
# $1 is the HOME to run under ("" = this process's own, the no-seat fallback).
# The CLI's own exit status lands in CLI_RC, exactly as a foreground run gave it.
run_cli() { # run_cli <home|""> <out> <err> [cli args...]
  local h="$1" out="$2" err="$3"; shift 3
  if [[ -n "$h" ]]; then
    HOME="$h" coderabbit review "$@" >"$out" 2>"$err" &
  else
    coderabbit review "$@" >"$out" 2>"$err" &
  fi
  CLI_PID=$!
  wait "$CLI_PID"; CLI_RC=$?
  CLI_PID=""
}

reserve_codes() { # reserve_codes <rc> -> rc, with 70/71 collapsed to 1
  case "$1" in
    70|71) log "the CLI exited $1, which this wrapper reserves for its own verdicts -- reporting 1"
           printf '1' ;;
    *)     printf '%s' "$1" ;;
  esac
}

# No pool registered -> behave exactly like the bare CLI so nothing breaks.
# (macOS ships bash 3.2 -- no mapfile, no associative arrays.)
SEATS=()
if [[ -d "$SEATS_DIR" ]]; then
  for d in "$SEATS_DIR"/*/; do
    [[ -f "$d/.coderabbit/auth.json" ]] && SEATS+=("${d%/}")
  done
fi
if (( ${#SEATS[@]} == 0 )); then
  log "no seats registered in $SEATS_DIR -- falling back to the default identity (run=$CR_RUN_ID)"
  # Deliberately NOT `exec` any more. exec replaced this shell, which
  # made the CLI's own exit code the verdict verbatim -- including an
  # argument-parse failure, the one code the caller most needs told apart. This
  # is the path a FRESH box takes (no seat pool registered yet), which is exactly
  # where a stale flag is most likely, so it cannot be the one path that still
  # conflates misconfiguration with a review verdict. Buffer-and-replay matches
  # the seat path below; the streams stay separate, so --agent JSON is unaffected.
  out=$(mktemp "${TMPDIR:-/tmp}/cr-review-out.XXXXXX"); err=$(mktemp "${TMPDIR:-/tmp}/cr-review-err.XXXXXX")
  run_cli "" "$out" "$err" "$@"
  rc=$CLI_RC
  cat "$out"; cat "$err" >&2
  if argparse_failed "$err" "$rc"; then
    rm -f "$out" "$err"
    die_argparse
  fi
  # Single-account rate limit. With no pool there is nothing to rotate to, so
  # a limit refusal IS the "every seat is spent" verdict -- report 75 (the one
  # code PRlaunch accepts as an authorized skip) instead of the CLI's bare
  # non-zero, which a caller would have to treat as a real review failure.
  # Same limited() test as the seat path; a zero exit is a review, whatever it says.
  if limited "$rc" "$out" "$err"; then
    rm -f "$out" "$err"
    log "the default identity is rate-limited and no seat pool is registered -- record the authorized cr_cli skip (run=$CR_RUN_ID)"
    finished=1
    exit 75
  fi
  rm -f "$out" "$err"
  rc=$(reserve_codes "$rc")
  finished=1
  exit "$rc"
fi

# Reviews this seat's IDENTITY made in the rolling hour.
#
# `.uses` alone under-counts: it only records launches THIS wrapper made, so any
# bare `coderabbit review` (a human, another tool) spends the account's 5/hr
# invisibly. That is exactly how a seat reported "0/5 used" and was then
# rejected as rate-limited.
#
# The CLI itself writes one directory per review under $HOME/.coderabbit/reviews,
# and its mtime is the review time -- an authoritative log we don't have to
# maintain. Count those, and take the MAX of the two signals: `.uses` still
# covers a fresh seat whose reviews/ dir doesn't exist yet, and over-counting
# only makes us back off earlier (safe direction).
# Per-seat hourly ceiling. CodeRabbit's CLI allowance is a property of the SEAT's
# plan, not of the pool. Measured over a whole local review store: two 5/hr
# seats exceeded 5 in ~1 of ~150 active hours each, while a higher-tier seat
# reached 10 in 41 of 789. One GLOBAL max therefore read a spent 5/hr seat as
# half-idle -- which is how an unattended sweep fired 112 launches for 23
# completions overnight. Override per seat with <seat>/.hourly_max; $CR_SEAT_HOURLY_MAX is
# only the fallback for a seat that has no measured ceiling yet.
seat_max() {
  local f="$1/.hourly_max" v
  if [[ -f "$f" ]]; then
    # tr strips junk but does NOT normalize: it happily yields "08", which bash
    # then reads as octal and rejects. num_or is what makes the result safe to
    # put in (( )), so the ceiling must go through it like every other disk read.
    v=$(num_or "$(tr -dc '0-9' < "$f" 2>/dev/null)" 0)
    (( v > 0 )) && { printf '%s' "$v"; return; }
  fi
  printf '%s' "$HOURLY_MAX"
}

# Epoch SECONDS of every review this seat completed inside the rolling hour.
#
# The store is <repoHash>/<branchHash>/reviews/<epoch_ms>/ and the DIRECTORY NAME
# is the review time in millis. The previous version counted depth-1 repo-hash
# dirs by mtime, so five reviews of the SAME repo registered as ONE -- the seat
# reported 0-1/10 while the server was already refusing it. Read the names.
review_times() {
  local d="$1/.coderabbit/reviews" cutoff_ms
  [[ -d "$d" ]] || return 0
  cutoff_ms=$(( ( $(now) - 3600 ) * 1000 ))
  find "$d" -mindepth 4 -maxdepth 4 -type d 2>/dev/null \
    | awk -F/ -v c="$cutoff_ms" '$NF ~ /^[0-9]{13}$/ && $NF+0 >= c { printf "%d\n", $NF/1000 }'
}

reviews_in_window() {
  review_times "$1" | wc -l | tr -d ' '
}

# NOTE on the awk predicate, repeated at all three `.uses` cutoffs below.
#
# `$1 >= c` alone is a LEXICAL comparison whenever $1 is not a number, and a word
# sorts above any digit string -- so "notanumber" satisfies every cutoff and is
# COUNTED AS A USE. Ten junk lines make a 10/hr seat look spent, and the wrapper
# then reports exit 75: an AUTHORIZED SKIP that was never earned, for a seat with
# quota to spare. num() cannot save this one -- it sanitizes what reaches (( )),
# and this is a miscount that happens before that. Guard the field itself, the
# way review_times already does for the review-dir names.
uses_in_window() {
  local f="$1/.uses" a=0 b
  if [[ -f "$f" ]]; then
    a=$(awk -v c="$(( $(now) - 3600 ))" '$1 ~ /^[0-9]+$/ && $1+0 >= c' "$f" | wc -l | tr -d ' ')
  fi
  b=$(reviews_in_window "$1")
  (( b > a )) && a="$b"
  printf '%s' "$a"
}

prune_uses() {
  local f="$1/.uses"
  [[ -f "$f" ]] || return 0
  # Unique per process: concurrent wrappers used to share "$f.tmp", so one's mv
  # raced the other's redirect and printed "No such file or directory" while
  # silently losing the prune (observed live).
  # The same numeric guard as uses_in_window -- and here it also SELF-HEALS: an
  # unguarded cutoff keeps junk forever (a word outranks every timestamp, so it
  # never ages out), which is what makes one bad write permanent corruption.
  local t="$f.tmp.$$"
  awk -v c="$(( $(now) - 3600 ))" '$1 ~ /^[0-9]+$/ && $1+0 >= c' "$f" > "$t" 2>/dev/null && mv "$t" "$f" || rm -f "$t"
}

# Seconds until this seat gains a quota slot (0 = it has one now).
#
# The hourly cap is a ROLLING window, so "at-cap" is temporary by construction: a
# seat at 5/5 regains a slot the moment its oldest in-window use ages past 3600s.
# The original give-up comment ("waiting does not help on any useful timescale")
# was wrong about that -- a batch could die instantly with a slot freeing 30s
# later. This is what lets the caller wait for an imminent refresh.
#
# Timestamps come from BOTH sources the count uses: `.uses` lines and review-dir
# mtimes (a review made outside the wrapper leaves no `.uses` line). If a seat is
# over cap (used > MAX, which happens when the reviews/ fallback out-counts .uses)
# then more than one use must expire, so index by how many are surplus.
secs_to_slot() {
  local seat="$1" now_s used smax need oldest refresh=0 cool
  now_s=$(now)
  used=$(uses_in_window "$seat")
  smax=$(seat_max "$seat")
  if (( used >= smax )); then
    need=$(( used - smax + 1 ))
    # Same numeric guard as uses_in_window; without it a junk line is selected as
    # the "oldest" use and num() then flattens it to 0, losing the real refresh
    # time. num() stays as the belt-and-braces read of whatever survives.
    oldest=$(num "$( { awk -v c="$(( now_s - 3600 ))" '$1 ~ /^[0-9]+$/ && $1+0 >= c {print $1}' "$seat/.uses" 2>/dev/null
                       review_times "$seat"; } | sort -n | sed -n "${need}p" )")
    if (( oldest > 0 )); then refresh=$(( oldest + 3600 - now_s )); else refresh=3600; fi
    (( refresh < 0 )) && refresh=0
  fi
  # A cooling seat needs BOTH its cooldown to lapse and quota to exist -> whichever
  # is later governs.
  if [[ -f "$seat/.limited_until" ]]; then
    cool=$(( $(num "$(cat "$seat/.limited_until" 2>/dev/null)") - now_s ))
    (( cool > refresh )) && refresh=$cool
  fi
  (( refresh < 0 )) && refresh=0
  printf '%s' "$refresh"
}

cooling() {
  local f="$1/.limited_until"
  [[ -f "$f" ]] || return 1
  # An unreadable or truncated stamp means "we don't know it is cooling", which
  # is the safe read: the seat gets tried, and a real refusal re-stamps it.
  (( $(num "$(cat "$f" 2>/dev/null)") > $(now) ))
}

# Acquiring is also the ONLY place `.uses` is pruned. prune_uses is a
# read-modify-write (awk to a temp, then mv over the file), so it is only safe
# for whoever holds that seat's lock. rank_seats used to prune EVERY seat on
# EVERY attempt while holding no lock at all, so a scanning process could mv its
# stale copy over a line the actual lock-holder had just appended -- silently
# undercounting recent launches and handing out a seat nearer its real cap than
# the accounting believed. Pruning here pairs it with the append at
# the end of the review, which is the other `.uses` write and is already under
# this same lock; ranking is now strictly read-only.
acquire() {
  local lock="$1/.lock"
  if mkdir "$lock" 2>/dev/null; then printf '%s' "$$" > "$lock/pid"; prune_uses "$1"; return 0; fi
  # break a lock whose owner died or that outlived a plausible review
  local age owner
  age=$(( $(now) - $(num "$(mtime "$lock")") ))
  owner=$(cat "$lock/pid" 2>/dev/null || echo 0)
  if (( age > LOCK_STALE )) || ! kill -0 "$owner" 2>/dev/null; then
    rm -rf "$lock"
    mkdir "$lock" 2>/dev/null && { printf '%s' "$$" > "$lock/pid"; prune_uses "$1"; return 0; }
  fi
  return 1
}
release() { rm -rf "$1/.lock"; }

# Rank: fewest reviews in the rolling hour first, then least-recently-used.
# Recomputed on every attempt -- usage and locks both move while we wait.
rank_seats() {
  for s in "${SEATS[@]}"; do
    # READ-ONLY. Ranking inspects every seat, including ones another process
    # holds, so it must not write to any of them -- see acquire(), which owns the
    # prune now. Nothing here needs a pruned file: uses_in_window and
    # secs_to_slot both apply the rolling-hour cutoff in memory, and `tail -1` on
    # an unpruned file is if anything a BETTER least-recently-used key -- a seat
    # whose uses have all aged out keeps its real last-use time instead of being
    # flattened to 0 and tying with a seat that was never used at all.
    # Same reasoning as uses_in_window: a review made outside the wrapper leaves
    # no `.uses` line, so fall back to the newest reviews/ dir mtime or a seat
    # busy elsewhere looks least-recently-used and gets picked first.
    last=$(num "$(tail -1 "$s/.uses" 2>/dev/null)")
    newest=$(num "$(find "$s/.coderabbit/reviews" -maxdepth 1 -mindepth 1 -type d -mmin -60 2>/dev/null \
               | while IFS= read -r d; do mtime "$d"; done | sort -n | tail -1)")
    (( newest > last )) && last="$newest"
    printf '%s\t%s\t%s\n' "$(uses_in_window "$s")" "$last" "$s"
  done | sort -k1,1n -k2,2n | cut -f3
}

# A seat is unavailable for one of three reasons, and they are NOT equivalent:
#
#   at-cap / cooling  the seat's QUOTA is spent. Waiting does not help on any
#                     useful timescale -> give up now, exactly as before.
#   busy              a live review holds the seat's lock. That clears in ~1-3
#                     min (a typical CLI review), so waiting is the right move.
#
# Before this split `busy` was treated as terminal: with 2 seats a burst of 3
# launches always dropped the 3rd *even with 3 quota slots unused*, because
# concurrency (= seat count), not the hourly cap, was binding. A sweep that
# fires its CLI launches as a burst could never reach the pool's 10/hr.
#
# So: retry ONLY while at least one seat is merely busy, bounded by WAIT_MAX.
# 1200s sizes the queue a burst actually forms: 10 launches over 2 seats at ~3min
# a review is ~15min of drain, so the last-queued worker waits ~12min. Still well
# under LOCK_STALE, so a genuinely wedged lock is still reclaimed, not waited on.
WAIT_MAX=$(num_or "${CR_SEAT_WAIT_MAX:-}" 1200) # secs to wait out busy seats (0 = fail fast)
POLL=$(num_or "${CR_SEAT_POLL:-}" 20)           # secs between attempts
# Also wait for a ROLLING-WINDOW quota refresh this many secs out (0 = off).
# Off by default so interactive callers keep failing fast; batch callers opt in.
REFRESH_WAIT=$(num_or "${CR_SEAT_WAIT_REFRESH:-}" 0)

deadline=$(( $(now) + WAIT_MAX ))
announced=0

while :; do
  ORDER=()
  while IFS= read -r s; do
    [[ -n "$s" ]] && ORDER+=("$s")
  done < <(rank_seats)
  # Test seam (cr-review.test.sh T10): reach the empty-ranking branch on demand
  # rather than by corrupting a seat, so the fail-closed exit stays covered.
  [[ "${CR_SEAT_FORCE_RANK_EMPTY:-0}" = 1 ]] && ORDER=()

  # SEATS is non-empty by construction -- we exec'd out to the bare CLI above if
  # the pool was unregistered -- so an empty ranking CANNOT mean "no seats". It
  # means rank_seats itself failed, and continuing would report a spent pool
  # (exit 75, an authorized skip) for seats nobody ever looked at. Fail closed.
  #
  # This also keeps the loop off `"${ORDER[@]}"` while the array is empty, which
  # on bash 3.2 -- what macOS ships, and what this script targets -- is itself an
  # "unbound variable" abort under `set -u`, not an empty iteration.
  if (( ${#ORDER[@]} == 0 )); then
    die_internal "ranked 0 of ${#SEATS[@]} registered seats -- seat state unreadable?"
  fi

  skipped=""; busy_seats=0
  for seat in "${ORDER[@]}"; do
    name=$(basename "$seat")

    if cooling "$seat";                              then skipped="$skipped $name:cooling"; continue; fi
    if (( $(uses_in_window "$seat") >= $(seat_max "$seat") )); then skipped="$skipped $name:at-cap"; continue; fi
    if ! acquire "$seat"; then
      skipped="$skipped $name:busy"; busy_seats=$(( busy_seats + 1 )); continue
    fi
    held="$seat"

    log "using seat '$name' ($(uses_in_window "$seat")/$(seat_max "$seat") used this hour) run=$CR_RUN_ID"
    # Buffer the two streams separately: `--agent` mode emits JSON lines on stdout and
    # callers parse them, so CLI stderr must never be folded in. Replayed verbatim below.
    out=$(mktemp "${TMPDIR:-/tmp}/cr-review-out.XXXXXX"); err=$(mktemp "${TMPDIR:-/tmp}/cr-review-err.XXXXXX")
    run_cli "$seat" "$out" "$err" "$@"
    rc=$CLI_RC

    # Checked BEFORE the limit test and before any rotation. A parse
    # error is a property of OUR command line, not of this seat, so rotating
    # would replay the identical failure against every seat in the pool, taking
    # and releasing each lock for nothing -- and if a later seat then came back
    # rate-limited we would report 75, an AUTHORIZED SKIP, for a review that was
    # never once attempted. Replay the CLI's own diagnostic so the operator can
    # see which flag it choked on, leave the seat's state untouched (no use
    # recorded: no quota was spent), and stop.
    if argparse_failed "$err" "$rc"; then
      cat "$out"; cat "$err" >&2
      rm -f "$out" "$err"
      release "$seat"; held=""
      die_argparse
    fi

    if limited "$rc" "$out" "$err"; then
      printf '%s' "$(( $(now) + COOLDOWN ))" > "$seat/.limited_until"
      log "seat '$name' is rate-limited -- resting ${COOLDOWN}s, trying the next seat"
      rm -f "$out" "$err"; release "$seat"; held=""; skipped="$skipped $name:limited"
      continue
    fi

    cat "$out"; cat "$err" >&2
    now >> "$seat/.uses"
    rm -f "$seat/.limited_until" "$out" "$err"
    release "$seat"; held=""
    # A review actually ran: the CLI's own code is the verdict, clean or not --
    # except for the codes this wrapper reserves for its own verdicts. See
    # reserve_codes().
    rc=$(reserve_codes "$rc")
    finished=1
    exit "$rc"
  done

  # Nothing acquired. Two reasons are worth waiting for:
  #   * a BUSY seat        -- a live review holds the lock, clears in ~1-3 min.
  #   * an IMMINENT REFRESH -- the hourly cap is a ROLLING window, so a seat at
  #     5/5 regains a slot when its oldest use ages out. Waiting for that is only
  #     worth it if it lands inside our remaining budget.
  #
  # The refresh wait is OPT-IN (CR_SEAT_WAIT_REFRESH, secs; 0 = off) and off by
  # default ON PURPOSE. This wrapper is shared with INTERACTIVE work (PRlaunch,
  # /execute, deep-review); turning a fast, honest "pool is spent" into a silent
  # 20-minute hang would be a bad trade for a human waiting at a prompt. Batch
  # callers that genuinely prefer latency over failure (an unattended sweep
  # launcher) set it explicitly.
  remaining=$(( deadline - $(now) ))
  wait_reason=""
  if (( busy_seats > 0 )); then
    wait_reason="busy"
  elif (( REFRESH_WAIT > 0 )); then
    next_slot=-1
    for seat in "${ORDER[@]}"; do
      s2=$(secs_to_slot "$seat")
      (( next_slot < 0 || s2 < next_slot )) && next_slot=$s2
    done
    if (( next_slot >= 0 && next_slot <= REFRESH_WAIT && next_slot < remaining )); then
      wait_reason="refresh in ${next_slot}s"
    fi
  fi
  [[ -n "$wait_reason" ]] || break
  (( remaining > 0 )) || { log "waited ${WAIT_MAX}s for a seat -- giving up"; break; }

  if (( announced == 0 )); then
    log "no seat free (${skipped# }) -- queueing on ${wait_reason}, up to ${WAIT_MAX}s"
    announced=1
  fi
  # Jitter so simultaneously-launched workers don't retry in lockstep and
  # thundering-herd the same lock.
  sleep_for=$(( POLL + RANDOM % 10 ))
  (( sleep_for > remaining )) && sleep_for=$remaining
  sleep "$sleep_for"
done

log "ALL SEATS EXHAUSTED (${skipped:- none available}) run=$CR_RUN_ID -- record the authorized cr_cli skip"
# Every seat was looked at and every one was genuinely spent or busy. This is the
# ONE path allowed to authorize a skip, which is why nothing else may reach 75.
finished=1
exit 75
