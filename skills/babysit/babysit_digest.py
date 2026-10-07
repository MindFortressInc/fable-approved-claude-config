#!/usr/bin/env python3
"""babysit_digest.py -- catch-up digest for the babysit sweep.

Answers "what happened since I last looked". Folds two ALREADY-DURABLE
sources -- `~/.claude/automation-ledger.jsonl` (hooks/ledger-append.sh) and
`~/.claude/logs/headless-babysit.log` (launchd/headless-skill.sh,
hooks/babysit-fire-log.sh, launchd/babysit-hourly-gate.sh) -- into one
deterministic JSON document covering the window since this module's own
render mark. Stdlib only; makes NO `gh`/API calls and does NOT re-classify --
the same contract as babysit_classify.py: this module computes, the caller
renders. Callable from any interactive session that wants to catch up, or
from a sweep's report step.

Render mark: this module keeps its OWN mark (`~/.claude/lastdigest/<session_id>`,
DEFAULT_LASTDIGEST_DIR below), stamped once per render at the end of `main()`.
It is deliberately not shared with any per-prompt presence hook: a hook that
stamps on every prompt would overwrite the mark to "now" a moment before the
digest reads it, the gap would always read ~0, and `render_mode: "full"`
would be unreachable no matter how long the reader was actually away.

Usage:
  python3 babysit_digest.py [--since ISO8601|epoch] [--session-id ID]
                            [--ledger PATH] [--log PATH] [--lastdigest-dir DIR]

Env overrides (unset in production), mirroring babysit_classify.py:
  BABYSIT_NOW                ISO8601 UTC "now" (shared helper: now_dt())
  BABYSIT_LEDGER             path to automation-ledger.jsonl
  BABYSIT_LOG                path to headless-babysit.log
  BABYSIT_LASTDIGEST_DIR     dir holding <session_id> render-mark files
  BABYSIT_CADENCE_HOLE_MIN   minutes between sweep rows that count as a hole
"""
import argparse
import bisect
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)
from babysit_classify import parse_iso, iso, now_dt  # noqa: E402  (reuse, don't reinvent)

DEFAULT_LEDGER = os.path.expanduser("~/.claude/automation-ledger.jsonl")
DEFAULT_LOG = os.path.expanduser("~/.claude/logs/headless-babysit.log")
DEFAULT_LASTDIGEST_DIR = os.path.expanduser("~/.claude/lastdigest")


def _cadence_hole_min():
    """The public sweep is hourly (`/loop 1h /babysit-prs`, or the hourly
    launchd backstop). One interval of slack absorbs jitter and a long but
    healthy sweep; a gap of more than two hourly cycles (120 min) between
    consecutive `sweep` rows is the earliest point a missed sweep is
    distinguishable from jitter. Override with BABYSIT_CADENCE_HOLE_MIN when
    running a different cadence; a non-numeric value falls back to 120."""
    try:
        return float(os.environ.get("BABYSIT_CADENCE_HOLE_MIN", "120"))
    except ValueError:
        return 120.0


# How long since this session's last render (resolve_since below) before the
# digest says `render_mode: "full"` instead of a terse delta. This answers "was
# the reader away?", a different question from the cadence hole above ("did a
# sweep go missing?"), so the two use independent thresholds.
RENDER_FULL_GAP_MIN = 60

# Cold start (no render mark for this session -- first run, or a fresh
# terminal): fall back to a fixed lookback rather than refuse to render.
COLD_START_LOOKBACK_H = 2

# launchd/headless-skill.sh and hooks/babysit-fire-log.sh write these two lines
# per launch, in LOCAL time via `date '+%F %T'`:
#   `=== 2026-01-01 09:00:00 [babysit] fire: /babysit-prs no-loop`
#   `=== 2026-01-01 09:31:12 [babysit] exit=0`
# A prior command's unflushed stdout with no trailing newline can run straight
# into the "===" (e.g. "Execution error=== ... exit=124"), so these search
# anywhere in the line, never anchor to column 0.
FIRE_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[babysit\] fire: (.*)")
EXIT_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[babysit\] exit=(-?\d+)")

# launchd/babysit-hourly-gate.sh writes ONE of these per backstop slot, in
# LOCAL time and with NO `=== ` prefix:
#   `... [babysit] SKIP: interactive babysit alive (heartbeat age 507s < 4200s)`
#   `... [babysit] heartbeat stale (age 9000s >= 4200s) -> running backup`
#   `... [babysit] no heartbeat -> running backup`
# headless-skill.sh's own `SKIP: previous run still holds <lock>` carries no
# heartbeat age, so GATE_SKIP_RE deliberately does not match it.
GATE_SKIP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[babysit\] SKIP: .*?heartbeat age (\d+)s")
GATE_RUN_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[babysit\] (?:heartbeat stale|no heartbeat) .*running backup")
# The gate's own STALE_S (pinned equal by a test). A SKIP means "a heartbeat
# fresher than this exists", so the sweep that justifies it must fall inside it.
GATE_STALE_S = 4200


