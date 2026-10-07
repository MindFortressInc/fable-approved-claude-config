"""Tests for skills/scorecard/scorecard.py.

scorecard.py's ledger parsing + aggregation are pure functions, unit-tested here
against a fixture ledger with PR-state lookups stubbed (no live gh). The ledger
row shapes mirror what hooks/ledger-append.sh writes (tested separately in
test_ledger_append.py).
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT


def _load_scorecard():
    path = os.path.join(REPO_ROOT, "skills", "scorecard", "scorecard.py")
    spec = importlib.util.spec_from_file_location("scorecard_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scorecard = _load_scorecard()


class ScorecardPureTest(unittest.TestCase):
    # ts anchors: current window (now-7d, now] and prior (now-14d, now-7d].
    NOW_ISO = "2026-07-06T12:00:00Z"
    ROWS = [
        # --- current window ---
        {"status": "shipped", "ticket": "ENG-1", "repo": "api", "pr": 501, "ts": "2026-07-02T10:00:00Z"},
        {"status": "shipped", "ticket": "ENG-2", "repo": "web", "pr": 502, "ts": "2026-07-03T10:00:00Z"},
        {"status": "resolved", "ticket": "ENG-3", "ts": "2026-07-03T11:00:00Z"},
        {"status": "failed", "ticket": "ENG-4", "ts": "2026-07-04T10:00:00Z"},
        {"skill": "babysit", "event": "sweep", "bumps": 2, "fixes": 1, "red_ci": 1, "decision": "PROGRESSING", "ts": "2026-07-02T12:00:00Z"},
        {"skill": "prlaunch", "event": "unit", "repo": "api", "pr": 501,
         "gates": {"cr_cli": "rate limit", "outcome_eval": "na", "prlaunch_skip": True}, "ts": "2026-07-02T10:05:00Z"},
        {"skill": "wrapup", "event": "cleanup_depth", "count": 3, "ts": "2026-07-04T18:00:00Z"},
        # --- prior window (3 shipped → current's 2 is a >20% regression) ---
        {"status": "shipped", "ticket": "ENG-01", "repo": "web", "pr": 401, "ts": "2026-06-25T10:00:00Z"},
        {"status": "shipped", "ticket": "ENG-02", "repo": "web", "pr": 402, "ts": "2026-06-25T11:00:00Z"},
        {"status": "shipped", "ticket": "ENG-03", "repo": "web", "pr": 403, "ts": "2026-06-26T11:00:00Z"},
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="scorecard-test-")
        self.ledger = os.path.join(self.tmp, "fixture.jsonl")
        with open(self.ledger, "w") as fh:
            for r in self.ROWS:
                fh.write(json.dumps(r) + "\n")
            fh.write("this-is-not-json\n")  # partial/corrupt line must be tolerated
        self.now = scorecard.parse_ts({"ts": self.NOW_ISO})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _windows(self):
        recs = scorecard.parse_ledger(self.ledger)
        cur, prev = scorecard.split_windows(recs, self.now, 7)
        return scorecard.aggregate(cur), scorecard.aggregate(prev)

    def test_parse_tolerates_bad_line(self):
        self.assertEqual(len(scorecard.parse_ledger(self.ledger)), len(self.ROWS))

    def test_absent_ledger_is_empty(self):
        self.assertEqual(scorecard.parse_ledger("/no/such/ledger.jsonl"), [])

    def test_current_window_aggregation(self):
        cur, _ = self._windows()
        self.assertEqual(cur["bulldozer"]["shipped"], 2)
        self.assertEqual(cur["bulldozer"]["resolved"], 1)
        self.assertEqual(cur["bulldozer"]["failed"], 1)
        self.assertEqual(len(cur["bulldozer"]["shipped_prs"]), 2)
        self.assertEqual(cur["babysit"]["fixes"], 1)
        self.assertEqual(cur["babysit"]["red_ci"], 1)
        self.assertEqual(cur["prlaunch"]["units"], 1)
        self.assertEqual(cur["prlaunch"]["cr_cli_skipped"], 1)
        self.assertEqual(cur["prlaunch"]["outcome_na"], 1)
        self.assertEqual(cur["prlaunch"]["prlaunch_skip"], 1)

    def test_prior_window_aggregation(self):
        _, prev = self._windows()
        self.assertEqual(prev["bulldozer"]["shipped"], 3)
        self.assertEqual(prev["bulldozer"]["failed"], 0)

    def test_regression_flagged_on_shipped_drop(self):
        cur, prev = self._windows()
        flags = scorecard.flag_regressions(cur, prev)
        self.assertTrue(any("bulldozer shipped" in f for f in flags), flags)

    def test_no_flag_without_prior_baseline(self):
        # cr_cli skips 0→1 but prev==0 ⇒ no baseline ⇒ never flagged.
        empty = {
            "bulldozer": {"shipped": 0, "failed": 0},
            "babysit": {"fixes": 0, "red_ci": 0},
            "prlaunch": {"cr_cli_skipped": 0, "prlaunch_skip": 0},
        }
        cur = json.loads(json.dumps(empty))
        cur["prlaunch"]["cr_cli_skipped"] = 1
        self.assertEqual(scorecard.flag_regressions(cur, empty), [])

    def test_exactly_20pct_change_is_not_flagged(self):
        prev = scorecard.aggregate([])
        cur = scorecard.aggregate([])
        prev["bulldozer"]["shipped"], cur["bulldozer"]["shipped"] = 5, 4
        self.assertEqual(scorecard.flag_regressions(cur, prev), [])
        self.assertNotIn("⚠", scorecard._delta(5, 4, "up"))

    def test_non_object_gates_skips_unit_without_crashing(self):
        agg = scorecard.aggregate([
            {"skill": "prlaunch", "event": "unit", "gates": "na", "ts": "2026-07-02T10:05:00Z"},
            {"skill": "prlaunch", "event": "unit", "gates": {"prlaunch_skip": True},
             "ts": "2026-07-02T10:06:00Z"},
        ])
        self.assertEqual(agg["prlaunch"]["units"], 1)
        self.assertEqual(agg["prlaunch"]["prlaunch_skip"], 1)

    def test_enrich_pr_states_with_stub_runner(self):
        prs = [
            {"repo": "web", "pr": 1},
            {"repo": "web", "pr": 2},
            {"repo": "api", "pr": 3},
            {"repo": "worker", "pr": 4},
        ]
        states = {("web", 1): "MERGED", ("web", 2): "OPEN",
                  ("api", 3): "CLOSED", ("worker", 4): None}
        counts = scorecard.enrich_pr_states(prs, lambda repo, pr: states[(repo, pr)])
        self.assertEqual(counts["merged"], 1)
        self.assertEqual(counts["open"], 1)
        self.assertEqual(counts["closed_unmerged"], 1)
        self.assertEqual(counts["unknown"], 1)

    def test_bare_repo_without_owner_is_unknown_not_guessed(self):
        # No owner configured: a bare repo name must NOT be resolved against a
        # baked-in org — the runner returns None before ever invoking gh.
        self.assertIsNone(scorecard.default_gh_runner("web", 1, owner=None))

    def test_render_no_gh_smoke(self):
        cur, prev = self._windows()
        md = scorecard.render(cur, prev, None, 5, 2, 7, scorecard.flag_regressions(cur, prev), self.now)
        self.assertIn("Automation scorecard", md)
        self.assertIn("skipped (--no-gh)", md)
        self.assertIn("Verdict", md)

    def test_render_empty_says_no_data(self):
        cur, prev = scorecard.aggregate([]), scorecard.aggregate([])
        md = scorecard.render(cur, prev, None, 0, 0, 7, [], self.now)
        self.assertIn("no data yet", md)


if __name__ == "__main__":
    unittest.main()
