#!/usr/bin/env bash
# cr-seats.sh -- manage the CodeRabbit seat pool used by cr-review.sh.
#
# CodeRabbit rate-limits PER DEVELOPER, not per org (docs: Pro = 5 PR/hr + 5 IDE/hr
# + 5 CLI/hr, three separate rolling buckets per seat). Every worker on this machine
# authenticates as one identity, so all of them share one bucket. One seat dir per
# teammate turns that into N buckets.
#
# The CLI reads credentials from $HOME/.coderabbit/auth.json, so a seat is just a
# fake HOME:  ~/.claude/cr-seats/<name>/.coderabbit/auth.json
#
#   cr-seats.sh add <name>                register a seat from an agentic API key
#                                         (key on stdin or in $CR_SEAT_API_KEY)
#   cr-seats.sh adopt <name>              register a seat from the current ~/.coderabbit
#   cr-seats.sh list                      show seats + hourly usage
#   cr-seats.sh headroom [horizon_s]     slots usable now (+ refreshing within horizon)
#   cr-seats.sh next-slot                 secs until the pool's next slot frees
#   cr-seats.sh remove <name>             drop a seat
set -euo pipefail

SEATS_DIR="${CR_SEATS_DIR:-$HOME/.claude/cr-seats}"
HOURLY_MAX="${CR_SEAT_HOURLY_MAX:-10}"  # top-of-plan CLI reviews/hr/developer.
# NOT lowered to a guessed Fair Usage tier: the real hourly
# rate degrades with trailing-7-day volume and is unobservable from here, so a
# hardcoded floor would throttle us on days the allowance is genuinely there.
# Ask for the full rate and let CodeRabbit refuse -- a refusal is free, and the
# per-seat cooldown below IS the backoff. Seats being refused at "3/10" is that
# working, not a miscount.

now() { date +%s; }

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
    v=$(tr -dc '0-9' < "$f" 2>/dev/null)
    [[ -n "$v" && "$v" != 0 ]] && { printf '%s' "$v"; return; }
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

die() { printf 'cr-seats: %s\n' "$1" >&2; exit 1; }

# Seat names become directory names that `remove` deletes with rm -rf, so only
# a plain name is accepted -- never `.`, `..`, a slash, or an empty string.
seat_home() {
  case "$1" in
    ''|.|..|*[!A-Za-z0-9._-]*) die "invalid seat name '$1' (use letters, digits, . _ -)" ;;
  esac
  printf '%s/%s' "$SEATS_DIR" "$1"
}

# Reviews this seat's identity made in the last 3600s.
#
# `.uses` only records launches cr-review.sh made, so a bare `coderabbit review`
# spends the account's hourly allowance without leaving a line — which is how a
# seat displayed "0/5 used" and was then rejected as rate-limited (2026-07-27).
# The CLI writes one dir per review under $HOME/.coderabbit/reviews and its mtime
# is the review time, so count those too and report the MAX (must match
# cr-review.sh's uses_in_window, or `list` and the router disagree).
recent_uses() {
  local uses="$1/.uses" cutoff a=0 b=0
  cutoff=$(( $(now) - 3600 ))
  [[ -f "$uses" ]] && a=$(awk -v c="$cutoff" '$1 >= c' "$uses" | wc -l | tr -d ' ')
  b=$(review_times "$1" | wc -l | tr -d ' ')
  (( b > a )) && a="$b"
  printf '%s' "$a"
}

link_gitconfig() {
  # coderabbit shells out to git; give the seat HOME the real git identity/config.
  local h="$1"
  [[ -e "$h/.gitconfig" ]] || [[ ! -f "$HOME/.gitconfig" ]] || ln -s "$HOME/.gitconfig" "$h/.gitconfig"
}

