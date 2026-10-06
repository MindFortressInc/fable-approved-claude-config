#!/bin/bash
# resume-fleet v0.1 — wait until ~the usage-limit reset, then run resume_fleet in short
# retry rounds that bracket the exact reset moment. Sends Esc+continue only to sessions
# BLOCKED on the limit popup. Stops after N consecutive empty rounds (fleet settled).
#
# Run it detached so it survives the launching session, e.g.:
#   TARGET=$(date -j -f "%Y-%m-%d %H:%M" "2026-07-12 01:50" +%s) \
#     nohup caffeinate -i env TARGET="$TARGET" bash resume_scheduler.sh >/dev/null 2>&1 &
#
# CONFIG (env): TARGET (epoch of first fire; default +3h), ROUNDS, GAP (sec), DRY_STOP,
#   plus everything resume_fleet.sh reads (EDITOR_APP/EDITOR_PROC/NTABS/LOG).

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET="${FLEET:-$HERE/resume_fleet.sh}"
LOG="${LOG:-$HOME/.claude/resume_fleet.log}"

TARGET="${TARGET:-$(( $(date +%s) + 10800 ))}"
ROUNDS="${ROUNDS:-10}"
GAP="${GAP:-600}"
DRY_STOP="${DRY_STOP:-2}"

now=$(date +%s); wait=$(( TARGET - now )); [ "$wait" -lt 0 ] && wait=0
echo "[$(date '+%F %H:%M:%S')] scheduler armed: first fire $(date -r "$TARGET" '+%H:%M'), up to $ROUNDS rounds/$((GAP/60))m" >> "$LOG"
sleep "$wait"

dry=0
for r in $(seq 1 "$ROUNDS"); do
  echo "[$(date '+%H:%M:%S')] --- retry round $r/$ROUNDS ---" >> "$LOG"
  blocked="$(MODE=act LOG="$LOG" bash "$FLEET" | tail -n1)"
  # A failed fleet run yields empty/non-numeric output — that's a failure, not
  # "0 blocked": don't let it feed the dry streak and end the schedule early.
  if ! [[ "$blocked" =~ ^[0-9]+$ ]]; then
    echo "[$(date '+%H:%M:%S')] round $r: fleet run FAILED (no count) — not counting as dry" >> "$LOG"
    dry=0   # a failure breaks consecutiveness: two dry rounds must be adjacent
    [ "$r" -lt "$ROUNDS" ] && sleep "$GAP"
    continue
  fi
  if [ "$blocked" -eq 0 ]; then
    dry=$((dry+1))
    echo "[$(date '+%H:%M:%S')] round $r: 0 blocked (dry streak $dry/$DRY_STOP)" >> "$LOG"
    [ "$dry" -ge "$DRY_STOP" ] && { echo "[$(date '+%H:%M:%S')] settled -> stopping" >> "$LOG"; break; }
  else
    dry=0
  fi
  [ "$r" -lt "$ROUNDS" ] && sleep "$GAP"
done
echo "[$(date '+%F %H:%M:%S')] scheduler exit" >> "$LOG"