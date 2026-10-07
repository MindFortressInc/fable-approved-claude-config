"""Tests for skills/babysit/babysit_digest.py -- the catch-up digest.

Every test runs against SYNTHETIC fixtures in a temp dir, never the live
~/.claude/automation-ledger.jsonl or ~/.claude/logs/headless-babysit.log: a
real sweep may be appending to those concurrently.

Ledger rows use the shapes the public writers produce (commands/babysit-prs.md
"Durable record": {skill:"babysit", event:"sweep", pending, bumps, fixes,
red_ci, decision, ts}; /PRlaunch `unit`; /wrapup `cleanup_depth`). Log lines
use the exact formats of launchd/headless-skill.sh, hooks/babysit-fire-log.sh
and launchd/babysit-hourly-gate.sh.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "babysit")
DIGEST_SCRIPT = os.path.join(SKILL_DIR, "babysit_digest.py")
FIRE_HOOK = os.path.join(REPO_ROOT, "hooks", "babysit-fire-log.sh")

sys.path.insert(0, SKILL_DIR)
from babysit_classify import iso  # noqa: E402
import babysit_digest as bd  # noqa: E402

NOW = datetime(2026, 8, 6, 20, 0, 0, tzinfo=timezone.utc)


def ago(minutes):
    return NOW - timedelta(minutes=minutes)


def w(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def ledger_line(**kw):
    d = dict(kw)
    d.setdefault("ts", iso(NOW))
    return json.dumps(d)


def sweep_row(minutes_ago, pending=10, bumps=0, fixes=0, red_ci=0, decision="PROGRESSING"):
    """A row exactly as commands/babysit-prs.md's Durable record writes it."""
    return ledger_line(skill="babysit", event="sweep", pending=pending, bumps=bumps,
                       fixes=fixes, red_ci=red_ci, decision=decision,
                       ts=iso(ago(minutes_ago)))


