#!/usr/bin/env python3
"""Tests for scorecard.py's "Deep-review escapes" section.

The weekly /scorecard grades the fleet window-over-window. This section adds
the deep-review -> CodeRabbit escape rate from hooks/cr-parity.py, so a
regression in what deep-review lets through is flagged by the same
flag_regressions mechanism as every other metric, with CR coverage stated next
to it (a rate over a shrinking denominator looks like improvement).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SCORECARD = os.path.join(REPO_ROOT, "skills", "scorecard", "scorecard.py")

spec = importlib.util.spec_from_file_location("scorecard_escapes_under_test", SCORECARD)
scorecard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scorecard)

NOW = "2026-10-15T00:00:00Z"


def gf(gate, ts, branch, findings):
    return {"event": "gate_findings", "skill": "prlaunch", "repo": "api-service",
            "branch": branch, "gate": gate, "findings": findings, "ts": ts}


def esc(title, sev="major"):
    return {"severity": sev, "file": "api/x.py", "title": title, "disposition": "fixed"}


# Current window (10-08, 10-15]: 4 covered branches, 3 with a major+ escape.
# Prior window (10-01, 10-08]: 4 covered branches, 1 with a major+ escape.
ROWS = []
for i in range(4):
    ROWS.append(gf("deep_review", "2026-10-10T00:00:00Z", f"cur{i}", []))
    ROWS.append(gf("cr_cli", "2026-10-10T01:00:00Z", f"cur{i}",
                   [esc("Race: two workers claim the same job")] if i < 3 else []))
    ROWS.append(gf("deep_review", "2026-10-03T00:00:00Z", f"prev{i}", []))
    ROWS.append(gf("cr_cli", "2026-10-03T01:00:00Z", f"prev{i}",
                   [esc("zzz")] if i < 1 else []))
# A prior-window branch whose CR run lands in the current window still belongs
# to the prior window (branches are bucketed by their first deep-review), but
# that late run is not observed: each window is read as of its own end, so both
# windows get the same observation lag and the straddle counts as uncovered.
ROWS.append(gf("deep_review", "2026-10-07T00:00:00Z", "straddle", []))
ROWS.append(gf("cr_cli", "2026-10-09T00:00:00Z", "straddle", []))
# Deep-reviewed in the current window, never CR-reviewed: unknown, not clean.
ROWS.append(gf("deep_review", "2026-10-11T00:00:00Z", "uncovered", []))


class ScorecardEscapesTests(unittest.TestCase):
    def setUp(self):
        self.now = scorecard.parse_ts({"ts": NOW})

    def test_windows_bucket_by_first_deep_review(self):
        cur, prev = scorecard.escape_windows(ROWS, self.now, 7)
        self.assertEqual((cur["deep_reviewed"], cur["covered"]), (5, 4))
        self.assertEqual(cur["coverage_pct"], 80)
        self.assertEqual(cur["escape_branch_pct"], 75)
        self.assertEqual(cur["major_per_100"], 75)
        self.assertEqual(cur["classes"], {"concurrency_stale": 3})
        self.assertEqual(cur["other"], 0)
        self.assertEqual((prev["deep_reviewed"], prev["covered"]), (5, 4))
        self.assertEqual(prev["coverage_pct"], 80)
        self.assertEqual(prev["escape_branch_pct"], 25)
        self.assertEqual(prev["other"], 1)

    def test_windows_share_the_same_observation_lag(self):
        # Identical activity one window apart must score identically: the prior
        # window may not count CR runs that landed after its own end.
        rows = [gf("deep_review", "2026-10-07T00:00:00Z", "p", []),
                gf("cr_cli", "2026-10-09T00:00:00Z", "p", [esc("late")]),
                gf("deep_review", "2026-10-14T00:00:00Z", "c", []),
                gf("cr_cli", "2026-10-16T00:00:00Z", "c", [esc("late")])]
        cur, prev = scorecard.escape_windows(rows, self.now, 7)
        self.assertEqual(cur, prev)
        self.assertEqual((prev["deep_reviewed"], prev["covered"]), (1, 0))

    def test_escape_rise_is_flagged_by_existing_mechanism(self):
        cur, prev = scorecard.aggregate([]), scorecard.aggregate([])
        cur["escapes"], prev["escapes"] = scorecard.escape_windows(ROWS, self.now, 7)
        flags = scorecard.flag_regressions(cur, prev)
        self.assertTrue(any("branches w/ escape" in x for x in flags), flags)
        self.assertTrue(any("major+ escapes per 100" in x for x in flags), flags)
        # Prior baseline is the comparison, so the improving direction is silent.
        flags = scorecard.flag_regressions(prev, cur)
        self.assertFalse(any("escape" in x for x in flags), flags)

    def test_aggregate_without_escape_data_is_never_flagged(self):
        cur, prev = scorecard.aggregate([]), scorecard.aggregate([])
        self.assertEqual(cur["escapes"]["covered"], 0)
        self.assertEqual(scorecard.flag_regressions(cur, prev), [])
        md = scorecard.render(cur, prev, None, 0, 0, 7, [], self.now)
        self.assertIn("Deep-review escapes", md)

    def test_render_states_coverage_and_other(self):
        cur, prev = scorecard.aggregate([]), scorecard.aggregate([])
        cur["escapes"], prev["escapes"] = scorecard.escape_windows(ROWS, self.now, 7)
        md = scorecard.render(cur, prev, None, 0, 0, 7,
                              scorecard.flag_regressions(cur, prev), self.now)
        self.assertIn("| Deep-review escapes | CR coverage of deep-reviewed branches % | 80 | 80 |", md)
        self.assertIn("| Deep-review escapes | branches w/ escape % | 75 | 25 |", md)
        self.assertIn("4 of 5 deep-reviewed branches CR-covered; 1 unknown", md)
        self.assertIn("concurrency_stale 3", md)
        self.assertIn("other 0", md)
        self.assertIn("regressed", md)

    def test_unloadable_cr_parity_degrades_to_empty_escapes(self):
        orig = scorecard.CR_PARITY
        scorecard.CR_PARITY = os.path.join(HERE, "no-such-cr-parity.py")
        self.addCleanup(setattr, scorecard, "CR_PARITY", orig)
        cur, prev = scorecard.escape_windows(ROWS, self.now, 7)
        self.assertEqual(cur, prev)
        self.assertIn("no-such-cr-parity.py", cur["error"])
        self.assertEqual(dict(cur, error=None), scorecard.empty_escapes())
        agg_cur, agg_prev = scorecard.aggregate([]), scorecard.aggregate([])
        agg_cur["escapes"], agg_prev["escapes"] = cur, prev
        self.assertEqual(scorecard.flag_regressions(agg_cur, agg_prev), [])
        md = scorecard.render(agg_cur, agg_prev, None, 0, 0, 7, [], self.now)
        self.assertIn("| Deep-review escapes | _skipped — cr-parity.py failed: FileNotFoundError:", md)
        self.assertNotIn("| Deep-review escapes | _no data yet — ledger empty_", md)

    def test_malformed_rows_are_skipped_not_fatal(self):
        bad_file = dict(esc("unhashable file"), file=["api", "x.py"])
        rows = ROWS + [gf("deep_review", "2026-10-12T00:00:00Z", ["not", "hashable"], []),
                       gf("cr_cli", "2026-10-12T00:00:00Z", {"not": "a str"}, []),
                       gf("deep_review", "2026-10-12T00:00:00Z", "badfile", [bad_file]),
                       gf("cr_cli", "2026-10-12T01:00:00Z", "badfile", [bad_file])]
        cur, prev = scorecard.escape_windows(rows, self.now, 7)
        self.assertEqual(prev, scorecard.escape_windows(ROWS, self.now, 7)[1])
        self.assertEqual((cur["deep_reviewed"], cur["covered"]), (6, 5))
        self.assertEqual(cur["escape_branch_pct"], 80)
        self.assertEqual(cur["classes"], {"concurrency_stale": 3})
        self.assertEqual(cur["other"], 1)

    def test_main_prints_section_from_ledger(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for r in ROWS:
                fh.write(json.dumps(r) + "\n")
        self.addCleanup(os.unlink, fh.name)
        p = subprocess.run([sys.executable, SCORECARD, "--no-gh", "--ledger", fh.name,
                            "--now", NOW], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("branches w/ escape % | 75 | 25 |", p.stdout)


if __name__ == "__main__":
    unittest.main()
