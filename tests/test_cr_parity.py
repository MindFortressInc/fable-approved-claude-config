#!/usr/bin/env python3
"""Regression tests for cr-parity.py (the parity scoreboard).

PRlaunch runs deep-review first and requires it clean before the CodeRabbit CLI,
so a CR CLI finding recorded on a branch AFTER its first deep-review is a miss
by construction. cr-parity.py turns the automation ledger's `gate_findings`
rows into that miss rate, per ISO week and per repo x class.

Each test pins a property the ad-hoc measurement had to get right:
  test_a_worktree_suffix_folds_into_repo     .dev-NNNN / -devNNNN are one repo
  test_b_cr_before_deep_review_is_not_escape  t0 is the first deep_review
  test_c_repeated_cr_runs_dedupe             same (file, title[:80]) counts once
  test_d_smoke_rows_never_count              both smoke shapes are excluded
  test_e_uncovered_branch_is_unknown_not_clean   the coverage discipline
  test_f_weekly_rates_use_covered_denominator    the number that reproduces W41
  test_g_severity_folds_to_major_plus        MAJOR / HIGH / major/high
  test_h_classifier_is_deterministic_with_other  no LLM; `other` is explicit
  test_i_every_class_reported_even_at_zero   no class silently absent
  test_j_cli_reports_coverage_other_and_bar  stated every run
  test_k_absent_ledger_is_no_data            never a traceback; --json stays JSON
  test_l_cr_only_ledger_still_reports_cr_only  CR-only rows are not "no data"
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
SCRIPT = os.path.join(REPO_ROOT, "hooks", "cr-parity.py")


def _load():
    spec = importlib.util.spec_from_file_location("cr_parity_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cp = _load()


def row(gate, ts, findings, repo="api-service", branch="me/dev-1-x"):
    return {"event": "gate_findings", "skill": "prlaunch", "repo": repo,
            "branch": branch, "gate": gate, "sha": "abc",
            "findings_count": len(findings), "findings": findings, "ts": ts}


def f(title, sev="major", file="api/x.py", disp="fixed"):
    return {"severity": sev, "file": file, "title": title, "disposition": disp}


class ParityTests(unittest.TestCase):
    def test_a_worktree_suffix_folds_into_repo(self):
        self.assertEqual(cp.norm_repo("config-repo.dev-7452"), "config-repo")
        self.assertEqual(cp.norm_repo("api-service-dev9278"), "api-service")
        self.assertEqual(cp.norm_repo("web-app"), "web-app")
        recs = [row("deep_review", "2026-10-06T00:00:00Z", [], repo="config-repo.dev-1"),
                row("cr_cli", "2026-10-06T01:00:00Z", [f("Guard None")], repo="config-repo")]
        b = cp.branches(recs)
        self.assertEqual(list(b), [("config-repo", "me/dev-1-x")])
        self.assertTrue(b[("config-repo", "me/dev-1-x")]["covered"])

    def test_b_cr_before_deep_review_is_not_escape(self):
        recs = [row("cr_cli", "2026-10-06T00:00:00Z", [f("early finding")]),
                row("deep_review", "2026-10-06T01:00:00Z", [f("dr", sev="HIGH")]),
                row("deep_review", "2026-10-06T05:00:00Z", []),
                row("cr_cli", "2026-10-06T02:00:00Z", [f("late finding")])]
        b = cp.branches(recs)[("api-service", "me/dev-1-x")]
        self.assertEqual([e["title"] for e in b["esc"].values()], ["late finding"])
        self.assertEqual(b["t0"].isoformat(), "2026-10-06T01:00:00+00:00")

    def test_c_repeated_cr_runs_dedupe(self):
        recs = [row("deep_review", "2026-10-06T00:00:00Z", []),
                row("cr_cli", "2026-10-06T01:00:00Z", [f("X" * 90 + "a")]),
                row("cr_cli", "2026-10-06T02:00:00Z", [f("X" * 90 + "b"),
                                                       f("X" * 90, file="api/y.py")])]
        b = cp.branches(recs)[("api-service", "me/dev-1-x")]
        self.assertEqual(len(b["esc"]), 2)

    def test_d_smoke_rows_never_count(self):
        recs = [row("deep_review", "2026-10-06T00:00:00Z",
                    [f("smoke", disp="waived: smoke test"), f("real", sev="HIGH")]),
                row("cr_cli", "2026-10-06T01:00:00Z",
                    [f("Smoke"), f("anything", disp="waived: smoke test")])]
        b = cp.branches(recs)[("api-service", "me/dev-1-x")]
        self.assertEqual(len(b["dr"]), 1)
        self.assertEqual(len(b["esc"]), 0)
        self.assertTrue(b["covered"], "a CR run with only smoke rows still ran")

    def test_e_uncovered_branch_is_unknown_not_clean(self):
        recs = [row("deep_review", "2026-10-06T00:00:00Z", [], branch="a"),
                row("cr_cli", "2026-10-06T01:00:00Z", [], branch="a"),        # clean CR
                row("deep_review", "2026-10-06T00:00:00Z", [], branch="b"),   # never CR'd
                row("cr_cli", "2026-10-05T00:00:00Z", [f("x")], branch="c"),  # CR only
                row("deep_review", "2026-10-07T00:00:00Z", [], branch="d"),
                row("cr_cli", "2026-10-06T00:00:00Z", [f("x")], branch="d")]  # CR before t0
        b = cp.branches(recs)
        self.assertTrue(b[("api-service", "a")]["covered"])
        self.assertFalse(b[("api-service", "b")]["covered"])
        self.assertFalse(b[("api-service", "d")]["covered"])
        self.assertIsNone(b[("api-service", "c")]["t0"])
        wk = cp.weekly(b)
        self.assertEqual(len(wk), 1)
        w = wk[0]
        self.assertEqual((w["deep_reviewed"], w["covered"]), (3, 1))
        self.assertEqual(w["with_escape_pct"], 0.0)
        self.assertEqual(cp.coverage(b), {"deep_reviewed": 3, "covered": 1,
                                          "coverage_pct": 1 / 3, "cr_only": 1})

    def test_f_weekly_rates_use_covered_denominator(self):
        recs = []
        for i, esc in enumerate([[f("a", "critical"), f("b", "minor", disp="waived: fp")],
                                 [f("c", "MAJOR", disp="ticket EX-9")],
                                 [], None]):
            br = f"b{i}"
            recs.append(row("deep_review", "2026-10-06T00:00:00Z",
                            [f(f"dr{i}", sev="HIGH")], branch=br))
            if esc is not None:
                recs.append(row("cr_cli", "2026-10-06T01:00:00Z", esc, branch=br))
        recs.append(row("deep_review", "2026-09-29T00:00:00Z", [], branch="old"))
        wk = {w["week"]: w for w in cp.weekly(cp.branches(recs))}
        self.assertEqual(sorted(wk), ["2026-W40", "2026-W41"])
        w = wk["2026-W41"]
        self.assertEqual(w["deep_reviewed"], 4)
        self.assertEqual(w["covered"], 3)
        self.assertAlmostEqual(w["with_escape_pct"], 2 / 3)
        self.assertAlmostEqual(w["escapes_per_branch"], 1.0)
        self.assertAlmostEqual(w["major_per_branch"], 2 / 3)
        self.assertAlmostEqual(w["dr_per_branch"], 1.0)
        self.assertEqual(w["disposition"], {"fixed": 1, "waived": 1, "ticket": 1, "none": 0})
        self.assertEqual(wk["2026-W40"]["covered"], 0)
        self.assertIsNone(wk["2026-W40"]["with_escape_pct"])

    def test_g_severity_folds_to_major_plus(self):
        for s in ("critical", "CRITICAL", "major", "MAJOR", "HIGH", "major/high"):
            self.assertTrue(cp.major_plus(s), s)
        for s in ("minor", "MEDIUM", "LOW", "trivial", "minor/major", None, "batch"):
            self.assertFalse(cp.major_plus(s), s)
        self.assertEqual(cp.norm_sev("major/minor (recurring)"), "major")

    def test_h_classifier_is_deterministic_with_other(self):
        cases = [
            ("api/routers/auth.py", "/auth/validate-app still selects inactive membership",
             "authz_trust"),
            ("api/log.py", "Raw email address written to logs", "secret_pii_consent"),
            ("alembic/versions/0123_x.py", "downgrade drops the column", "migration_backfill"),
            (".github/workflows/ci.yml", "pin the action to a sha", "ci_runner"),
            ("tests/test_x.py", "test never asserts the result", "test_fixtures"),
            ("api/jobs.py", "Race: two workers claim the same job", "concurrency_stale"),
            ("api/svc.py", "blocking call inside async handler stalls the event loop",
             "async_session"),
            ("api/svc.py", "exception swallowed, lease never released", "error_path"),
            ("api/q.py", "unbounded limit parameter allows huge query", "input_bounds"),
            ("src/components/Panel.tsx", "button stays disabled after reset", "frontend_ui"),
            ("api/x.py", "zzz qqq", "other"),
        ]
        for file, title, want in cases:
            self.assertEqual(cp.classify(file, title), want, (file, title))
            self.assertEqual(cp.classify(file, title), cp.classify(file, title))
        self.assertEqual(cp.CLASSES[-1], "other")
        self.assertEqual(len(set(cp.CLASSES)), len(cp.CLASSES))

    def test_i_every_class_reported_even_at_zero(self):
        recs = [row("deep_review", "2026-10-06T00:00:00Z",
                    [f("Race: two workers claim the same job", sev="HIGH")]),
                row("cr_cli", "2026-10-06T01:00:00Z",
                    [f("Race on retry", sev="major"), f("zzz", sev="major"),
                     f("nit wording", sev="minor")])]
        rows = cp.class_table(cp.branches(recs))
        got = {(r["repo"], r["class"]): r for r in rows}
        for repo in ("ALL", "api-service"):
            for c in cp.CLASSES:
                self.assertIn((repo, c), got)
        race = got[("ALL", "concurrency_stale")]
        self.assertEqual((race["caught"], race["misses"], race["n"]), (1, 1, 2))
        self.assertAlmostEqual(race["miss_rate"], 0.5)
        self.assertEqual(got[("ALL", "other")]["misses"], 1)
        zero = got[("ALL", "migration_backfill")]
        self.assertEqual((zero["misses"], zero["n"]), (0, 0))
        self.assertIsNone(zero["miss_rate"])

    def _ledger(self, recs):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for r in recs:
            tmp.write(json.dumps(r) + "\n")
        tmp.write("not-json\n")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_j_cli_reports_coverage_other_and_bar(self):
        path = self._ledger([
            row("deep_review", "2026-10-06T00:00:00Z", [f("dr", sev="HIGH")]),
            row("cr_cli", "2026-10-06T01:00:00Z", [f("zzz"), f("Race on retry")]),
            row("deep_review", "2026-10-06T00:00:00Z", [], branch="uncovered"),
            {"event": "unit", "skill": "prlaunch", "ts": "2026-10-06T00:00:00Z"},
        ])
        p = subprocess.run([sys.executable, SCRIPT, "--ledger", path,
                            "--since", "2026-10-01"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout
        self.assertIn("2026-W41", out)
        self.assertIn("coverage: 1 of 2 deep-reviewed branches (50%)", out)
        self.assertIn("other (unclassified): 1 of 2 major+ misses (50%)", out)
        self.assertIn("proposed CR-deprecation bar", out)
        self.assertIn("informational", out)
        self.assertIn("parity statement", out)

        p = subprocess.run([sys.executable, SCRIPT, "--ledger", path, "--json",
                            "--since", "2026-10-01"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        self.assertEqual(doc["coverage"]["covered"], 1)
        self.assertEqual(doc["weekly"][0]["week"], "2026-W41")
        self.assertEqual(len([r for r in doc["classes"] if r["repo"] == "ALL"]),
                         len(cp.CLASSES))

    def test_k_absent_ledger_is_no_data(self):
        p = subprocess.run([sys.executable, SCRIPT, "--ledger", "/no/such/ledger.jsonl"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("no data", p.stdout)

        p = subprocess.run([sys.executable, SCRIPT, "--ledger", "/no/such/ledger.jsonl",
                            "--json"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        self.assertEqual((doc["weekly"], doc["coverage"]["deep_reviewed"]), ([], 0))

    def test_l_cr_only_ledger_still_reports_cr_only(self):
        path = self._ledger([row("cr_cli", "2026-10-06T01:00:00Z", [f("zzz")])])
        p = subprocess.run([sys.executable, SCRIPT, "--ledger", path],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("1 CR-reviewed branch(es) carry no deep-review stamp", p.stdout)
        p = subprocess.run([sys.executable, SCRIPT, "--ledger", path, "--json"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["coverage"]["cr_only"], 1)


if __name__ == "__main__":
    unittest.main()