def _local_line(dt_utc, text):
    """Render a UTC instant the way headless-skill.sh / babysit-fire-log.sh
    write it (`date '+%F %T'`, LOCAL time, `=== ` prefix), so the fixture
    matches production regardless of which timezone the test runs in."""
    return "=== %s [babysit] %s" % (dt_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S"), text)


def _gate_line(dt_utc, text):
    """launchd/babysit-hourly-gate.sh's shape: LOCAL time, NO `=== ` prefix."""
    return "%s [babysit] %s" % (dt_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S"), text)


def _skip(dt_utc, age_s=300):
    return _gate_line(dt_utc, "SKIP: interactive babysit alive (heartbeat age %ds < 4200s)" % age_s)


FIRE = "fire: /babysit-prs no-loop"


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="babysit-digest-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger = os.path.join(self.tmp, "automation-ledger.jsonl")
        self.log = os.path.join(self.tmp, "headless-babysit.log")
        self.lastdigest_dir = os.path.join(self.tmp, "lastdigest")

    def _fold(self, since_dt, now_dt=NOW):
        return bd.fold(since_dt=since_dt, now_dt_=now_dt,
                       ledger_path=self.ledger, log_path=self.log)


# ---------------------------------------------------------------------------
# Empty window
# ---------------------------------------------------------------------------
class EmptyWindowTests(TmpDirCase):
    def test_empty_window_no_crash_no_content(self):
        w(self.ledger, "")
        out = self._fold(since_dt=ago(30))
        self.assertEqual(out["queue"]["sweeps"], 0)
        self.assertEqual(out["shipped"]["opened"], [])
        self.assertEqual(out["gap"]["cadence_holes"], [])
        self.assertEqual(out["stall"], [])
        self.assertEqual(out["alarms"], {"red_ci": None, "cleanup_depth": None})

    def test_window_excludes_rows_outside_it(self):
        w(self.ledger, sweep_row(300) + "\n")
        out = self._fold(since_dt=ago(30))
        self.assertEqual(out["queue"]["sweeps"], 0, "row outside [since, now] must not be folded in")

    def test_corrupt_and_undatable_ledger_lines_are_skipped(self):
        w(self.ledger, "\n".join([
            "not json at all",
            "[1, 2, 3]",
            json.dumps({"event": "sweep", "ts": "yesterday-ish"}),
            sweep_row(10),
        ]) + "\n")
        rows = bd.read_ledger(self.ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self._fold(since_dt=ago(30))["queue"]["sweeps"], 1)


# ---------------------------------------------------------------------------
# Cadence holes (hourly cadence, env-overridable threshold)
# ---------------------------------------------------------------------------
class CadenceHoleTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("BABYSIT_CADENCE_HOLE_MIN", None)

    def test_default_threshold_is_two_hourly_cycles(self):
        self.assertEqual(bd._cadence_hole_min(), 120.0)

    def test_gap_over_threshold_flagged(self):
        # 190 minutes between sweeps: more than two hourly cycles.
        w(self.ledger, "\n".join([sweep_row(220, pending=140), sweep_row(30, pending=133)]) + "\n")
        holes = self._fold(since_dt=ago(250))["gap"]["cadence_holes"]
        self.assertEqual(len(holes), 1)
        self.assertAlmostEqual(holes[0]["minutes"], 190.0, delta=0.1)
        self.assertEqual((holes[0]["start"], holes[0]["end"]), (iso(ago(220)), iso(ago(30))))

    def test_gap_under_threshold_not_flagged(self):
        # A normal hourly cadence with one long sweep: 75 minutes apart.
        w(self.ledger, "\n".join([sweep_row(140), sweep_row(80), sweep_row(5)]) + "\n")
        self.assertEqual(self._fold(since_dt=ago(150))["gap"]["cadence_holes"], [],
                         "a healthy hourly cadence must not read as a hole")

    def test_a_single_long_healthy_sweep_is_not_a_cadence_hole(self):
        """A window spanning hours with ONE sweep row has no gap BETWEEN two
        rows, so nothing is reported -- a long healthy sweep is not a hole."""
        w(self.ledger, sweep_row(5) + "\n")
        self.assertEqual(self._fold(since_dt=ago(300))["gap"]["cadence_holes"], [])

    def test_env_override_changes_the_threshold(self):
        w(self.ledger, "\n".join([sweep_row(90), sweep_row(30)]) + "\n")
        self.assertEqual(self._fold(since_dt=ago(100))["gap"]["cadence_holes"], [])
        os.environ["BABYSIT_CADENCE_HOLE_MIN"] = "40"
        holes = self._fold(since_dt=ago(100))["gap"]["cadence_holes"]
        self.assertEqual(len(holes), 1)
        self.assertAlmostEqual(holes[0]["minutes"], 60.0, delta=0.1)

    def test_non_numeric_override_falls_back_to_default(self):
        os.environ["BABYSIT_CADENCE_HOLE_MIN"] = "two hours"
        self.assertEqual(bd._cadence_hole_min(), 120.0)

    def test_non_finite_or_non_positive_override_falls_back_to_default(self):
        """float() accepts nan/inf/negatives; nan compares False against every
        gap and silently disables hole detection."""
        for raw in ("nan", "inf", "-inf", "-5", "0", "abc"):
            with self.subTest(raw=raw):
                os.environ["BABYSIT_CADENCE_HOLE_MIN"] = raw
                self.assertEqual(bd._cadence_hole_min(), 120.0)
        os.environ["BABYSIT_CADENCE_HOLE_MIN"] = "nan"
        w(self.ledger, "\n".join([sweep_row(220), sweep_row(30)]) + "\n")
        self.assertEqual(len(self._fold(since_dt=ago(250))["gap"]["cadence_holes"]), 1,
                         "nan must not disable cadence-hole detection")


# ---------------------------------------------------------------------------
# Stall detection (fire with no matching exit)
# ---------------------------------------------------------------------------
class StallDetectionTests(TmpDirCase):
    def test_fire_with_no_exit_is_a_stall(self):
        w(self.log, "\n".join([
            _local_line(ago(60), FIRE),
            _local_line(ago(40), "exit=0"),
            _local_line(ago(20), FIRE),
        ]) + "\n")
        out = self._fold(since_dt=ago(70))
        self.assertEqual(len(out["stall"]), 1)
        self.assertIn("no exit", out["stall"][0]["reason"])
        self.assertEqual(out["stall"][0]["prompt"], "/babysit-prs no-loop")

    def test_completed_fire_exit_pairs_are_not_stalls(self):
        w(self.log, "\n".join([
            _local_line(ago(60), FIRE),
            _local_line(ago(40), "exit=0"),
            _local_line(ago(30), FIRE),
            _local_line(ago(10), "exit=0"),
        ]) + "\n")
        self.assertEqual(self._fold(since_dt=ago(70))["stall"], [])

    def test_fire_immediately_followed_by_another_fire_is_a_stall(self):
        """A fire with NO exit before the next fire (the process died without
        writing its exit line) must still be caught."""
        w(self.log, "\n".join([
            _local_line(ago(60), FIRE),
            _local_line(ago(40), FIRE),
            _local_line(ago(20), "exit=0"),
        ]) + "\n")
        out = self._fold(since_dt=ago(70))
        self.assertEqual(len(out["stall"]), 1)
        self.assertEqual(out["stall"][0]["fired_at"], iso(ago(60)))
        self.assertEqual(out["stall"][0]["reason"], "no exit before next fire")

    def test_stray_prefix_before_the_triple_equals_does_not_break_parsing(self):
        """A preceding command's unflushed stdout with no trailing newline runs
        straight into the next echo: 'Execution error=== ... exit=124'."""
        w(self.log, "\n".join([
            _local_line(ago(60), FIRE),
            "Execution error" + _local_line(ago(40), "exit=124")[4:],
        ]) + "\n")
        self.assertEqual(self._fold(since_dt=ago(70))["stall"], [],
                         "the exit= must still be recognized despite the stray prefix")

    def test_negative_exit_code_parses(self):
        w(self.log, _local_line(ago(10), "exit=-1") + "\n")
        events = bd.read_log_events(self.log)
        self.assertEqual([(e["kind"], e["rc"]) for e in events], [("exit", -1)])

    def test_local_time_log_is_interpreted_correctly_against_utc_window(self):
        """Log timestamps are LOCAL; everything else in the fold is UTC. The
        parsed instant must round-trip to the UTC instant it came from."""
        target = ago(45)
        parsed = bd._local_naive_to_utc(target.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        self.assertIsNotNone(parsed)
        self.assertLess(abs((parsed - target).total_seconds()), 1.0)

    def test_missing_log_is_empty_not_a_crash(self):
        self.assertEqual(bd.read_log_events(os.path.join(self.tmp, "nope.log")), [])
        self.assertEqual(bd.read_gate_events(os.path.join(self.tmp, "nope.log")), [])


# ---------------------------------------------------------------------------
# Coverage: absence of data must not render as absence of problems
# ---------------------------------------------------------------------------
class CoverageTests(TmpDirCase):
    def test_no_log_rows_in_window_is_no_data_not_clean(self):
        """The log's newest fire/exit pair PREDATES the window, so `stall` is
        [] -- coverage must mark it unjudgeable rather than clean."""
        w(self.log, "\n".join([_local_line(ago(600), FIRE), _local_line(ago(590), "exit=0")]) + "\n")
        w(self.ledger, "\n".join(sweep_row(m) for m in (50, 30, 10)) + "\n")
        out = self._fold(since_dt=ago(60))
        self.assertEqual(out["stall"], [], "premise: stall is empty")
        self.assertTrue(out["coverage"]["stall_no_data"])
        self.assertEqual(out["coverage"]["fire_exit_rows"], 0)

    def test_sweeps_ran_but_none_were_logged_is_itself_the_anomaly(self):
        """The ledger proves sweeps happened; the log recorded none of them --
        the shape of an interactive `/loop` cadence, which emits no fire/exit."""
        w(self.log, "")
        w(self.ledger, "\n".join(sweep_row(m) for m in (50, 30, 10)) + "\n")
        cov = self._fold(since_dt=ago(60))["coverage"]
        self.assertTrue(cov["unobserved_sweeps"])
        self.assertEqual(cov["sweep_rows"], 3)

    def test_partial_blindness_is_flagged_not_read_as_full_coverage(self):
        """3 ledger sweeps, 1 fire (headless and interactive sweeps mixed):
        `unobserved_sweeps` is all-or-nothing and stays False, so without
        `partially_observed` `stall` would look authoritative over two sweeps
        it cannot see."""
        w(self.log, "\n".join([_local_line(ago(50), FIRE), _local_line(ago(35), "exit=0")]) + "\n")
        w(self.ledger, "\n".join(sweep_row(m) for m in (50, 30, 10)) + "\n")
        out = self._fold(since_dt=ago(60))
        cov = out["coverage"]
        self.assertEqual(out["stall"], [])
        self.assertFalse(cov["stall_no_data"])
        self.assertFalse(cov["unobserved_sweeps"])
        self.assertTrue(cov["partially_observed"])
        self.assertEqual((cov["fire_rows"], cov["sweep_rows"]), (1, 3))

    def test_exits_alone_do_not_count_as_coverage(self):
        """An exit whose fire fell before the window is a boundary artifact;
        counting it would mask a launcher that never fires."""
        w(self.log, _local_line(ago(50), "exit=0") + "\n")
        w(self.ledger, "\n".join(sweep_row(m) for m in (40, 20)) + "\n")
        cov = self._fold(since_dt=ago(60))["coverage"]
        self.assertEqual(cov["fire_rows"], 0)
        self.assertTrue(cov["partially_observed"])

    def test_a_window_with_log_rows_is_judgeable(self):
        """No false alarm: when every sweep has its own fire/exit pair, an
        empty `stall` genuinely means clean."""
        w(self.log, "\n".join([
            _local_line(ago(50), FIRE), _local_line(ago(45), "exit=0"),
            _local_line(ago(30), FIRE), _local_line(ago(25), "exit=0"),
        ]) + "\n")
        w(self.ledger, "\n".join(sweep_row(m) for m in (50, 30)) + "\n")
        out = self._fold(since_dt=ago(60))
        cov = out["coverage"]
        self.assertEqual(out["stall"], [])
        self.assertFalse(cov["stall_no_data"])
        self.assertFalse(cov["unobserved_sweeps"])
        self.assertFalse(cov["cadence_no_data"])
        self.assertFalse(cov["partially_observed"])

    def test_one_sweep_row_cannot_establish_a_cadence_baseline(self):
        w(self.ledger, sweep_row(10) + "\n")
        out = self._fold(since_dt=ago(60))
        self.assertEqual(out["gap"]["cadence_holes"], [])
        self.assertTrue(out["coverage"]["cadence_no_data"])


class FireLogHookVisibilityTests(TmpDirCase):
    """End to end through the REAL hooks/babysit-fire-log.sh: a launched sweep
    that dies mid-run shows as a `stall`. $BABYSIT_LOG pins the hook to this
    test's temp log (it otherwise appends to the live log)."""

    def _emit(self, *args):
        env = dict(os.environ)
        env["BABYSIT_LOG"] = self.log
        p = subprocess.run(["bash", FIRE_HOOK, *args], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)

    def _fold_now(self):
        """The hook stamps real wall-clock time, not the NOW fixture."""
        real_now = datetime.now(timezone.utc)
        return bd.fold(since_dt=real_now - timedelta(minutes=30), now_dt_=real_now,
                       ledger_path=self.ledger, log_path=self.log)

    def test_a_sweep_that_dies_mid_run_shows_as_a_stall(self):
        self._emit("fire", "/babysit-prs no-loop")
        out = self._fold_now()
        self.assertEqual(len(out["stall"]), 1, out["stall"])
        self.assertIn("no exit", out["stall"][0]["reason"])
        self.assertFalse(out["coverage"]["stall_no_data"])

    def test_a_sweep_that_completes_is_not_a_stall(self):
        self._emit("fire", "/babysit-prs no-loop")
        self._emit("exit", "0")
        out = self._fold_now()
        self.assertEqual(out["stall"], [])
        self.assertFalse(out["coverage"]["stall_no_data"])

    def test_without_the_emitted_lines_the_same_death_reads_as_no_data(self):
        w(self.log, "")
        out = self._fold_now()
        self.assertEqual(out["stall"], [])
        self.assertTrue(out["coverage"]["stall_no_data"])


# ---------------------------------------------------------------------------
# Backstop gate: a SKIP with no sweep behind it is a dark slot
# ---------------------------------------------------------------------------
class GateSkipTests(TmpDirCase):
    def test_skip_backed_by_a_recent_fire_is_not_suppression(self):
        w(self.log, "\n".join([
            _local_line(ago(15), FIRE),
            _skip(ago(8), age_s=420),
            _local_line(ago(5), "exit=0"),
        ]) + "\n")
        gate = self._fold(since_dt=ago(30))["gate"]
        self.assertEqual(gate["skipped"], 1)
        self.assertEqual(gate["unbacked"], [])
        self.assertFalse(gate["suppressed_backstop"])
        self.assertFalse(gate["no_data"])

    def test_skip_backed_only_by_a_ledger_sweep_row_is_not_suppression(self):
        """An interactive /babysit-prs session stamps the heartbeat via
        hooks/babysit-heartbeat.py and writes ledger rows but no fire: line."""
        w(self.log, _skip(ago(5)) + "\n")
        w(self.ledger, sweep_row(30) + "\n")
        gate = self._fold(since_dt=ago(30))["gate"]
        self.assertEqual(gate["unbacked"], [])
        self.assertFalse(gate["suppressed_backstop"])

    def test_skip_after_a_failed_launch_is_unbacked(self):
        """A launch that fails (`claude -p` never starts) has already written
        its fire line; the fire alone is no proof a sweep ran."""
        w(self.log, "\n".join([
            _local_line(ago(20), FIRE),
            _local_line(ago(20), "exit=127"),
            _skip(ago(8), age_s=720),
        ]) + "\n")
        gate = self._fold(since_dt=ago(30))["gate"]
        self.assertTrue(gate["suppressed_backstop"])
        self.assertEqual([u["skipped_at"] for u in gate["unbacked"]], [iso(ago(8))])

    def test_skip_while_the_sweep_ran_is_backed_even_if_it_later_failed(self):
        """The nonzero exit lands AFTER the skip: the sweep was running when
        the gate stood down, so that slot was not dark."""
        w(self.log, "\n".join([
            _local_line(ago(55), FIRE),
            _skip(ago(40), age_s=900),
            _local_line(ago(5), "exit=124"),
        ]) + "\n")
        self.assertEqual(self._fold(since_dt=ago(60))["gate"]["unbacked"], [])

    def test_chatty_session_that_stopped_sweeping_is_unbacked_and_loud(self):
        """The last real sweep is hours old, the babysit session keeps taking
        turns (re-stamping the heartbeat), and the gate skips every slot."""
        w(self.log, "\n".join([
            _local_line(ago(300), FIRE),
            _local_line(ago(290), "exit=0"),
            _skip(ago(170), age_s=120),
            _skip(ago(110), age_s=120),
            _skip(ago(50), age_s=120),
        ]) + "\n")
        w(self.ledger, sweep_row(290) + "\n")
        out = self._fold(since_dt=ago(180))
        gate = out["gate"]
        self.assertEqual(gate["skipped"], 3)
        self.assertTrue(gate["suppressed_backstop"])
        self.assertEqual([u["skipped_at"] for u in gate["unbacked"]],
                         [iso(ago(170)), iso(ago(110)), iso(ago(50))])
        self.assertEqual(gate["unbacked"][0]["heartbeat_age_s"], 120)
        self.assertEqual(out["stall"], [], "stall cannot see this -- the gap `gate` closes")

    def test_skip_after_a_forfeited_launch_is_unbacked(self):
        """A single-turn forfeit exits rc=0 with no `sweep` row, and so does a
        relaunch that forfeits too. A later sweep's row (after the next fire)
        is not theirs."""
        w(self.log, "\n".join([
            _local_line(ago(30), FIRE),
            _local_line(ago(29), "exit=0"),
            _local_line(ago(29), FIRE),
            _local_line(ago(28), "exit=0"),
            _skip(ago(20), age_s=480),
            _local_line(ago(10), FIRE),
            _local_line(ago(2), "exit=0"),
        ]) + "\n")
        w(self.ledger, sweep_row(2) + "\n")
        gate = self._fold(since_dt=ago(30))["gate"]
        self.assertTrue(gate["suppressed_backstop"])
        self.assertEqual([u["skipped_at"] for u in gate["unbacked"]], [iso(ago(20))])

    def test_skip_after_a_locked_collision_is_backed_by_the_owners_row(self):
        """A LOCKED launch exits rc=0 with no row of its own, but the lock
        owner's row lands before our next fire: a sweep WAS running."""
        w(self.log, "\n".join([
            _local_line(ago(30), FIRE),
            _local_line(ago(29), "exit=0"),
            _skip(ago(20), age_s=600),
        ]) + "\n")
        w(self.ledger, sweep_row(15) + "\n")
        self.assertEqual(self._fold(since_dt=ago(30))["gate"]["unbacked"], [])

    def test_backing_lookback_reaches_before_the_window(self):
        """The fire that backs a skip can predate `since`; the lookback is the
        gate's own 4200s window, not the render window."""
        w(self.log, "\n".join([
            _local_line(ago(40), FIRE),
            _local_line(ago(25), "exit=0"),
            _skip(ago(5)),
        ]) + "\n")
        w(self.ledger, sweep_row(26) + "\n")
        gate = self._fold(since_dt=ago(10))["gate"]
        self.assertEqual(gate["skipped"], 1)
        self.assertEqual(gate["unbacked"], [])

    def test_backing_window_is_the_gates_stale_threshold(self):
        stale_min = bd.GATE_STALE_S / 60.0
        # fire 1 lands 1 min OUTSIDE skip 1's window; fire 2 (after skip 1, so
        # it cannot back it) lands 1 min INSIDE skip 2's.
        skip1, skip2 = 80, 9
        w(self.log, "\n".join([
            _local_line(ago(skip1 + stale_min + 1), FIRE),
            _skip(ago(skip1)),
            _local_line(ago(skip2 + stale_min - 1), FIRE),
            _skip(ago(skip2)),
        ]) + "\n")
        gate = self._fold(since_dt=ago(100))["gate"]
        self.assertEqual(gate["skipped"], 2)
        self.assertEqual([u["skipped_at"] for u in gate["unbacked"]], [iso(ago(skip1))])

    def test_skips_outside_the_window_are_not_counted(self):
        w(self.log, _skip(ago(90)) + "\n")
        gate = self._fold(since_dt=ago(30))["gate"]
        self.assertEqual(gate["skipped"], 0)
        self.assertFalse(gate["suppressed_backstop"])
        self.assertTrue(gate["no_data"])

    def test_run_decisions_are_counted(self):
        w(self.log, "\n".join([
            _gate_line(ago(25), "heartbeat stale (age 9000s >= 4200s) -> running backup"),
            _gate_line(ago(5), "no heartbeat -> running backup"),
        ]) + "\n")
        gate = self._fold(since_dt=ago(30))["gate"]
        self.assertEqual((gate["decisions"], gate["ran"], gate["skipped"]), (2, 2, 0))
        self.assertFalse(gate["no_data"])

    def test_no_gate_lines_is_no_data_not_healthy(self):
        w(self.log, "")
        gate = self._fold(since_dt=ago(30))["gate"]
        self.assertTrue(gate["no_data"])
        self.assertFalse(gate["suppressed_backstop"])

    def test_skip_lines_do_not_perturb_stall_or_coverage(self):
        base = [_local_line(ago(50), FIRE), _local_line(ago(40), "exit=0"), _local_line(ago(20), FIRE)]
        w(self.log, "\n".join(base) + "\n")
        before = self._fold(since_dt=ago(60))
        w(self.log, "\n".join(base + [_skip(ago(45)), _skip(ago(10))]) + "\n")
        after = self._fold(since_dt=ago(60))
        self.assertEqual(before["stall"], after["stall"])
        self.assertEqual(before["coverage"], after["coverage"])

    def test_parses_the_gate_line_verbatim(self):
        w(self.log, "2026-10-01 19:47:01 [babysit] SKIP: interactive babysit alive "
                    "(heartbeat age 507s < 4200s)\n")
        events = bd.read_gate_events(self.log)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["kind"], events[0]["heartbeat_age_s"]), ("skip", 507))

    def test_other_launcher_lines_are_not_gate_decisions(self):
        """headless-skill.sh's own lock SKIP, and the gate's verdict / cycle
        lines, are not backstop decisions and must not be counted as such."""
        w(self.log, "\n".join([
            _gate_line(ago(20), "SKIP: previous run still holds /tmp/headless-skill-babysit.lock (age 30s)"),
            _gate_line(ago(15), "verdict (attempt=1 rc=0 dur=900s): OK sweep wrote its Step 3 ledger row"),
            _gate_line(ago(14), "cycle done attempt=1 recovery=none rc=0"),
        ]) + "\n")
        self.assertEqual(bd.read_gate_events(self.log), [])
        self.assertEqual(bd.read_log_events(self.log), [])

    def test_stale_threshold_matches_the_gate_script(self):
        """Two constants in two files drift unless pinned."""
        with open(os.path.join(REPO_ROOT, "launchd", "babysit-hourly-gate.sh")) as fh:
            m = re.search(r"(?m)^STALE_S=(\d+)", fh.read())
        self.assertIsNotNone(m)
        self.assertEqual(bd.GATE_STALE_S, int(m.group(1)))


# ---------------------------------------------------------------------------
# Shipped + queue
# ---------------------------------------------------------------------------
class ShippedTests(TmpDirCase):
    def test_unit_rows_are_reported_as_opened(self):
        w(self.ledger, "\n".join([
            ledger_line(skill="prlaunch", event="unit", repo="acme-api", pr=999,
                        gates={"deep_review_findings": 0}, ts=iso(ago(35))),
            ledger_line(skill="prlaunch", event="unit", repo="acme-api", pr=998,
                        ts=iso(ago(300))),      # before the window
        ]) + "\n")
        shipped = self._fold(since_dt=ago(60))["shipped"]
        self.assertEqual(shipped, {"opened": [
            {"repo": "acme-api", "pr": 999, "gates": {"deep_review_findings": 0},
             "ts": iso(ago(35))}]})

    def test_no_units_reports_an_empty_list(self):
        w(self.ledger, sweep_row(5) + "\n")
        self.assertEqual(self._fold(since_dt=ago(30))["shipped"], {"opened": []})

    def test_unit_amendment_rows_do_not_double_count_as_opened(self):
        w(self.ledger, "\n".join([
            ledger_line(skill="prlaunch", event="unit", repo="acme-web", pr=766,
                        gates={"deep_review_findings": 0}, ts=iso(ago(50))),
            ledger_line(skill="prlaunch", event="unit_amendment", repo="acme-web", pr=766,
                        gates={"deep_review_findings": 1}, ts=iso(ago(10))),
        ]) + "\n")
        self.assertEqual(len(self._fold(since_dt=ago(60))["shipped"]["opened"]), 1)


class QueueTests(TmpDirCase):
    def test_trajectory_totals_and_decision_streak(self):
        w(self.ledger, "\n".join([
            sweep_row(170, pending=12, bumps=1, decision="WAITING"),
            sweep_row(110, pending=10, bumps=2, fixes=1),
            sweep_row(50, pending=9, fixes=2),
            sweep_row(5, pending=7, bumps=1),
        ]) + "\n")
        q = self._fold(since_dt=ago(180))["queue"]
        self.assertEqual(q["sweeps"], 4)
        self.assertEqual((q["pending_first"], q["pending_last"], q["pending_delta"]), (12, 7, -5))
        self.assertEqual((q["bumps"], q["fixes"]), (4, 3))
        self.assertEqual((q["decision"], q["decision_streak"]), ("PROGRESSING", 3))

    def test_malformed_bump_and_fix_counts_are_skipped_not_fatal(self):
        """A hand-edited or foreign ledger row can carry a non-numeric count;
        it must be skipped, not crash the whole digest."""
        w(self.ledger, "\n".join([
            sweep_row(50, bumps=2, fixes=1),
            ledger_line(skill="babysit", event="sweep", pending=9, bumps="x",
                        fixes={"a": 1}, ts=iso(ago(40))),
            ledger_line(skill="babysit", event="sweep", pending=9, bumps="1.5",
                        fixes=True, ts=iso(ago(30))),
            ledger_line(skill="babysit", event="sweep", pending=9, bumps=[1],
                        fixes=float("nan"), ts=iso(ago(20))),
            sweep_row(5, bumps=1, fixes=1),
        ]) + "\n")
        q = self._fold(since_dt=ago(60))["queue"]
        self.assertEqual((q["sweeps"], q["bumps"], q["fixes"]), (5, 3, 2))
        env = dict(os.environ, BABYSIT_NOW=iso(NOW), BABYSIT_LEDGER=self.ledger,
                   BABYSIT_LOG=self.log, BABYSIT_LASTDIGEST_DIR=self.lastdigest_dir)
        p = subprocess.run([sys.executable, DIGEST_SCRIPT, "--session-id", "malformed-counts",
                            "--since", iso(ago(60))],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["queue"]["bumps"], 3)

    def test_non_numeric_pending_leaves_delta_none(self):
        w(self.ledger, "\n".join([
            ledger_line(skill="babysit", event="sweep", pending=None, ts=iso(ago(50))),
            sweep_row(5, pending=7),
        ]) + "\n")
        q = self._fold(since_dt=ago(60))["queue"]
        self.assertIsNone(q["pending_delta"])
        self.assertEqual(q["bumps"], 0)


# ---------------------------------------------------------------------------
# Alarms (ledger-derived)
# ---------------------------------------------------------------------------
class AlarmsTests(TmpDirCase):
    def test_red_ci_reports_the_window_max_not_just_the_latest(self):
        """A red-CI spike that self-resolved before the last sweep still
        happened while the reader was away."""
        w(self.ledger, "\n".join([sweep_row(50, red_ci=3), sweep_row(10, red_ci=0)]) + "\n")
        self.assertEqual(self._fold(since_dt=ago(60))["alarms"]["red_ci"], 3)

    def test_no_sweeps_means_red_ci_unknown_not_zero(self):
        w(self.ledger, "")
        self.assertIsNone(self._fold(since_dt=ago(60))["alarms"]["red_ci"])

    def test_cleanup_depth_is_the_latest_row(self):
        w(self.ledger, "\n".join([
            ledger_line(skill="wrapup", event="cleanup_depth", count=4, ts=iso(ago(40))),
            ledger_line(skill="wrapup", event="cleanup_depth", count=1, ts=iso(ago(10))),
        ]) + "\n")
        self.assertEqual(self._fold(since_dt=ago(60))["alarms"]["cleanup_depth"], 1)


# ---------------------------------------------------------------------------
# No network: stdlib-only, no gh/API calls
# ---------------------------------------------------------------------------
class DigestNoNetworkTests(TmpDirCase):
    NETWORK_MODULES = {
        "socket", "ssl", "http", "urllib", "urllib2", "urllib3", "requests",
        "httpx", "aiohttp", "ftplib", "smtplib", "telnetlib", "xmlrpc",
        "asyncio", "subprocess",
    }

    def test_static_source_imports_no_network_capable_module(self):
        import ast
        with open(DIGEST_SCRIPT) as fh:
            tree = ast.parse(fh.read(), filename=DIGEST_SCRIPT)
        found = set()
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            found.update(n for n in names if n.split(".")[0] in self.NETWORK_MODULES)
        self.assertEqual(found, set(), "babysit_digest.py must stay no-network")

    def test_dynamic_fold_never_opens_a_socket(self):
        import socket as socket_mod

        def _blow_up(*a, **kw):
            raise AssertionError("babysit_digest.py must never open a socket")

        w(self.ledger, "\n".join([sweep_row(30), sweep_row(5)]) + "\n")
        w(self.log, "\n".join([_local_line(ago(30), FIRE), _skip(ago(10))]) + "\n")
        with mock.patch.object(socket_mod, "socket", side_effect=_blow_up), \
             mock.patch.object(socket_mod, "create_connection", side_effect=_blow_up):
            out = self._fold(since_dt=ago(60))
        self.assertEqual(out["queue"]["sweeps"], 2)


# ---------------------------------------------------------------------------
# Cold start + since resolution
# ---------------------------------------------------------------------------
class ColdStartTests(TmpDirCase):
    def test_resolve_since_falls_back_to_lookback_when_no_mark(self):
        since_dt, cold_start, source = bd.resolve_since(
            None, self.lastdigest_dir, "session-does-not-exist", NOW)
        self.assertTrue(cold_start)
        self.assertEqual(source, "cold_start_2h_lookback")
        self.assertAlmostEqual((NOW - since_dt).total_seconds() / 3600.0,
                               bd.COLD_START_LOOKBACK_H, delta=0.01)

    def test_explicit_since_wins_over_the_mark_iso_or_epoch(self):
        os.makedirs(self.lastdigest_dir)
        w(os.path.join(self.lastdigest_dir, "s1"), iso(ago(15)) + "\n")
        dt, cold, src = bd.resolve_since("2026-01-01T00:00:00Z", self.lastdigest_dir, "s1", NOW)
        self.assertEqual((iso(dt), cold, src), ("2026-01-01T00:00:00Z", False, "explicit"))
        epoch = str(int(ago(45).timestamp()))
        dt, _, src = bd.resolve_since(epoch, self.lastdigest_dir, "s1", NOW)
        self.assertEqual((dt, src), (ago(45), "explicit"))

    def test_unparseable_since_falls_through_to_the_mark(self):
        os.makedirs(self.lastdigest_dir)
        w(os.path.join(self.lastdigest_dir, "s1"), iso(ago(15)) + "\n")
        _, _, src = bd.resolve_since("last tuesday", self.lastdigest_dir, "s1", NOW)
        self.assertEqual(src, "lastdigest")

    def test_full_cold_start_no_mark_no_ledger_no_log(self):
        """First run, no state anywhere: valid, complete JSON."""
        since_dt, cold_start, _ = bd.resolve_since(None, self.lastdigest_dir, "brand-new", NOW)
        out = bd.fold(since_dt=since_dt, now_dt_=NOW,
                      ledger_path=os.path.join(self.tmp, "missing-ledger.jsonl"),
                      log_path=os.path.join(self.tmp, "missing-log.log"),
                      cold_start=cold_start)
        self.assertTrue(out["cold_start"])
        self.assertEqual(out["queue"]["sweeps"], 0)
        self.assertEqual(out["shipped"]["opened"], [])
        self.assertEqual(out["stall"], [])
        self.assertEqual(out["gap"]["cadence_holes"], [])
        self.assertTrue(out["gate"]["no_data"])
        self.assertEqual(out["render_mode"], "full", "a 2h cold-start lookback exceeds the 60 min mark")
        self.assertEqual(set(out), {"since", "now", "gap_minutes", "cold_start", "render_mode",
                                    "gap", "stall", "coverage", "gate", "shipped", "queue",
                                    "alarms"})
        json.dumps(out)


class ResolveSinceSessionIdGuardTests(TmpDirCase):
    def test_absolute_path_session_id_does_not_escape_lastdigest_dir(self):
        outside = os.path.join(self.tmp, "outside-secret.txt")
        w(outside, iso(ago(5)) + "\n")  # if the guard failed, this would be read
        _, cold_start, source = bd.resolve_since(None, self.lastdigest_dir, outside, NOW)
        self.assertTrue(cold_start)
        self.assertEqual(source, "cold_start_2h_lookback")

    def test_dotdot_session_id_does_not_escape_lastdigest_dir(self):
        # The dir must exist, or "<dir>/../escaped.txt" fails to resolve
        # regardless of the guard and the test proves nothing.
        os.makedirs(self.lastdigest_dir)
        w(os.path.join(self.tmp, "escaped.txt"), iso(ago(5)) + "\n")
        _, cold_start, _ = bd.resolve_since(None, self.lastdigest_dir, "../escaped.txt", NOW)
        self.assertTrue(cold_start)
        for bad in ("..", "."):
            with self.subTest(session_id=bad):
                self.assertTrue(bd.resolve_since(None, self.lastdigest_dir, bad, NOW)[1])

    def test_nul_byte_session_id_does_not_crash(self):
        _, cold_start, source = bd.resolve_since(None, self.lastdigest_dir, "sess-\x00-evil", NOW)
        self.assertTrue(cold_start)
        self.assertEqual(source, "cold_start_2h_lookback")

    def test_normal_session_id_still_reads_its_own_mark(self):
        os.makedirs(self.lastdigest_dir)
        w(os.path.join(self.lastdigest_dir, "normal-session-123"), iso(ago(15)) + "\n")
        since_dt, cold_start, source = bd.resolve_since(
            None, self.lastdigest_dir, "normal-session-123", NOW)
        self.assertFalse(cold_start)
        self.assertEqual(source, "lastdigest")
        self.assertAlmostEqual((NOW - since_dt).total_seconds() / 60.0, 15.0, delta=0.1)

    def test_stamp_refuses_an_escaping_session_id(self):
        bd._stamp_lastdigest(self.lastdigest_dir, "../escaped-stamp", NOW)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escaped-stamp")))
        bd._stamp_lastdigest(self.lastdigest_dir, "", NOW)
        self.assertFalse(os.path.exists(self.lastdigest_dir))


# ---------------------------------------------------------------------------
# render_mode reachability: the real script, two invocations, one session
# ---------------------------------------------------------------------------
class RenderModeReachabilityTests(TmpDirCase):
    """Drives the REAL script as a subprocess across TWO invocations sharing
    one session id, using BABYSIT_NOW to place them on a synthetic clock
    without waiting in real time. The second run must measure the gap from the
    FIRST run's render mark."""

    def setUp(self):
        super().setUp()
        self.session_id = "render-mode-reachability-session"
        w(self.ledger, "")
        w(self.log, "")

    def _run_digest(self, now_dt_, *extra):
        env = dict(os.environ)
        env["BABYSIT_NOW"] = iso(now_dt_)
        env["BABYSIT_LEDGER"] = self.ledger
        env["BABYSIT_LOG"] = self.log
        env["BABYSIT_LASTDIGEST_DIR"] = self.lastdigest_dir
        p = subprocess.run([sys.executable, DIGEST_SCRIPT, "--session-id", self.session_id, *extra],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_real_absence_reaches_full_render_mode(self):
        first = self._run_digest(NOW)
        self.assertEqual(first["since_source"], "cold_start_2h_lookback")
        second = self._run_digest(NOW + timedelta(minutes=86))
        self.assertAlmostEqual(second["gap_minutes"], 86.0, delta=1.0)
        self.assertEqual(second["render_mode"], "full")
        self.assertEqual(second["since_source"], "lastdigest")

    def test_short_gap_renders_terse_with_the_real_gap(self):
        self._run_digest(NOW)
        second = self._run_digest(NOW + timedelta(minutes=20))
        self.assertAlmostEqual(second["gap_minutes"], 20.0, delta=1.0)
        self.assertEqual(second["render_mode"], "terse")

    def test_digest_stamps_its_own_mark_with_the_render_now(self):
        self._run_digest(NOW)
        with open(os.path.join(self.lastdigest_dir, self.session_id)) as fh:
            self.assertEqual(fh.read().strip(), iso(NOW))
        self.assertEqual([f for f in os.listdir(self.lastdigest_dir) if ".tmp." in f], [],
                         "the atomic write must leave no tmp file behind")

    def test_explicit_since_still_stamps_the_mark(self):
        out = self._run_digest(NOW, "--since", iso(ago(300)))
        self.assertEqual(out["since_source"], "explicit")
        self.assertAlmostEqual(out["gap_minutes"], 300.0, delta=0.1)
        second = self._run_digest(NOW + timedelta(minutes=10))
        self.assertEqual(second["since_source"], "lastdigest")
        self.assertAlmostEqual(second["gap_minutes"], 10.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
