"""Tests for hooks/babysit-progress.sh's attempt-cap store and its reader.

`attempts` / `add-attempt` / `clear-attempts` / `reset-pr` maintain ONE
"automation has tried enough, a human must look" bucket that both the `fix`
and the `rebase` planners in skills/babysit/babysit_classify.py read
(`load_fix_attempts`). Each sweep is a fresh process, so without a durable
counter a fix that fails validation every time looks like a first attempt
forever.

The bash tests run the REAL hook against a throwaway store via its own
$BABYSIT_PROGRESS override, never the live store.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
HOOK = os.path.join(REPO_ROOT, "hooks", "babysit-progress.sh")
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "babysit")

sys.path.insert(0, SKILL_DIR)
from babysit_classify import load_fix_attempts  # noqa: E402

KEY = "acme-api#3200"
HEAD_A = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"


class _StoreCase(unittest.TestCase):
    def setUp(self):
        fd, self.store = tempfile.mkstemp(prefix="babysit-progress-test-", suffix=".json")
        os.close(fd)
        os.remove(self.store)  # let the hook's `ensure` create it fresh
        self.env = dict(os.environ)
        self.env["BABYSIT_PROGRESS"] = self.store

    def tearDown(self):
        if os.path.exists(self.store):
            os.remove(self.store)

    def _run(self, *args):
        return subprocess.run(["bash", HOOK, *args],
                              capture_output=True, text=True, env=self.env)

    def _store(self):
        with open(self.store) as fh:
            return json.load(fh)

    def _seed(self):
        """Both counters exhausted, plus a waiver and a known-FP entry on the
        SAME key -- the hard boundary a reset must not cross."""
        store = {
            "cli_reviewed": {KEY: {"head": "abc1234", "at": "2026-01-01T00:00:00Z",
                                   "rounds": 4}},
            "known_fp": {KEY: {"reason": "ack-reply false positive",
                               "since": "2026-01-01T00:00:00Z"}},
            "waived_findings": {KEY: {"api/x.py:RULE": {
                "reason": "premise-false, verified against code",
                "since": "2026-01-01T00:00:00Z", "by": "reviewer",
                "authorized_by_human": True}}},
            "fix_attempts": {KEY: {"count": 3, "last": "author-call/unvalidatable",
                                   "at": "2026-01-01T00:00:00Z"}},
            "merges": [],
        }
        with open(self.store, "w") as fh:
            json.dump(store, fh)
        return store


class AttemptCounterTest(_StoreCase):
    def test_attempts_is_zero_on_a_fresh_store(self):
        p = self._run("attempts", KEY)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "0")

    def test_add_attempt_increments_and_records_reason_head_and_guard_exit(self):
        self.assertEqual(self._run("add-attempt", KEY, "suite red", HEAD_A).returncode, 0)
        p = self._run("add-attempt", KEY, "merge guard refused", HEAD_A, "2")
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = self._store()["fix_attempts"][KEY]
        self.assertEqual(rec["count"], 2)
        self.assertEqual(rec["last"], "merge guard refused")
        self.assertEqual(rec["head"], HEAD_A)
        self.assertEqual(rec["guard_exit"], "2")
        self.assertEqual(self._run("attempts", KEY).stdout.strip(), "2")

    def test_add_attempt_without_a_head_records_an_empty_head(self):
        """Omitting the head is allowed and is the fail-safe direction: the
        classifier can then never read the record as "the head moved"."""
        self.assertEqual(self._run("add-attempt", KEY, "why").returncode, 0)
        rec = self._store()["fix_attempts"][KEY]
        self.assertEqual(rec["head"], "")
        self.assertEqual(rec["guard_exit"], "")

    def test_add_attempt_does_not_disturb_other_store_namespaces(self):
        before = self._seed()
        self.assertEqual(self._run("add-attempt", KEY, "why", HEAD_A).returncode, 0)
        after = self._store()
        self.assertEqual(after["fix_attempts"][KEY]["count"], 4)
        for ns in ("cli_reviewed", "known_fp", "waived_findings", "merges"):
            self.assertEqual(after[ns], before[ns], ns)

    def test_the_written_record_round_trips_through_the_classifier_reader(self):
        self._run("add-attempt", KEY, "suite red", HEAD_A, "2")
        got = load_fix_attempts(self.store)[KEY]
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["head"], HEAD_A)
        self.assertEqual(got["guard_exit"], "2")


class ResetPrTest(_StoreCase):
    """A human ruling (`reset-pr`, alias `reset-cli-rounds`) lifts BOTH
    exhaustion counters in ONE commit; the agent-invoked `clear-attempts`
    lifts only the attempt bucket."""

    def setUp(self):
        super().setUp()
        # Count jq invocations: `save()` is exactly one jq call per commit, so
        # this proves commit COUNT deterministically, not by timing.
        real_jq = shutil.which("jq")
        self.assertIsNotNone(real_jq, "jq must be on PATH for this test to mean anything")
        self.jq_dir = tempfile.mkdtemp(prefix="babysit-jq-shadow-")
        self.jq_log = os.path.join(self.jq_dir, "calls.log")
        open(self.jq_log, "w").close()
        wrapper = os.path.join(self.jq_dir, "jq")
        with open(wrapper, "w") as fh:
            fh.write("#!/usr/bin/env bash\n"
                     "echo call >> '%s'\n"
                     "exec '%s' \"$@\"\n" % (self.jq_log, real_jq))
        os.chmod(wrapper, 0o755)
        self.env["PATH"] = self.jq_dir + os.pathsep + self.env.get("PATH", "")

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.jq_dir, ignore_errors=True)

    def _jq_calls(self):
        with open(self.jq_log) as fh:
            return sum(1 for _ in fh)

    def test_reset_pr_clears_both_counters_and_leaves_findings_untouched(self):
        before = self._seed()
        p = self._run("reset-pr", KEY)
        self.assertEqual(p.returncode, 0, p.stderr)
        store = self._store()
        self.assertEqual(store["cli_reviewed"][KEY]["rounds"], 0)
        self.assertNotIn(KEY, store.get("fix_attempts", {}))
        self.assertEqual(store["waived_findings"][KEY], before["waived_findings"][KEY],
                         "a waiver is a human judgment about a finding, not exhaustion")
        self.assertEqual(store["known_fp"][KEY], before["known_fp"][KEY])

    def test_reset_pr_is_a_single_jq_transaction(self):
        self._seed()
        self.assertEqual(self._run("reset-pr", KEY).returncode, 0)
        self.assertEqual(self._jq_calls(), 1,
                         "two save() calls would leave an observable half-reset commit")

    def test_reset_cli_rounds_alias_is_the_same_single_transaction(self):
        self._seed()
        self.assertEqual(self._run("reset-cli-rounds", KEY).returncode, 0)
        self.assertEqual(self._jq_calls(), 1)
        store = self._store()
        self.assertEqual(store["cli_reviewed"][KEY]["rounds"], 0)
        self.assertNotIn(KEY, store.get("fix_attempts", {}))

    def test_clear_attempts_never_touches_review_rounds(self):
        """`clear-attempts` runs on every successful automated push; if it
        zeroed the round counter that cap would become unreachable."""
        self._seed()
        self.assertEqual(self._run("clear-attempts", KEY).returncode, 0)
        self.assertEqual(self._jq_calls(), 1)
        store = self._store()
        self.assertNotIn(KEY, store.get("fix_attempts", {}))
        self.assertEqual(store["cli_reviewed"][KEY]["rounds"], 4)

    def test_reset_pr_on_a_legacy_store_with_no_fix_attempts_key(self):
        legacy = {"cli_reviewed": {KEY: {"head": "a", "rounds": 4}},
                  "known_fp": {}, "waived_findings": {}, "merges": []}
        with open(self.store, "w") as fh:
            json.dump(legacy, fh)
        p = self._run("reset-pr", KEY)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self._store()["cli_reviewed"][KEY]["rounds"], 0)

    def test_reset_pr_when_the_pr_was_never_cli_reviewed(self):
        legacy = {"cli_reviewed": {}, "known_fp": {}, "waived_findings": {},
                  "merges": [], "fix_attempts": {KEY: {"count": 2, "last": "x", "at": "t"}}}
        with open(self.store, "w") as fh:
            json.dump(legacy, fh)
        p = self._run("reset-pr", KEY)
        self.assertEqual(p.returncode, 0, p.stderr)
        store = self._store()
        self.assertEqual(store["cli_reviewed"][KEY]["rounds"], 0)
        self.assertNotIn(KEY, store.get("fix_attempts", {}))

    def test_reset_and_clear_are_idempotent(self):
        self._seed()
        for cmd in ("reset-pr", "reset-pr", "clear-attempts", "clear-attempts"):
            p = self._run(cmd, KEY)
            self.assertEqual(p.returncode, 0, "%s: %s" % (cmd, p.stderr))
        self.assertNotIn(KEY, self._store().get("fix_attempts", {}))


class LoadFixAttemptsTest(unittest.TestCase):
    """The reader is a type-checked whitelist: one malformed record must never
    crash the whole sweep."""

    def _write(self, obj):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
        self.addCleanup(os.remove, path)
        return path

    def test_missing_or_corrupt_store_reads_empty(self):
        self.assertEqual(load_fix_attempts("/nonexistent/babysit-progress.json"), {})
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write("{not json")
        self.addCleanup(os.remove, path)
        self.assertEqual(load_fix_attempts(path), {})

    def test_non_dict_store_and_non_dict_section_read_empty(self):
        self.assertEqual(load_fix_attempts(self._write([1, 2])), {})
        self.assertEqual(load_fix_attempts(self._write({"fix_attempts": []})), {})

    def test_malformed_records_are_skipped_or_normalised(self):
        path = self._write({"fix_attempts": {
            "a#1": "not-a-dict",
            "a#2": {"count": "many"},
            "a#3": {"count": 2, "head": 12345, "guard_exit": None},
            "a#4": {"count": 1, "head": HEAD_A, "guard_exit": "2", "last": "why"},
        }})
        got = load_fix_attempts(path)
        self.assertNotIn("a#1", got)
        self.assertNotIn("a#2", got)
        self.assertEqual(got["a#3"]["head"], "", "a non-string head must not reach the sha check")
        self.assertEqual(got["a#3"]["guard_exit"], "")
        self.assertEqual(got["a#4"], {"count": 1, "last": "why", "at": "",
                                      "head": HEAD_A, "guard_exit": "2"})


if __name__ == "__main__":
    unittest.main()
