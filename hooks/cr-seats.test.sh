#!/usr/bin/env bash
# Covers two seat-pool fixes: per-seat hourly ceilings, and counting REAL
# reviews (<repo>/<branch>/reviews/<epoch_ms>) instead of depth-1 repo dirs.
#
# Run against the OLD scripts too -- T9 and T10 MUST fail there, or they are
# vacuous and prove nothing about the fix.
#
#   cr-seats.test.sh [<cr-review.sh> <cr-seats.sh>]   (default: the siblings)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAP="${1:-$HERE/cr-review.sh}"
SEATSH="${2:-$HERE/cr-seats.sh}"
SCRATCH="${TMPDIR:-/tmp}/cr-percap.$$"
rm -rf "$SCRATCH"; mkdir -p "$SCRATCH/bin" "$SCRATCH/seats"
trap 'rm -rf "$SCRATCH"' EXIT
export CR_SEATS_DIR="$SCRATCH/seats"
export CR_SEAT_HOURLY_MAX=10          # the GLOBAL fallback
export CR_SEAT_WAIT_MAX=0             # fail fast; we are not testing queueing

cat > "$SCRATCH/bin/coderabbit" <<'STUB'
#!/usr/bin/env bash
echo "STUB invoked as seat=$(basename "$HOME")"
echo "Review complete. 0 issues found."
STUB
chmod +x "$SCRATCH/bin/coderabbit"
export PATH="$SCRATCH/bin:$PATH"

for s in alpha bravo charlie; do
  mkdir -p "$SCRATCH/seats/$s/.coderabbit"
  printf '{"user":{"user_name":"%s"}}' "$s" > "$SCRATCH/seats/$s/.coderabbit/auth.json"
done

# n reviews for <seat>, all under ONE repo+branch hash, minutes_ago apart.
# This is the exact shape the old counter collapsed to 1.
seed_reviews() {
  local seat="$1" n="$2" base d i ms
  base="$SCRATCH/seats/$seat/.coderabbit/reviews/repohash1/branchhash1/reviews"
  mkdir -p "$base"
  for ((i=0; i<n; i++)); do
    ms=$(( ( $(date +%s) - (i+1)*120 ) * 1000 ))   # 2,4,6... min ago
    d="$base/$ms"; mkdir -p "$d"; : > "$d/git.json"
  done
}

pass=0; fail=0
ok()   { echo "  PASS: $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL: $1"; fail=$((fail+1)); }

echo "T9: 5 reviews of the SAME repo count as 5, not 1 (depth-4 epoch-ms names)"
seed_reviews charlie 5
printf '5\n' > "$SCRATCH/seats/charlie/.hourly_max"
o=$("$SEATSH" list 2>&1)
if grep -qE '^charlie +[^ ]+ +5/5' <<<"$o"; then ok "cr-seats list reports charlie 5/5"
else bad "cr-seats list did not report 5/5 for charlie"; echo "$o" | sed 's/^/        /'; fi

echo "T10: a seat at its OWN ceiling (5) is skipped though the global max is 10"
# Give the other two a use each. Without this the old counter reads charlie as 1/10 --
# tied with alpha/bravo -- and the ranker happens to pick another seat anyway, so
# the test would PASS against the broken code for the wrong reason. With the tie
# broken, only a counter that sees charlie's real 5 keeps charlie out.
date +%s > "$SCRATCH/seats/alpha/.uses"
date +%s > "$SCRATCH/seats/bravo/.uses"
o=$("$WRAP" --base main 2>&1)
if grep -qF "seat=charlie" <<<"$o"; then bad "used charlie, which is at its per-seat cap"
else ok "at-per-seat-cap seat skipped"; fi
if grep -qE "seat=(alpha|bravo)" <<<"$o"; then ok "fell through to an uncapped seat"
else bad "no seat ran at all"; echo "$o" | sed 's/^/        /'; fi

echo "T11: no .hourly_max -> global fallback still applies (alpha at 10 of 10)"
seed_reviews alpha 10
o=$("$SEATSH" list 2>&1)
if grep -qE '^alpha +[^ ]+ +10/10' <<<"$o"; then ok "alpha falls back to the global 10"
else bad "alpha did not fall back to 10"; echo "$o" | sed 's/^/        /'; fi

echo "T12: headroom counts only what is genuinely free"
# charlie is at 5/5 and alpha at 10/10, so ONLY bravo can contribute. Derive the
# expectation from bravo's actual usage rather than hardcoding it -- T10's run
# consumed a slot, and asserting a constant here just encodes that accident.
ju=$("$SEATSH" list 2>/dev/null | awk '$1=="bravo"{split($3,a,"/"); print a[1]}')
want=$(( 10 - ${ju:-0} ))
h=$("$SEATSH" headroom 2>&1)
if [[ "$h" == "$want" ]]; then ok "headroom=$h (bravo's $ju/10 only; charlie+alpha capped)"
else bad "headroom=$h, want $want (bravo used=$ju)"; fi

echo; echo "RESULT: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
