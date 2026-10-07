#!/usr/bin/env bash
# ledger-append.sh — append one validated JSON record to the automation ledger.
#
# The automation fleet (bulldozer, babysit-prs, PRlaunch,
# wrapup) calls this once per result/event so there is a DURABLE quality record
# that /scorecard aggregates (run state otherwise lives only in /tmp and is lost).
#
# Interface — matched EXACTLY by every call site (bulldozer Step 2.4 is canonical):
#     ~/.claude/hooks/ledger-append.sh '<one JSON object string>'
# Exactly ONE argument, a JSON object. A `ts` (ISO8601 UTC) is added when absent;
# an existing `ts` is preserved. `launch_id` / `skill` are stamped from
# $BABYSIT_LAUNCH_ID / $LEDGER_SKILL when those are exported and the payload
# lacks the key (see below). The record is appended as one compact line to
# $HOME/.claude/automation-ledger.jsonl.
#
# This is a VALIDATOR, not a fail-open safety hook. A bad payload (missing arg,
# non-JSON, non-object) fails LOUDLY: it exits 1 with a one-line stderr message
# and writes NOTHING. It never partial-writes. Do NOT change this to exit 0 on
# bad input — a silently-swallowed record defeats the measurement loop.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "ledger-append: expected exactly 1 arg (a JSON object), got $#" >&2
  exit 1
fi

payload="$1"

# Must parse AND be a JSON object. `jq -e` exits nonzero on a false/null result
# or a parse error, so this one gate covers non-JSON, arrays, scalars, and null.
if ! printf '%s' "$payload" | jq -e 'type == "object"' >/dev/null 2>&1; then
  echo "ledger-append: argument is not a JSON object" >&2
  exit 1
fi

# Add ts / launch_id / skill only if ABSENT; emit one compact line. Computed
# BEFORE the append, so a jq failure here aborts (set -e) without touching the
# ledger. All three stamps fill a gap and never overwrite what the caller stated.
#
# This is the ONE choke point every call site goes through, so the two optional
# stamps live here rather than at each caller:
#   * launch_id ($BABYSIT_LAUNCH_ID) makes a row joinable BY LOOKUP to the log
#     slice of the unattended run (sweep launcher) that wrote it -- instead of a
#     manual timestamp-window scan. Whatever launcher mints the id stamps the same
#     value into its own log.
#   * skill ($LEDGER_SKILL) answers "which arm wrote this" for callers whose
#     payload shape has no `skill` field (e.g. a worker's result JSON appended
#     verbatim), so readers stop recovering it structurally.
#
# Both are ADDITIVE and only ever appear when the caller exported the variable --
# a hand-run invocation gets no key at all, never a `skill: null`. Ledger readers
# should access rows through .get()-style lookups with no key whitelist, so a new
# key cannot break them.
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# The launch_id is sanitised to [A-Za-z0-9._-]. A launcher that stamps the same id
# into its own log must apply the SAME class, or the two stores hold different
# keys for one run and the join silently resolves to nothing -- worse than no
# join at all, because it looks like one. An id that sanitises to empty is
# skipped rather than written as a blank key.
line="$(printf '%s' "$payload" | jq -c \
  --arg ts "$ts" \
  --arg launch_id "${BABYSIT_LAUNCH_ID:-}" \
  --arg skill "${LEDGER_SKILL:-}" '
    ($launch_id | gsub("[^A-Za-z0-9._-]"; "")) as $lid
  | (if has("ts")        then . else . + {ts: $ts}               end)
  | (if $lid   == "" or has("launch_id") then . else . + {launch_id: $lid}     end)
  | (if $skill == "" or has("skill")     then . else . + {skill: $skill}       end)
')"

ledger="$HOME/.claude/automation-ledger.jsonl"
mkdir -p "$(dirname "$ledger")"
printf '%s\n' "$line" >>"$ledger"
