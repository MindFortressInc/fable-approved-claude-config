#!/usr/bin/env python3
"""Tests for the PRlaunch `driver` ledger field and the /execute scorecard row.

Gate and unit rows are stamped `skill:"prlaunch"` whoever drove them, so
without a driver the ledger cannot say how many units /execute shipped. `driver` says which skill drove the unit. It comes from
$LEDGER_DRIVER, which callers set inline on each ledger-writing call: a Bash tool
call is a new shell, so an `export` does not reach the next one.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT, HookSandbox, make_git_repo, run_hook_args

SCORECARD = os.path.join(REPO_ROOT, "skills", "scorecard", "scorecard.py")
spec = importlib.util.spec_from_file_location("scorecard_driver_under_test", SCORECARD)
scorecard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scorecard)

PRLAUNCH_MD = os.path.join(REPO_ROOT, "commands", "PRlaunch.md")
BULLDOZER_MD = os.path.join(REPO_ROOT, "commands", "bulldozer.md")
EXECUTE_MD = os.path.join(REPO_ROOT, "skills", "execute", "SKILL.md")
ORCHESTRATE_MD = os.path.join(REPO_ROOT, "skills", "orchestrate", "SKILL.md")
NOW = "2026-11-02T00:00:00Z"


def read(path):
    with open(path) as fh:
        return fh.read()


def unit(ts, driver=None, **extra):
    row = {"skill": "prlaunch", "event": "unit", "repo": "r", "pr": 1, "ts": ts,
           "gates": {"cr_cli": "clean", "outcome_eval": "na", "prlaunch_skip": False}}
    if driver is not None:
        row["driver"] = driver
    row.update(extra)
    return row


class LedgerAppendDriverTests(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()
        self.ledger = os.path.join(self.sbx.claude, "automation-ledger.jsonl")

    def tearDown(self):
        self.sbx.close()

    def append(self, payload, **env):
        return run_hook_args(self.sbx, "ledger-append.sh", [payload], extra_env=env)

    def row(self):
        with open(self.ledger) as fh:
            return json.loads(fh.read().splitlines()[-1])

    def test_every_driver_value_reaches_a_prlaunch_row(self):
        for value in ("execute", "orchestrate", "bulldozer", "manual"):
            rc, _, err = self.append('{"skill":"prlaunch","event":"unit"}', LEDGER_DRIVER=value)
            self.assertEqual(rc, 0, err)
            self.assertEqual(self.row()["driver"], value)

    def test_no_env_leaves_the_key_off(self):
        """Unset is not `manual`: a worker that dropped the prefix must not read as a human."""
        self.append('{"skill":"prlaunch","event":"unit"}')
        self.assertNotIn("driver", self.row())

    def test_empty_env_leaves_the_key_off(self):
        self.append('{"skill":"prlaunch","event":"unit"}', LEDGER_DRIVER="")
        self.assertNotIn("driver", self.row())

    def test_only_prlaunch_rows_are_stamped(self):
        """A `claude -p` worker can run with the variable in its env; a babysit row it writes is not a PRlaunch unit."""
        self.append('{"skill":"babysit","event":"sweep"}', LEDGER_DRIVER="execute")
        self.assertNotIn("driver", self.row())
        self.append('{"status":"shipped"}', LEDGER_DRIVER="bulldozer", LEDGER_SKILL="bulldozer")
        self.assertNotIn("driver", self.row())

    def test_a_row_stamped_prlaunch_by_env_counts(self):
        self.append('{"event":"unit"}', LEDGER_DRIVER="execute", LEDGER_SKILL="prlaunch")
        self.assertEqual(self.row()["driver"], "execute")

    def test_explicit_payload_driver_wins(self):
        self.append('{"skill":"prlaunch","event":"unit","driver":"orchestrate"}', LEDGER_DRIVER="execute")
        self.assertEqual(self.row()["driver"], "orchestrate")

    def test_unknown_value_warns_and_still_writes_the_row(self):
        """A typo must not lose the unit, and must not add a fifth bucket."""
        rc, _, err = self.append('{"skill":"prlaunch","event":"unit"}', LEDGER_DRIVER="exceute")
        self.assertEqual(rc, 0, err)
        self.assertIn("exceute", err)
        self.assertNotIn("driver", self.row())


class PrlaunchRowsCarryDriverTests(unittest.TestCase):
    """The real writers: prlaunch-gate.sh, and the phase-6 block out of PRlaunch.md."""

    def setUp(self):
        self.sbx = HookSandbox()
        self.repo = os.path.join(self.sbx.dir, "api-service")
        make_git_repo(self.repo, "main", self.sbx.env())
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "-b", "me/dev-42-x"],
                       check=True, env=self.sbx.env(), capture_output=True)
        self.ledger = os.path.join(self.sbx.claude, "automation-ledger.jsonl")

    def tearDown(self):
        self.sbx.close()

    def rows(self, event):
        with open(self.ledger) as fh:
            return [r for r in map(json.loads, fh) if r.get("event") == event]

    def gate(self, *args, **env):
        rc, out, err = run_hook_args(self.sbx, "prlaunch-gate.sh", list(args), extra_env=env, cwd=self.repo)
        self.assertEqual(rc, 0, out + err)

    def test_gate_findings_row_inherits_the_driver(self):
        self.gate("record", "deep_review", "--findings", "[]", LEDGER_DRIVER="execute")
        self.assertEqual(self.rows("gate_findings")[0]["driver"], "execute")

    def test_gate_findings_row_outside_execute_claims_nothing(self):
        self.gate("record", "deep_review", "--findings", "[]")
        self.assertNotIn("driver", self.rows("gate_findings")[0])

    def run_phase6(self, driver):
        """Run the ACTUAL phase-6 block, placeholders filled the way a worker fills them."""
        blocks = [b for b in re.findall(r"```bash\n(.*?)```", read(PRLAUNCH_MD), re.S)
                  if 'ledger-append.sh "$U"' in b]
        self.assertEqual(len(blocks), 1)
        script = blocks[0]
        for placeholder, value in (("<repo>", "api-service"), ("<PR#>", "7"),
                                   ("<deep-review CRITICAL+HIGH count>", "0"),
                                   ("<N> scenarios", "3 scenarios"), ("<driver>", driver)):
            self.assertIn(placeholder, script)
            script = script.replace(placeholder, value)
        self.gate("record", "cr_cli", "--skipped", "rate limit")
        self.gate("record", "outcome_eval", "--na", "no user-facing surface")
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=self.sbx.env(), cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("REJECTED", proc.stderr)
        return self.rows("unit")[0]

    def test_phase6_block_tags_the_unit_row(self):
        row = self.run_phase6("execute")
        self.assertEqual(row["driver"], "execute")
        self.assertEqual(row["pr"], 7)
        self.assertEqual(row["gates"]["cr_cli"], "rate limit")

    def test_phase6_block_with_no_driver_writes_an_untagged_row(self):
        self.assertNotIn("driver", self.run_phase6(""))


class ScorecardDriverTests(unittest.TestCase):
    def setUp(self):
        self.now = scorecard.parse_ts({"ts": NOW})
        self.rows = [
            unit("2026-10-30T00:00:00Z", "execute"),
            unit("2026-10-30T01:00:00Z", "execute"),
            unit("2026-10-30T02:00:00Z", "orchestrate"),
            unit("2026-10-30T03:00:00Z", "bulldozer"),
            unit("2026-10-30T04:00:00Z", "manual"),
            unit("2026-10-30T05:00:00Z"),                    # historical shape
            unit("2026-10-30T06:00:00Z", "typo"),            # never a new bucket
            unit("2026-10-30T07:00:00Z", ["execute"]),       # never a traceback
            unit("2026-10-24T00:00:00Z", "execute"),         # prior window
            unit("2026-10-24T01:00:00Z"),
            {"event": "gate_findings", "skill": "prlaunch", "driver": "execute", "gate": "deep_review",
             "branch": "b", "findings": [], "ts": "2026-10-30T00:00:00Z"},  # not a unit
        ]

    def aggregates(self, rows):
        cur, prev = scorecard.split_windows(rows, self.now, 7)
        return scorecard.aggregate(cur), scorecard.aggregate(prev)

    def test_units_bucket_by_driver_and_old_rows_read_as_untagged(self):
        cur, prev = self.aggregates(self.rows)
        self.assertEqual(cur["drivers"], {"execute": 2, "orchestrate": 1, "bulldozer": 1,
                                          "manual": 1, "untagged": 3})
        self.assertEqual(sum(cur["drivers"].values()), cur["prlaunch"]["units"])
        self.assertEqual(prev["drivers"]["execute"], 1)
        self.assertEqual(prev["drivers"]["untagged"], 1)

    def test_render_prints_the_execute_row_and_states_the_untagged_count(self):
        cur, prev = self.aggregates(self.rows)
        md = scorecard.render(cur, prev, None, 0, 0, 7, [], self.now)
        self.assertIn("| /execute | units shipped (driver=execute) | 2 | 1 | +100% |", md)
        self.assertIn("execute 2 · orchestrate 1 · bulldozer 1 · manual 1 · untagged 3", md)

    def test_all_untagged_history_is_not_reported_as_zero_without_a_caveat(self):
        cur, prev = self.aggregates([unit("2026-10-30T00:00:00Z"), unit("2026-10-24T00:00:00Z")])
        md = scorecard.render(cur, prev, None, 0, 0, 7, [], self.now)
        self.assertIn("| /execute | units shipped (driver=execute) | 0 | 0 |", md)
        self.assertIn("untagged 1", md)
        self.assertIn("floor", md)

    def test_empty_ledger_says_no_data(self):
        empty = scorecard.aggregate([])
        md = scorecard.render(empty, empty, None, 0, 0, 7, [], self.now)
        self.assertIn("| /execute | _no data yet — ledger empty_ |", md)

    def test_the_stale_model_note_is_gone(self):
        cur, prev = self.aggregates([unit("2026-10-24T00:00:00Z"), unit("2026-10-24T01:00:00Z")])
        cur["bulldozer"]["shipped"] = 1
        flags = ["bulldozer shipped down 50% (2→1)"]
        md = scorecard.render(cur, prev, None, 0, 0, 7, flags, self.now)
        self.assertIn("regressed", md)
        self.assertNotIn("driver model", md)
        self.assertNotIn("prime suspect", md)
        self.assertNotIn("driver model", read(os.path.join(REPO_ROOT, "skills", "scorecard", "SKILL.md")))

    def test_cli_runs_against_a_mixed_ledger_with_corrupt_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for r in self.rows:
                fh.write(json.dumps(r) + "\n")
            fh.write("{half a row\n")
        self.addCleanup(os.unlink, fh.name)
        p = subprocess.run([sys.executable, SCORECARD, "--no-gh", "--ledger", fh.name, "--now", NOW],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("| /execute | units shipped (driver=execute) | 2 | 1 |", p.stdout)
        self.assertIn("stale open Ops tickets (no update in 14d): _skipped (--no-gh)_", p.stdout)


class StaleOpsCountTests(unittest.TestCase):
    def setUp(self):
        self.now = scorecard.parse_ts({"ts": NOW})

    def test_counts_across_pages_with_the_right_filter(self):
        pages = [{"issues": {"nodes": [{"id": "a"}, {"id": "b"}],
                             "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}},
                 {"issues": {"nodes": [{"id": "c"}],
                             "pageInfo": {"hasNextPage": False, "endCursor": None}}}]
        calls = []

        def gql(query, variables):
            calls.append((query, variables))
            return pages[len(calls) - 1]

        self.assertEqual(scorecard.stale_ops_count(self.now, gql=gql), 3)
        query, first = calls[0]
        self.assertIn('startsWith: "Ops:"', query)
        # Linear's Duplicate state has its own type, so completed/canceled alone counts it as open.
        self.assertIn('nin: ["completed", "canceled", "duplicate"]', query)
        self.assertEqual(first["cutoff"], "2026-10-19T00:00:00Z")
        self.assertIsNone(first["after"])
        self.assertEqual(calls[1][1]["after"], "c1")

    def test_a_failed_query_is_unknown_not_zero(self):
        def boom(query, variables):
            raise RuntimeError("linear down")

        def bye(query, variables):
            raise SystemExit(1)  # done_without_evidence.gql die()s on any Linear error

        self.assertIsNone(scorecard.stale_ops_count(self.now, gql=boom))
        self.assertIsNone(scorecard.stale_ops_count(self.now, gql=bye))

    def test_a_query_that_never_ends_is_unknown(self):
        def forever(query, variables):
            return {"issues": {"nodes": [{"id": "a"}],
                               "pageInfo": {"hasNextPage": True, "endCursor": "c"}}}

        self.assertIsNone(scorecard.stale_ops_count(self.now, gql=forever))

    def test_render_prints_the_count_and_an_unknown(self):
        empty = scorecard.aggregate([])
        md = scorecard.render(empty, empty, None, 0, 0, 7, [], self.now, stale_ops=12)
        self.assertIn("stale open Ops tickets (no update in 14d): 12", md)
        md = scorecard.render(empty, empty, None, 0, 0, 7, [], self.now, stale_ops=None)
        self.assertIn("stale open Ops tickets (no update in 14d): n/a", md)


class SkillTextTests(unittest.TestCase):
    def test_every_ledger_writing_prlaunch_call_carries_the_driver(self):
        text = read(PRLAUNCH_MD)
        for call in ("prlaunch-gate.sh record deep_review --findings",
                     "prlaunch-gate.sh record cr_cli --findings",
                     'ledger-append.sh "$U"'):
            self.assertIn("LEDGER_DRIVER='<driver>' ~/.claude/hooks/" + call, text)

    def test_each_orchestrator_names_its_own_driver(self):
        self.assertIn("LEDGER_DRIVER=execute", read(EXECUTE_MD))
        self.assertIn("LEDGER_DRIVER=orchestrate", read(ORCHESTRATE_MD))
        self.assertIn("LEDGER_DRIVER=bulldozer", read(BULLDOZER_MD))

    def test_execute_drops_todowrite_and_keeps_the_load_bearing_collision_rules(self):
        text = read(EXECUTE_MD)
        self.assertNotIn("TodoWrite", text)
        for rule in ("~/.claude/hooks/collision-check.sh <TICKET>",
                     "never remove a worktree you did not create",
                     "git worktree remove --force",
                     "git branch -D",
                     "the answer is UNKNOWN",
                     "immediately before creating the worktree",
                     "`· self`"):
            self.assertIn(rule, text)

    def test_orchestrate_gotcha_pack_carries_the_test_bound(self):
        """execute's batch section points at this bullet, so the pack must keep it."""
        pack = read(ORCHESTRATE_MD).split("Worker gotcha pack", 1)[1].split("```")[1]
        self.assertIn("MAX `-n 2`", pack)
        self.assertIn("NEVER `-n auto`", pack)
        self.assertIn("never the full suite", pack)
        self.assertIn("hooks/test-admission.py", pack)
        self.assertIn("its test bound", read(EXECUTE_MD))


if __name__ == "__main__":
    unittest.main()