cmd_add() {
  # The key is read from $CR_SEAT_API_KEY or stdin, so it never sits in this
  # script's argv or your shell history. (A positional key still works, for old
  # callers, but is visible in `ps`.) `coderabbit auth login` itself only takes
  # the key as an argument, so it is briefly on that child's argv either way.
  local name="${1:-}" key="${2:-${CR_SEAT_API_KEY:-}}"
  [[ -n "$name" ]] || die "usage: cr-seats.sh add <name>   (key on stdin or in \$CR_SEAT_API_KEY)"
  if [[ -z "$key" ]]; then
    [[ -t 0 ]] && printf 'CodeRabbit API key for %s: ' "$name" >&2
    IFS= read -rs key || true
    [[ -t 0 ]] && printf '\n' >&2
  fi
  [[ -n "$key" ]] || die "no API key given for '$name' (stdin or \$CR_SEAT_API_KEY)"
  local h; h=$(seat_home "$name") || exit 1
  mkdir -p "$h"; chmod 700 "$h"
  link_gitconfig "$h"
  HOME="$h" coderabbit auth login --api-key "$key" >/dev/null 2>&1 \
    || die "auth login failed for '$name' -- check the key"
  [[ -f "$h/.coderabbit/auth.json" ]] || die "no auth.json written for '$name'"
  chmod 600 "$h/.coderabbit/auth.json"
  printf 'added seat: %s\n' "$name"
  HOME="$h" coderabbit auth status 2>&1 | sed 's/^/  /'
}

cmd_adopt() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "usage: cr-seats.sh adopt <name>"
  [[ -f "$HOME/.coderabbit/auth.json" ]] || die "no ~/.coderabbit/auth.json to adopt"
  local h; h=$(seat_home "$name") || exit 1
  mkdir -p "$h/.coderabbit"; chmod 700 "$h"
  cp "$HOME/.coderabbit/auth.json" "$h/.coderabbit/auth.json"
  chmod 600 "$h/.coderabbit/auth.json"
  link_gitconfig "$h"
  printf 'adopted current credentials as seat: %s\n' "$name"
  HOME="$h" coderabbit auth status 2>&1 | sed 's/^/  /'
}

cmd_remove() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "usage: cr-seats.sh remove <name>"
  local h; h=$(seat_home "$name") || exit 1
  [[ -d "$h" ]] || die "no such seat: $name"
  rm -rf "$h"
  printf 'removed seat: %s\n' "$name"
}

cmd_list() {
  [[ -d "$SEATS_DIR" ]] || { printf 'no seats registered (%s)\n' "$SEATS_DIR"; return; }
  local found=0 now; now=$(date +%s)
  printf '%-12s %-28s %-10s %s\n' SEAT ACCOUNT 'USED/HR' STATE
  for h in "$SEATS_DIR"/*/; do
    [[ -f "$h/.coderabbit/auth.json" ]] || continue
    found=1
    local name acct used smax state until
    name=$(basename "$h")
    acct=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("user",{}).get("user_name","?"))' \
             "$h/.coderabbit/auth.json" 2>/dev/null || echo '?')
    used=$(recent_uses "${h%/}")
    smax=$(seat_max "${h%/}")
    state=ready
    until=0; [[ -f "$h/.limited_until" ]] && until=$(cat "$h/.limited_until")
    if (( until > now )); then state="cooling ($((until - now))s)"
    elif (( used >= smax )); then state="at cap"
    fi
    [[ -d "$h/.lock" ]] && state="$state, busy"
    printf '%-12s %-28s %-10s %s\n' "$name" "$acct" "$used/$smax" "$state"
  done
  (( found )) || printf 'no seats registered (%s)\n' "$SEATS_DIR"
}

# How many reviews can actually START — ONE integer on stdout. This is
# deliberately NOT the same question as a sweep's launch cap:
#   launch cap = how many launches your SHARE allows (a fairness ration).
#   headroom   = how many the pool can physically absorb (a waste guard).
# Firing capped launches into zero headroom is what wasted three batches in one
# night: each one exits immediately having done nothing, and any that DO
# start burn a slot the retry then cannot use.
#
# The counted/skipped split MIRRORS cr-review.sh's own seat loop exactly (see its
# comment block at "at-cap / cooling ... busy"), because a headroom that disagrees
# with the router is worse than none:
#   * BUSY seats ARE counted. A live review holds the lock for ~1-3 min and
#     cr-review.sh deliberately WAITS it out, so a busy seat with quota
#     left will serve a queued launch.
#   * COOLING and AT-CAP seats are NOT counted, however short the cooldown.
#     Their QUOTA is spent; cr-review.sh treats both as terminal and fails fast
#     ("ALL SEATS EXHAUSTED") rather than waiting -- `(( busy_seats > 0 )) || break`.
#     An earlier version of this function counted a cooling seat whose timer was
#     shorter than the caller's wait window; that read headroom=2 against a pool of
#     `bravo:cooling charlie:at-cap alpha:at-cap` and both launches were refused on the
#     spot. Cooling is never headroom.
#
# The optional arg is a REFRESH HORIZON in seconds (default 0 = free right now).
# The hourly cap is a ROLLING window, so a seat at 5/5 regains a slot when its
# oldest in-window use ages past 3600s. With a horizon > 0 those imminent slots are
# counted too -- but ONLY pass one if the launched review will actually wait for it
# (cr-review.sh CR_SEAT_WAIT_REFRESH). Counting capacity nobody waits for is how
# headroom lies.
cmd_headroom() {
  local horizon="${1:-0}" now total=0
  [[ -d "$SEATS_DIR" ]] || { printf '0\n'; return; }
  now=$(date +%s)
  for h in "$SEATS_DIR"/*/; do
    [[ -f "$h/.coderabbit/auth.json" ]] || continue
    local used remaining until slot_in
    used=$(recent_uses "${h%/}")
    remaining=$(( $(seat_max "${h%/}") - used ))
    slot_in=$(seat_secs_to_slot "${h%/}")
    if (( remaining > 0 )); then
      until=0; [[ -f "$h/.limited_until" ]] && until=$(cat "$h/.limited_until" 2>/dev/null || echo 0)
      if (( until <= now )); then
        total=$(( total + remaining ))         # free right now
        continue
      fi
    fi
    # Not usable now. Count ONE imminent slot if it lands inside the horizon.
    (( horizon > 0 && slot_in > 0 && slot_in <= horizon )) && total=$(( total + 1 ))
  done
  printf '%s\n' "$total"
}

