"""Tests for hooks/check-careful.sh (+ careful-rm.py) — destructive-command gate.

Also verifies HOME isolation: the deferred-delete path appends to the SANDBOX
cleanup log, and the REAL ~/.claude/cleanup-needed.log must not grow.
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (
    REAL_CLEANUP_LOG, HookSandbox, decision, load_json, run_hook, run_python_hook,
)


class CheckCarefulTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()

    def tearDown(self):
        self.sbx.close()

    def _run(self, cmd, cwd="/tmp"):
        return run_hook(
            self.sbx, "check-careful.sh",
            {"tool_input": {"command": cmd}, "cwd": cwd},
        )

    def test_routine_rm_allows_silently(self):
        rc, out, _ = self._run("rm -rf .venv")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")
        self.assertIsNone(decision(out))

    def test_unrecognized_rm_defers_and_logs(self):
        real_before = os.path.getsize(REAL_CLEANUP_LOG) if os.path.exists(REAL_CLEANUP_LOG) else 0
        self.assertFalse(os.path.exists(self.sbx.cleanup_log))

        rc, out, _ = self._run("rm -rf /Users/x/some-project", cwd="/Users/x")
        self.assertEqual(decision(out), "deny")
        reason = load_json(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Deferred this delete", reason)

        # A line was appended to the SANDBOX cleanup log...
        self.assertTrue(os.path.exists(self.sbx.cleanup_log))
        with open(self.sbx.cleanup_log) as fh:
            lines = [ln for ln in fh if ln.strip()]
        self.assertEqual(len(lines), 1)

        # ...and the REAL cleanup log was untouched (isolation guarantee).
        real_after = os.path.getsize(REAL_CLEANUP_LOG) if os.path.exists(REAL_CLEANUP_LOG) else 0
        self.assertEqual(real_before, real_after, "REAL cleanup-needed.log must not grow")

    def test_deferred_rm_redacts_credentials_in_log(self):
        # A deferred command carrying tokens must not land in the queue verbatim.
        pat = "github_pat_11FAKEFAKE0abcdefghijklmnopqrstuvwxyz0123456789"
        classic = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        cmd = (
            'TOK=%s; curl -H "Authorization: Bearer %s" https://x.invalid | '  # example-url: RFC 2606 .invalid fixture, never dialled
            "sed \"s/${TOK}/x/g\"; rm -rf /Users/x/some-project" % (pat, classic)
        )
        rc, out, _ = self._run(cmd, cwd="/Users/x")
        self.assertEqual(decision(out), "deny")
        with open(self.sbx.cleanup_log) as fh:
            logged = fh.read()
        self.assertNotIn(pat, logged, "fine-grained PAT must be redacted")
        self.assertNotIn(classic, logged, "classic token must be redacted")
        self.assertIn("***REDACTED***", logged)
        # the delete target itself survives redaction so /cleanup can still act
        self.assertIn("/Users/x/some-project", logged)

    def test_deferred_rm_redacts_aws_temporary_credentials(self):
        # ASIA-prefixed IDs are AWS STS temporary access-key IDs — same shape
        # as AKIA (4-letter prefix + 16 alnum), must be redacted the same way.
        asia = "ASIA" + "Q" * 16
        cmd = "TOK=%s; echo $TOK; rm -rf /Users/x/asia-project" % asia
        rc, out, _ = self._run(cmd, cwd="/Users/x")
        self.assertEqual(decision(out), "deny")
        with open(self.sbx.cleanup_log) as fh:
            logged = fh.read()
        self.assertNotIn(asia, logged, "AWS STS temporary credential must be redacted")
        self.assertIn("***REDACTED***", logged)

    def test_deferred_rm_redacts_credentials_in_cwd(self):
        # A credential-shaped CWD (e.g. a scratch dir named after a token)
        # must not land in the queue verbatim either — same sink, same risk.
        pat = "github_pat_11FAKEFAKE0abcdefghijklmnopqrstuvwxyz0123456789"
        cwd = "/Users/x/%s" % pat
        rc, out, _ = self._run("rm -rf /Users/x/some-project", cwd=cwd)
        self.assertEqual(decision(out), "deny")
        with open(self.sbx.cleanup_log) as fh:
            logged = fh.read()
        self.assertNotIn(pat, logged, "credential-shaped cwd must be redacted")
        self.assertIn("***REDACTED***", logged)

    def test_deferred_rm_marks_redacted_path_field_at_write_time(self):
        # The queued JSON entry must self-flag when redaction altered cmd/cwd
        # (not just reason) — cleanup-sweep.py's run_entry() relies on this
        # marker to refuse auto-resolving a path that no longer matches
        # reality. This must be set by check-careful.sh at WRITE time, not
        # just by cleanup-sweep.py's read-time self-heal for legacy entries.
        pat = "github_pat_11FAKEFAKE0abcdefghijklmnopqrstuvwxyz0123456789"
        cwd = "/Users/x/%s" % pat
        rc, out, _ = self._run("rm -rf /Users/x/some-project", cwd=cwd)
        self.assertEqual(decision(out), "deny")
        with open(self.sbx.cleanup_log) as fh:
            entry = json.loads(fh.readline())
        self.assertTrue(
            entry.get("_redacted_path_field"),
            "entry must self-flag that cwd was credential-redacted",
        )

        # End-to-end: cleanup-sweep.py must refuse to auto-run this entry.
        rc, out, _ = run_python_hook(self.sbx, "cleanup-sweep.py", ["--run", "0"])
        self.assertEqual(rc, 0)
        self.assertIn("redact", out.lower())
        rc, out, _ = run_python_hook(self.sbx, "cleanup-sweep.py", ["--count"])
        self.assertEqual(out.strip(), "1", "entry must remain queued, not auto-run")

    def test_routine_rm_with_credential_cwd_never_reaches_log(self):
        # A routine/regenerable target never gets queued at all (allowed
        # silently) — confirm the redaction plumbing doesn't fire spuriously
        # and, more importantly, that nothing is written when there's nothing
        # to defer.
        pat = "github_pat_11FAKEFAKE0abcdefghijklmnopqrstuvwxyz0123456789"
        rc, out, _ = self._run("rm -rf .venv", cwd="/Users/x/%s" % pat)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")
        self.assertFalse(os.path.exists(self.sbx.cleanup_log))

    def test_force_push_asks(self):
        rc, out, _ = self._run("git push --force origin main")
        self.assertEqual(decision(out), "ask")
        self.assertIn("force-push", load_json(out)["hookSpecificOutput"]["permissionDecisionReason"].lower())

    def test_force_with_lease_does_not_warn(self):
        rc, out, _ = self._run("git push --force-with-lease origin main")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")

    def test_loop_mode_future_epoch_auto_proceeds(self):
        self.sbx.arm_loop_mode(int(time.time()) + 3600)
        rc, out, _ = self._run("git push --force origin main")
        self.assertEqual(decision(out), "allow")
        self.assertIn("loop-mode", load_json(out)["hookSpecificOutput"]["permissionDecisionReason"])
        # armed file survives (still in the future)
        self.assertTrue(os.path.exists(self.sbx.loop_mode))

    def test_loop_mode_past_epoch_self_disarms_and_asks(self):
        self.sbx.arm_loop_mode(100)  # long past
        rc, out, _ = self._run("git push --force origin main")
        self.assertEqual(decision(out), "ask")
        # expired loop-mode file self-disarmed (deleted) so it can't poison later
        self.assertFalse(os.path.exists(self.sbx.loop_mode))


if __name__ == "__main__":
    unittest.main()