def _local_naive_to_utc(s):
    """Parse a naive 'YYYY-MM-DD HH:MM:SS' LOCAL-time string into a UTC-aware
    datetime.

    headless-babysit.log is the one source here that is NOT UTC: its writers
    use `date '+%F %T'` with no `-u`, unlike ledger-append.sh (`date -u`) and
    babysit_classify.py's iso()/now_dt(). `time.mktime` interprets the naive
    struct in the process's CURRENT local timezone, which is exactly how the
    line was produced -- self-consistent on whatever machine this runs on.
    """
    try:
        naive = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    epoch = time.mktime(naive.timetuple())
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


# ============================================================================
# readers -- each fails soft (missing/corrupt source -> empty). A digest is a
# hint, and a hint must never crash its caller.
# ============================================================================
def read_ledger(path):
    """All ledger rows with a parseable `ts`, sorted ascending. NOT windowed
    -- call in_window() separately so tests can inspect the full read."""
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                ts = parse_iso(d.get("ts", ""))
                if ts is None:
                    continue
                d["_ts"] = ts
                rows.append(d)
    except (FileNotFoundError, OSError):
        return []
    rows.sort(key=lambda r: r["_ts"])
    return rows


def in_window(rows, since_dt, now_dt_):
    return [r for r in rows if since_dt <= r["_ts"] <= now_dt_]


def read_log_events(path):
    """[{"kind": "fire"|"exit", "ts": dt, ...}], sorted ascending."""
    events = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = FIRE_RE.search(line)
                if m:
                    ts = _local_naive_to_utc(m.group(1))
                    if ts:
                        events.append({"kind": "fire", "ts": ts, "prompt": m.group(2).strip()})
                    continue
                m = EXIT_RE.search(line)
                if m:
                    ts = _local_naive_to_utc(m.group(1))
                    if ts:
                        events.append({"kind": "exit", "ts": ts, "rc": int(m.group(2))})
    except (FileNotFoundError, OSError):
        return []
    events.sort(key=lambda e: e["ts"])
    return events


def read_gate_events(path):
    """[{"kind": "skip"|"run", "ts": dt, ...}] from the backstop gate's lines,
    sorted ascending. Kept apart from read_log_events so `stall`/`coverage`
    are unaffected by gate lines."""
    events = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = GATE_SKIP_RE.search(line)
                if m:
                    ts = _local_naive_to_utc(m.group(1))
                    if ts:
                        events.append({"kind": "skip", "ts": ts, "heartbeat_age_s": int(m.group(2))})
                    continue
                m = GATE_RUN_RE.search(line)
                if m:
                    ts = _local_naive_to_utc(m.group(1))
                    if ts:
                        events.append({"kind": "run", "ts": ts})
    except (FileNotFoundError, OSError):
        return []
    events.sort(key=lambda e: e["ts"])
    return events


# ============================================================================
# fold sections
# ============================================================================
def detect_cadence_holes(ledger_rows_in_window):
    """More than the cadence threshold between two CONSECUTIVE `sweep` rows
    inside the window. Deliberately does NOT compare against `since`/`now` at
    the window edges -- a hole must be observed BETWEEN two actual sweeps, so
    a single long-but-healthy sweep (one row) never produces a hole."""
    limit = _cadence_hole_min()
    sweeps = [r for r in ledger_rows_in_window if r.get("event") == "sweep"]
    holes = []
    for a, b in zip(sweeps, sweeps[1:]):
        delta_min = (b["_ts"] - a["_ts"]).total_seconds() / 60.0
        if delta_min > limit:
            holes.append({"start": iso(a["_ts"]), "end": iso(b["_ts"]),
                          "minutes": round(delta_min, 1)})
    return holes


