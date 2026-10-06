"""Tests for hooks/reconcile-ticket.sh — advance a ticket to Deployed once every
linked PR is merged into its repo's default branch.

Behaviour (read from the script):
  * ticket in an advance-from state (LINEAR_ADVANCE_FROM_STATE_IDS), >=1 linked
    GitHub PR, and EVERY PR merged into its default branch -> issueUpdate
    mutation fired -> prints "✅ ... Deployed".
  * ticket in any other state -- notably an Epics-style container status, which
    is started-type too and where /flushdeployed parks live-but-unfinished
    containers -> no-op.
  * any linked PR still open, or merged into anything but its repo's default
    branch (a stacked PR merged into its parent's branch) -> no-op.
  * API error / gh failure / advance-from list unset -> silent no-op.

curl + gh are shimmed (harness.CURL_SHIM / CURL_CAPTURE_SHIM / GH_API_PR_SHIM).
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import CURL_CAPTURE_SHIM, CURL_SHIM, GH_API_PR_SHIM, HookSandbox

IN_PROGRESS = "11111111-1111-1111-1111-111111111111"
IN_REVIEW = "55555555-5555-5555-5555-555555555555"
EPICS = "66666666-6666-6666-6666-666666666666"
DEPLOYED = "33333333-3333-3333-3333-333333333333"


def issue_node(state_id, state_type="started"):
    """A ticket in `state_id` with one linked GitHub PR."""
    return (
        '{"id":"iid1","identifier":"ENG-123",'
        f'"state":{{"id":"{state_id}","type":"{state_type}"}},'
        '"attachments":{"nodes":[{"url":"https://github.com/o/r/pull/5"}]}}'
    )


ISSUE_NODE = issue_node(IN_PROGRESS)


def pr_json(merged=True, base="main", default="main"):
    return json.dumps({"merged": merged, "base": {"ref": base, "repo": {"default_branch": default}}})


class ReconcileTicketTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox(linear_key="lin_fake")
        self.sbx.add_shim("curl", CURL_SHIM)
        self.sbx.add_shim("gh", GH_API_PR_SHIM)

    def tearDown(self):
        self.sbx.close()

    def _run(self, extra_env):
        env = self.sbx.env(extra_env, shim_path=True)
        proc = subprocess.run(
            ["bash", self.sbx.hook_path("reconcile-ticket.sh"), "ENG-123"],
            capture_output=True, text=True, env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _run_captured(self, extra_env):
        capfile = os.path.join(self.sbx.dir, "curl-capture.log")
        open(capfile, "w").close()
        self.sbx.add_shim("curl", CURL_CAPTURE_SHIM)
        env = dict(extra_env, CURL_CAPTURE_FILE=capfile)
        rc, out, err = self._run(env)
        with open(capfile) as fh:
            return rc, out, fh.read()

    def test_all_prs_merged_advances(self):
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": ISSUE_NODE, "FAKE_PR_STATE": "MERGED"})
        self.assertEqual(rc, 0)
        self.assertIn("Deployed", out)
        self.assertIn("ENG-123", out)

    def test_in_review_advances(self):
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": issue_node(IN_REVIEW), "FAKE_PR_STATE": "MERGED"})
        self.assertEqual(rc, 0)
        self.assertIn("Deployed", out)

    def test_open_pr_is_noop(self):
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": ISSUE_NODE, "FAKE_PR_STATE": "OPEN"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_api_error_is_silent_noop(self):
        rc, out, _ = self._run({"FAKE_MODE": "error", "FAKE_PR_STATE": "MERGED"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    # -- merged means merged into the DEFAULT branch ---------------------------

    def test_pr_merged_into_a_non_main_default_branch_advances(self):
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": ISSUE_NODE,
                                "FAKE_PR_JSON": pr_json(base="master", default="master")})
        self.assertEqual(rc, 0)
        self.assertIn("Deployed", out)

    def test_pr_merged_into_a_stacked_branch_is_noop(self):
        # A stacked PR merged into its parent PR's branch reports MERGED while
        # its code is still only inside the open parent.
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": ISSUE_NODE,
                                "FAKE_PR_JSON": pr_json(base="me/eng-122-parent")})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_gh_failure_is_noop(self):
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": ISSUE_NODE, "FAKE_GH_FAIL": "1"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    # -- advance-from allowlist ------------------------------------------------

    def test_epics_ticket_with_all_prs_merged_is_left_alone(self):
        # Epics-style containers are started-type, and their PRs are all merged
        # by definition -- a started-type rule bounced them back to Deployed.
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": issue_node(EPICS), "FAKE_PR_STATE": "MERGED"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_already_deployed_is_noop(self):
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": issue_node(DEPLOYED), "FAKE_PR_STATE": "MERGED"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_unset_advance_from_list_is_noop(self):
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": ISSUE_NODE, "FAKE_PR_STATE": "MERGED",
                                "LINEAR_ADVANCE_FROM_STATE_IDS": ""})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_advance_from_override_replaces_the_list(self):
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": issue_node("CUSTOM-WIP"), "FAKE_PR_STATE": "MERGED",
                                "LINEAR_ADVANCE_FROM_STATE_IDS": "OTHER-STATE,CUSTOM-WIP"})
        self.assertEqual(rc, 0)
        self.assertIn("Deployed", out)
        rc, out, _ = self._run({"FAKE_ISSUE_NODE": ISSUE_NODE, "FAKE_PR_STATE": "MERGED",
                                "LINEAR_ADVANCE_FROM_STATE_IDS": "CUSTOM-WIP"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    # -- the key travels in curl's config, not argv ----------------------------

    def test_key_reaches_auth_header_but_not_argv(self):
        rc, out, captured = self._run_captured({"FAKE_ISSUE_NODE": ISSUE_NODE, "FAKE_PR_STATE": "MERGED"})
        self.assertIn("Deployed", out)
        self.assertIn('header = "Authorization: lin_fake"', captured)
        argv_lines = [ln for ln in captured.splitlines() if not ln.startswith("header =")]
        self.assertFalse(any("lin_fake" in ln for ln in argv_lines),
                         "the API key must never appear in curl's argv")
        self.assertIn(DEPLOYED, captured, "mutation targets LINEAR_DEPLOYED_STATE_ID")


if __name__ == "__main__":
    unittest.main()
