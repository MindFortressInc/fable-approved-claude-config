"""Step 1.5 detector 12, `workflow-trust`, as a checklist row.

A workflow that can run code chosen by someone other than a repo writer
(`pull_request_target`, `workflow_run`, `issue_comment`, a fork
`pull_request`, a deployment event an app or bot creates) on a persistent
runner is code execution on shared infrastructure. The private config runs
this as a scripted detector; this repo publishes no `cheat-detect.sh`, so the
detector is a row in mechanical-passes.md's checklist plus the by-hand rules a
script has to get right. These tests pin the row, those rules, and that a hit
is a candidate the reviewer must clear by quoting a gate, never a finding on
its own.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT  # noqa: E402

MECHANICAL = os.path.join(REPO_ROOT, "playbooks", "deep-review", "mechanical-passes.md")


def _flat(text):
    return " ".join(text.split())


def _read():
    with open(MECHANICAL, encoding="utf-8") as fh:
        return fh.read()


class WorkflowTrustRowTest(unittest.TestCase):
    def setUp(self):
        text = _read()
        self.row = _flat(re.search(r"^\| `workflow-trust` \|.*$", text, re.M).group(0))

    def test_row_lists_every_untrusted_trigger(self):
        for trigger in ("pull_request_target", "workflow_run", "issue_comment", "pull_request",
                        "pull_request_review", "pull_request_review_comment", "deployment",
                        "deployment_status"):
            self.assertIn("`%s`" % trigger, self.row)

    def test_persistent_runner_is_anything_but_a_hosted_image(self):
        self.assertIn("any `runs-on` that is not a GitHub-hosted", self.row)
        self.assertIn("including custom labels and expressions", self.row)

    def test_row_reads_the_whole_post_image(self):
        self.assertIn("whole post-image workflow file", self.row)
        self.assertIn("**Candidate only**", self.row)


class ByHandRulesTest(unittest.TestCase):
    def setUp(self):
        text = _read()
        start = text.index("**Running `workflow-trust` by hand.**")
        end = text.index("**Turning a hit into a finding:**", start)
        self.rules = _flat(text[start:end])

    def test_only_event_keys_are_triggers(self):
        self.assertIn("Triggers are event keys under `on:` only.", self.rules)
        self.assertIn("A branch filter or an input that happens to be named `deployment` is not a trigger.",
                      self.rules)
        self.assertIn("Quoted keys count", self.rules)

    def test_lookalike_labels_groups_and_expressions_are_persistent(self):
        self.assertIn("`ubuntu-gpu-selfhosted`", self.rules)
        self.assertIn("any runner `group:`", self.rules)
        self.assertIn("block list counts too", self.rules)

    def test_guards_are_job_level_and_per_job(self):
        self.assertIn("only a job-level `if:` counts", self.rules)
        self.assertIn("A guard on one job does not clear another job", self.rules)
        self.assertIn("is not a guard", self.rules)
        self.assertIn("folded multi-line job-level `if:` still counts", self.rules)

    def test_a_guard_must_exclude_untrusted_actors(self):
        self.assertIn("A guard counts only when it excludes untrusted actors.", self.rules)
        self.assertIn("clears only the `pull_request` family", self.rules)
        self.assertIn("only as an equality on the creator's `login` or `id`", self.rules)
        self.assertIn("`creator.type == 'Bot'`", self.rules)
        self.assertIn("A check that selects forks", self.rules)
        self.assertIn("`||`", self.rules)

    def test_only_touched_workflows_are_read(self):
        self.assertIn("Only touched workflow files are read", self.rules)
        self.assertIn("a deleted workflow is skipped", self.rules)


class CandidateOnlyTest(unittest.TestCase):
    def test_a_hit_needs_an_unquotable_gate_to_become_a_finding(self):
        text = _flat(_read())
        self.assertIn("**`workflow-trust` hits are never findings on their own.**", text)
        self.assertIn("cannot quote the actor, creator or fork check that gates the job", text)
        self.assertIn("only when it actually excludes untrusted actors", text)
        self.assertIn("`github.actor != 'dependabot[bot]'`", text)


if __name__ == "__main__":
    unittest.main()