def detect_stalls(log_events, since_dt, now_dt_):
    """A `fire:` with no matching `exit=` before the next `fire:` (or before
    the window ends) is a stall -- the ledger alone can't see a sweep that
    died before writing its own row; that needs these fire/exit pairs."""
    windowed = [e for e in log_events if since_dt <= e["ts"] <= now_dt_]
    stalls = []
    pending = None
    for e in windowed:
        if e["kind"] == "fire":
            if pending is not None:
                stalls.append({"fired_at": iso(pending["ts"]),
                               "prompt": pending.get("prompt", ""),
                               "reason": "no exit before next fire"})
            pending = e
        elif e["kind"] == "exit":
            # An orphan exit (its fire predates `since`) is a window-boundary
            # artifact, not an anomaly; a matched exit clears the pending fire.
            pending = None
    if pending is not None:
        stalls.append({"fired_at": iso(pending["ts"]),
                       "prompt": pending.get("prompt", ""),
                       "reason": "no exit found in window"})
    return stalls


def assess_coverage(log_events, ledger_rows_in_window, since_dt, now_dt_):
    """Can `stall`/`cadence_holes` speak about this window AT ALL?

    `detect_stalls` returns `[]` both for "the log has rows and none stalled"
    and for "the log has no rows in this window"; a renderer that reads an
    empty `stall` as evidence of health would call the second case clean.
    Absence of data must not render as absence of problems:

      stall_no_data      ZERO fire/exit rows in the window -- `stall` is
                         vacuous, not clean.
      cadence_no_data    fewer than two `sweep` rows in the window. A hole is
                         only observable BETWEEN two consecutive sweeps, so
                         `cadence_holes: []` is likewise vacuous.
      unobserved_sweeps  the ledger proves sweeps ran, yet the log recorded
                         NONE of them -- the cadence is driven by a launcher
                         that does not emit fire/exit (an interactive `/loop`
                         session writes ledger rows but no fire/exit lines),
                         so a death mid-sweep would be invisible to `stall`.
      partially_observed the same at partial strength: FEWER fires than
                         sweeps (e.g. headless and interactive sweeps mixed).
                         Counts fires only, never exits: an exit without its
                         fire is a window-boundary artifact, not coverage.
    """
    windowed = [e for e in log_events if since_dt <= e["ts"] <= now_dt_]
    fires = [e for e in windowed if e["kind"] == "fire"]
    sweep_rows = [r for r in ledger_rows_in_window if r.get("event") == "sweep"]
    return {
        "fire_exit_rows": len(windowed),
        "fire_rows": len(fires),
        "sweep_rows": len(sweep_rows),
        "stall_no_data": len(windowed) == 0,
        "cadence_no_data": len(sweep_rows) < 2,
        "unobserved_sweeps": len(sweep_rows) > 0 and len(windowed) == 0,
        "partially_observed": len(sweep_rows) > 0 and len(fires) < len(sweep_rows),
    }


def build_gate(gate_events, log_events, all_ledger_rows, since_dt, now_dt_):
    """Did the backstop stand down for a sweep that actually ran?

    The gate SKIPS on a fresh heartbeat and records nothing else.
    hooks/babysit-heartbeat.py bumps that heartbeat on EVERY turn of a session
    that invoked /babysit-prs, including turns that run no sweep, so a session
    that stays chatty but stops sweeping suppresses the backstop with no fire:,
    no exit= and no ledger row -- invisible to `stall`, `coverage` and
    `cadence_holes`. A skip is BACKED when a `fire:` line or a ledger `sweep`
    row lands in the GATE_STALE_S before it (looked up across the WHOLE
    log/ledger, since the backing sweep can predate `since`). A fire whose
    paired exit= landed before the skip counts only if it was rc=0 with a
    `sweep` row behind it (before the next fire): a failed launch or a
    single-turn forfeit (rc=0, no row -- see hooks/babysit-fire-log.sh
    `verdict`) proves nothing. Ledger rows count on their own because an
    interactive session writes them but no fire: line. An UNBACKED skip is a
    slot where nothing was sweeping.

      decisions / skipped / ran  gate lines in the window, by outcome.
      unbacked                   [{skipped_at, heartbeat_age_s}] per dark slot.
      suppressed_backstop        any unbacked skip -- the alarm.
      no_data                    the gate wrote nothing in the window, so
                                 `suppressed_backstop: false` is vacuous.
    """
    windowed = [e for e in gate_events if since_dt <= e["ts"] <= now_dt_]
    skips = [e for e in windowed if e["kind"] == "skip"]
    # (instant, failed_at): a fire stops backing once its paired exit= (the
    # first exit before the next fire) lands, UNLESS that exit is rc=0 AND a
    # ledger `sweep` row landed at/after the fire and before the next fire. A
    # LOCKED collision keeps backing once the lock owner's row lands before
    # the next fire. In-flight fires (no exit yet) keep backing.
    sweep_ts = sorted(r["_ts"] for r in all_ledger_rows if r.get("event") == "sweep")
    backing = [(t, None) for t in sweep_ts]
    runs = []  # [fire_ts, exit_ts, rc]; exit_ts/rc None while in flight
    for e in log_events:
        if e["kind"] == "fire":
            runs.append([e["ts"], None, None])
        elif runs and runs[-1][1] is None:
            runs[-1][1], runs[-1][2] = e["ts"], e["rc"]
    for i, (fire, exited, rc) in enumerate(runs):
        if exited is None:
            backing.append((fire, None))
            continue
        lo = bisect.bisect_left(sweep_ts, fire)
        swept = (lo < len(sweep_ts)
                 and (i + 1 == len(runs) or sweep_ts[lo] < runs[i + 1][0]))
        backing.append((fire, None if rc == 0 and swept else exited))
    backing.sort(key=lambda b: b[0])
    starts = [b[0] for b in backing]
    lookback = timedelta(seconds=GATE_STALE_S)

    def backed(ts):
        lo = bisect.bisect_left(starts, ts - lookback)
        hi = bisect.bisect_right(starts, ts)
        return any(failed is None or failed > ts for _, failed in backing[lo:hi])

    unbacked = [{"skipped_at": iso(e["ts"]), "heartbeat_age_s": e["heartbeat_age_s"]}
                for e in skips if not backed(e["ts"])]
    return {
        "decisions": len(windowed),
        "skipped": len(skips),
        "ran": len(windowed) - len(skips),
        "unbacked": unbacked,
        "suppressed_backstop": bool(unbacked),
        "no_data": not windowed,
    }


