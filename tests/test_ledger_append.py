"""Tests for hooks/ledger-append.sh — the automation-ledger validator.

The hook is a VALIDATOR (fail-loud), not a fail-open safety hook, so these assert
it rejects bad input with a nonzero exit and NEVER partial-writes.
"""
import json
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import HookSandbox, REAL_HOOKS

# ledger-append.sh is not in the harness's symlinked HOOK_FILES set, so tests run
# the real script directly under a sandbox HOME (HookSandbox.env() sets $HOME).
LEDGER_HOOK = os.path.join(REAL_HOOKS, "ledger-append.sh")
ISO8601 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class LedgerAppendHookTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()
        self.ledger = os.path.join(self.sbx.claude, "automation-ledger.jsonl")

    def tearDown(self):
        self.sbx.close()

    def _run(self, *args, **kw):
        # env() starts from os.environ, so the two vars the hook stamps from are
        # cleared unless a test sets them -- otherwise a developer (or a live
        # sweep shell) with BABYSIT_LAUNCH_ID exported would silently add a key
        # to every row these tests assert on.
        env = self.sbx.env()
        env.pop("BABYSIT_LAUNCH_ID", None)
        env.pop("LEDGER_SKILL", None)
        env.update(kw.get("extra_env") or {})
        return subprocess.run(
            ["bash", LEDGER_HOOK, *args],
            capture_output=True, text=True, env=env,
        )

    def _lines(self):
        if not os.path.exists(self.ledger):
            return []
        with open(self.ledger) as fh:
            return [ln for ln in fh.read().splitlines() if ln.strip()]

    def test_valid_object_appended_with_ts(self):
        p = self._run('{"skill":"test","event":"x"}')
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self._lines()
        self.assertEqual(len(lines), 1, "exactly one line appended")
        rec = json.loads(lines[0])  # must be valid JSON
        self.assertEqual(rec["skill"], "test")
        self.assertIn("ts", rec, "ts injected when absent")
        self.assertRegex(rec["ts"], ISO8601)

    def test_second_append_makes_two_lines(self):
        self.assertEqual(self._run('{"a":1}').returncode, 0)
        self.assertEqual(self._run('{"b":2}').returncode, 0)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["a"], 1)
        self.assertEqual(json.loads(lines[1])["b"], 2)

    def test_existing_ts_preserved(self):
        self.assertEqual(self._run('{"ts":"2020-01-01T00:00:00Z","a":1}').returncode, 0)
        rec = json.loads(self._lines()[0])
        self.assertEqual(rec["ts"], "2020-01-01T00:00:00Z", "existing ts left untouched")

    def test_invalid_json_exits_nonzero_and_writes_nothing(self):
        p = self._run("not json")
        self.assertNotEqual(p.returncode, 0, "bad JSON must fail loudly")
        self.assertFalse(os.path.exists(self.ledger), "must not create the ledger on bad input")

    def test_non_object_json_rejected(self):
        for arg in ("[1,2]", "42", '"str"', "null"):
            p = self._run(arg)
            self.assertNotEqual(p.returncode, 0, "arg %r should be rejected" % arg)

    def test_missing_arg_exits_nonzero(self):
        self.assertNotEqual(self._run().returncode, 0)

    def test_extra_args_exit_nonzero(self):
        self.assertNotEqual(self._run('{"a":1}', '{"b":2}').returncode, 0)

    def test_rejected_append_leaves_prior_ledger_unchanged(self):
        self._run('{"a":1}')
        before = self._lines()
        self.assertNotEqual(self._run("not json").returncode, 0)
        self.assertEqual(self._lines(), before, "a rejected append must not alter the file")

    # -- joinable by id: launch_id ------------------------------------------
    # A ledger row and the log of the unattended run that wrote it cover the
    # same period with no shared key unless both stamp $BABYSIT_LAUNCH_ID.

    def test_launch_id_stamped_from_env_when_absent(self):
        p = self._run('{"skill":"babysit","event":"sweep"}',
                      extra_env={"BABYSIT_LAUNCH_ID": "watch-4242-1756600000"})
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = json.loads(self._lines()[0])
        self.assertEqual(rec["launch_id"], "watch-4242-1756600000")

    def test_launch_id_is_sanitised_to_the_shared_class(self):
        p = self._run('{"a":1}', extra_env={"BABYSIT_LAUNCH_ID": "w/1 2;x"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(self._lines()[0])["launch_id"], "w12x")

    def test_explicit_launch_id_in_payload_wins_over_env(self):
        """Same precedence as `ts`: the hook fills a gap, it never overwrites what
        the caller stated."""
        p = self._run('{"a":1,"launch_id":"explicit"}',
                      extra_env={"BABYSIT_LAUNCH_ID": "from-env"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(self._lines()[0])["launch_id"], "explicit")

    def test_no_launch_id_env_leaves_the_key_off_entirely(self):
        """A hand-run invocation has no launcher. The row must not gain a
        `launch_id: null`."""
        self.assertEqual(self._run('{"a":1}').returncode, 0)
        self.assertNotIn("launch_id", json.loads(self._lines()[0]))

    # -- every row says which arm wrote it: skill ---------------------------

    def test_skill_stamped_from_env_when_absent(self):
        """A worker result appended verbatim has no `skill` field; stamping at
        the single choke point fixes every call site at once."""
        p = self._run('{"status":"shipped","ticket":"ENG-1"}',
                      extra_env={"LEDGER_SKILL": "bulldozer"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(self._lines()[0])["skill"], "bulldozer")

    def test_explicit_skill_in_payload_wins_over_env(self):
        p = self._run('{"skill":"prlaunch","event":"unit"}',
                      extra_env={"LEDGER_SKILL": "bulldozer"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(self._lines()[0])["skill"], "prlaunch")

    def test_no_skill_env_leaves_the_key_off_entirely(self):
        self.assertEqual(self._run('{"a":1}').returncode, 0)
        self.assertNotIn("skill", json.loads(self._lines()[0]))

    def test_stamping_does_not_weaken_the_fail_loud_contract(self):
        """The stamps are additive; a bad payload must still be rejected outright
        rather than "fixed up" into a row."""
        p = self._run("not json", extra_env={"LEDGER_SKILL": "bulldozer",
                                             "BABYSIT_LAUNCH_ID": "watch-1"})
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(self.ledger))


if __name__ == "__main__":
    unittest.main()