# Seconds until <seat_dir> gains a quota slot (0 = has one now).
# MUST match cr-review.sh's secs_to_slot -- if `headroom` and the router disagree
# about when a seat frees, headroom promises capacity the router won't wait for.
seat_secs_to_slot() {
  local seat="$1" now smax used need oldest refresh=0 cool
  now=$(date +%s)
  used=$(recent_uses "$seat")
  smax=$(seat_max "$seat")
  if (( used >= smax )); then
    need=$(( used - smax + 1 ))
    oldest=$( { awk -v c="$(( now - 3600 ))" '$1 >= c {print $1}' "$seat/.uses" 2>/dev/null
                review_times "$seat"; } | sort -n | sed -n "${need}p" )
    if [[ -n "$oldest" ]]; then refresh=$(( oldest + 3600 - now )); else refresh=3600; fi
    (( refresh < 0 )) && refresh=0
  fi
  if [[ -f "$seat/.limited_until" ]]; then
    cool=$(( $(cat "$seat/.limited_until" 2>/dev/null || echo 0) - now ))
    (( cool > refresh )) && refresh=$cool
  fi
  (( refresh < 0 )) && refresh=0
  printf '%s' "$refresh"
}

# Seconds until the pool's NEXT slot frees, across all seats (for reporting).
cmd_next_slot() {
  local best=-1 s
  [[ -d "$SEATS_DIR" ]] || { printf '\n'; return; }
  for h in "$SEATS_DIR"/*/; do
    [[ -f "$h/.coderabbit/auth.json" ]] || continue
    s=$(seat_secs_to_slot "${h%/}")
    (( best < 0 || s < best )) && best=$s
  done
  printf '%s\n' "$best"
}

case "${1:-list}" in
  add)       shift; cmd_add "$@" ;;
  adopt)     shift; cmd_adopt "$@" ;;
  remove)    shift; cmd_remove "$@" ;;
  list)      cmd_list ;;
  headroom)  shift; cmd_headroom "$@" ;;
  next-slot) cmd_next_slot ;;
  *)         die "unknown command '${1}' (add|adopt|list|headroom|next-slot|remove)" ;;
esac