def build_shipped(ledger_rows_in_window):
    """`unit` rows are /PRlaunch's per-PR "opened" record. `unit_amendment`
    (a fix to an already-opened PR) is deliberately excluded -- it is not a
    NEW opening and would double count."""
    return {
        "opened": [
            {"repo": r.get("repo"), "pr": r.get("pr"), "gates": r.get("gates"),
             "ts": iso(r["_ts"])}
            for r in ledger_rows_in_window if r.get("event") == "unit"
        ],
    }


def build_queue(ledger_rows_in_window):
    """Pending trajectory (first sweep's `pending` -> last sweep's), decision
    streak (how many trailing sweeps in-window share the latest `decision`),
    and totals. With no sweep rows in the window every field is still
    present, so the renderer never meets a missing key."""
    sweeps = [r for r in ledger_rows_in_window if r.get("event") == "sweep"]
    if not sweeps:
        return {"sweeps": 0, "pending_first": None, "pending_last": None,
                "pending_delta": None, "bumps": 0, "fixes": 0,
                "decision": None, "decision_streak": 0}
    pending_first = sweeps[0].get("pending")
    pending_last = sweeps[-1].get("pending")
    delta = None
    if isinstance(pending_first, (int, float)) and isinstance(pending_last, (int, float)):
        delta = pending_last - pending_first
    last_decision = sweeps[-1].get("decision")
    streak = 0
    for s in reversed(sweeps):
        if s.get("decision") != last_decision:
            break
        streak += 1
    return {
        "sweeps": len(sweeps),
        "pending_first": pending_first, "pending_last": pending_last,
        "pending_delta": delta,
        "bumps": sum(int(s.get("bumps") or 0) for s in sweeps),
        "fixes": sum(int(s.get("fixes") or 0) for s in sweeps),
        "decision": last_decision, "decision_streak": streak,
    }


def build_alarms(ledger_rows_in_window):
    """Ledger-derived alarms for the window.

      red_ci         MAX over the window's `sweep` rows, not the latest: a
                     red-CI spike that self-resolved before the last sweep
                     still happened while the reader was away. None when no
                     sweep row carries a number (unknown, not zero).
      cleanup_depth  the latest `cleanup_depth` row's count (/wrapup).
    """
    sweeps = [r for r in ledger_rows_in_window if r.get("event") == "sweep"]
    red_ci_values = [s.get("red_ci") for s in sweeps if isinstance(s.get("red_ci"), (int, float))]
    cleanups = [r for r in ledger_rows_in_window if r.get("event") == "cleanup_depth"]
    return {
        "red_ci": max(red_ci_values) if red_ci_values else None,
        "cleanup_depth": cleanups[-1].get("count") if cleanups else None,
    }


# ============================================================================
# since-resolution + top-level fold
# ============================================================================
def _safe_session_id(session_id):
    """session_id becomes a path element: an absolute or ".."-relative value
    would let os.path.join escape lastdigest_dir (a leading "/" discards the
    base dir outright)."""
    return bool(session_id) and "/" not in session_id and session_id not in (".", "..")


