"""On a charge, reservation, hold or save path, the overlap check is a run.

deep-review 5c used to carry a one-line checkbox ("Parallel requests to test
blocking, race conditions, idempotency"). Stale-completion escapes went
through it: an older save's success clearing a newer save's failure, and a
charge that was not reserved atomically. These tests pin the replacement. A
diff on one of those paths must fire overlapping requests, or force the older
request to complete last, and record the command and the final state it
observed. The PRlaunch outcome eval records that run, and such a path cannot
take the outcome eval's N/A exit.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT  # noqa: E402

VALIDATION = os.path.join(REPO_ROOT, "playbooks", "deep-review", "validation.md")
PRLAUNCH = os.path.join(REPO_ROOT, "commands", "PRlaunch.md")


def _flat(text):
    return " ".join(text.split())


def _block(path, heading, end_marker):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    start = text.index(heading + "\n")
    end = text.index(end_marker, start + len(heading))
    return _flat(text[start:end])


class DeepReview5cOverlapRunTest(unittest.TestCase):
    def setUp(self):
        self.block = _block(VALIDATION, "### 5c. Runtime validation", "\n### 5d.")

    def test_names_the_paths_that_require_a_run(self):
        self.assertIn("charge, reservation, hold or save path", self.block)

    def test_requires_overlap_or_older_completes_last(self):
        self.assertIn("two overlapping requests", self.block)
        self.assertIn("older-completes-last", self.block)

    def test_requires_recorded_command_and_final_state(self):
        self.assertIn("Record the command and the final state it observed", self.block)

    def test_no_server_path_is_a_real_run_not_a_mock(self):
        self.assertIn("a script that drives the real code path twice at once counts", self.block)
        self.assertIn("a mocked unit test does not", self.block)

    def test_a_ticked_box_does_not_count(self):
        self.assertIn("A ticked box does not count", self.block)
        self.assertNotIn("Parallel requests to test blocking, race conditions, idempotency", self.block)


class PRlaunchOutcomeEvalOverlapRunTest(unittest.TestCase):
    def setUp(self):
        self.block = _block(PRLAUNCH, "### When it applies", "\n### The method")

    def test_overlap_path_is_never_outcome_eval_na(self):
        self.assertIn("charge, reservation, hold or save path", self.block)
        self.assertIn("never takes the N/A exit", self.block)

    def test_overlap_scenario_is_recorded_in_the_eval(self):
        self.assertIn("two overlapping requests", self.block)
        self.assertIn("older-completes-last", self.block)
        self.assertIn("deep-review 5c", self.block)
        self.assertIn("a mocked unit test does not", self.block)

    def test_pass_criterion_stays_in_the_users_seat(self):
        self.assertIn("written from the user's seat", self.block)


if __name__ == "__main__":
    unittest.main()