def resolve_since(explicit_since, lastdigest_dir, session_id, now):
    """(since_dt, cold_start, source). Precedence: explicit --since > this
    module's own last-render mark > a COLD_START_LOOKBACK_H lookback.

    The mark read here was always written by a PRIOR render (main() stamps it
    after emitting), never by the current turn, so the gap is real."""
    if explicit_since:
        dt = parse_iso(explicit_since)
        if dt is None:
            try:
                dt = datetime.fromtimestamp(float(explicit_since), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError, OSError):
                dt = None
        if dt is not None:
            return dt, False, "explicit"
    if _safe_session_id(session_id):
        try:
            with open(os.path.join(lastdigest_dir, session_id)) as fh:
                dt = parse_iso(fh.read().strip())
            if dt is not None:
                return dt, False, "lastdigest"
        except (OSError, ValueError):  # ValueError: a NUL byte in session_id
            pass
    return now - timedelta(hours=COLD_START_LOOKBACK_H), True, "cold_start_2h_lookback"


def _stamp_lastdigest(lastdigest_dir, session_id, now_dt_):
    """Atomically stamp `now_dt_` as this session's last-render mark: a
    per-writer-unique tmp name + os.replace, so a concurrent reader never
    observes a torn file. Fails soft -- a mark write must never crash a
    render."""
    if not _safe_session_id(session_id):
        return
    try:
        os.makedirs(lastdigest_dir, exist_ok=True)
        target = os.path.join(lastdigest_dir, session_id)
        tmp = "%s.tmp.%d.%d" % (target, os.getpid(), int(time.time() * 1e6))
        with open(tmp, "w") as fh:
            fh.write(iso(now_dt_) + "\n")
        os.replace(tmp, target)
    except Exception:
        pass


def fold(*, since_dt, now_dt_, ledger_path, log_path, cold_start=False):
    """The deterministic fold. Pure given its inputs (paths in, JSON-able
    dict out) -- no `gh`/API calls, no re-classification; every source is
    something a sweep or launcher already wrote."""
    all_ledger_rows = read_ledger(ledger_path)
    ledger_rows = in_window(all_ledger_rows, since_dt, now_dt_)
    log_events = read_log_events(log_path)
    gap_minutes = (now_dt_ - since_dt).total_seconds() / 60.0
    return {
        "since": iso(since_dt),
        "now": iso(now_dt_),
        "gap_minutes": round(gap_minutes, 1),
        "cold_start": cold_start,
        "render_mode": "full" if gap_minutes > RENDER_FULL_GAP_MIN else "terse",
        "gap": {"cadence_holes": detect_cadence_holes(ledger_rows)},
        "stall": detect_stalls(log_events, since_dt, now_dt_),
        # Whether `stall`/`gap` can speak about this window at all. A sibling,
        # not folded into `stall`, so `stall` stays a plain list.
        "coverage": assess_coverage(log_events, ledger_rows, since_dt, now_dt_),
        "gate": build_gate(read_gate_events(log_path), log_events, all_ledger_rows,
                           since_dt, now_dt_),
        "shipped": build_shipped(ledger_rows),
        "queue": build_queue(ledger_rows),
        "alarms": build_alarms(ledger_rows),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="babysit_digest.py")
    ap.add_argument("--since", default=None,
                    help="ISO8601 or epoch seconds; overrides this module's own render mark")
    ap.add_argument("--session-id", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
                    help="session id whose render mark to read/write "
                         "(default: $CLAUDE_CODE_SESSION_ID)")
    ap.add_argument("--ledger", default=os.environ.get("BABYSIT_LEDGER", DEFAULT_LEDGER))
    ap.add_argument("--log", default=os.environ.get("BABYSIT_LOG", DEFAULT_LOG))
    ap.add_argument("--lastdigest-dir", default=os.environ.get(
        "BABYSIT_LASTDIGEST_DIR", DEFAULT_LASTDIGEST_DIR))
    args = ap.parse_args(argv)

    now = now_dt()
    since_dt, cold_start, source = resolve_since(
        args.since, args.lastdigest_dir, args.session_id, now)
    out = fold(since_dt=since_dt, now_dt_=now, ledger_path=args.ledger,
               log_path=args.log, cold_start=cold_start)
    out["since_source"] = source
    sys.stdout.write(json.dumps(out) + "\n")
    # Stamp AFTER emitting, on every invocation (--since or not): this is the
    # mark the NEXT invocation's resolve_since() reads.
    _stamp_lastdigest(args.lastdigest_dir, args.session_id, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
