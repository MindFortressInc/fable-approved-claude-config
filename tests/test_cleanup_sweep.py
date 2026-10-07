"""Tests for hooks/cleanup-sweep.py — the deferred-delete queue helper."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import HookSandbox, run_python_hook


class CleanupSweepTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()
        self._real_home = os.environ.get("HOME")

    def tearDown(self):
        if self._real_home is not None:
            os.environ["HOME"] = self._real_home
        self.sbx.close()

    def _load_sweep_module(self):
        """Import cleanup-sweep.py in-process, bound to the sandbox HOME.

        In-process so a test can inject a real concurrent append at the exact
        point a sweep is mid-delete — the window the append-vs-rewrite race lives in —
        instead of racing two processes against the clock.
        """
        os.environ["HOME"] = self.sbx.home
        path = self.sbx.hook_path("cleanup-sweep.py")
        spec = importlib.util.spec_from_file_location("cleanup_sweep_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _run_main(self, mod, *args):
        """Call the module's main() with argv, swallowing its report output."""
        argv, stdout = sys.argv, sys.stdout
        sys.argv = ["cleanup-sweep.py", *args]
        sys.stdout = io.StringIO()
        try:
            mod.main()
        finally:
            sys.argv, sys.stdout = argv, stdout

    def _queue(self):
        try:
            with open(self.sbx.cleanup_log) as fh:
                return [json.loads(ln) for ln in fh if ln.strip()]
        except FileNotFoundError:
            return []

    def _seed(self, entry):
        with open(self.sbx.cleanup_log, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _sweep(self, *args):
        return run_python_hook(self.sbx, "cleanup-sweep.py", args)

    def test_count_empty_is_zero(self):
        rc, out, _ = self._sweep("--count")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "0")

    def test_json_parses_seeded_entry(self):
        target = os.path.join(self.sbx.dir, "victim")
        os.makedirs(target)
        self._seed({"ts": 1, "cwd": "/tmp", "cmd": "rm -rf %s" % target, "reason": "unrecognized"})
        rc, out, _ = self._sweep("--json")
        self.assertEqual(rc, 0)
        rows = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["i"], 0)
        self.assertIn("victim", rows[0]["cmd"])

    def test_run_deletes_target_and_drops_entry(self):
        target = os.path.join(self.sbx.dir, "victim")
        os.makedirs(target)
        self._seed({"ts": 1, "cwd": "/tmp", "cmd": "rm -rf %s" % target, "reason": "unrecognized"})

        rc, out, _ = self._sweep("--run", "0")
        self.assertEqual(rc, 0)
        self.assertIn("deleted", out)
        self.assertFalse(os.path.exists(target), "target should be gone")

        rc, out, _ = self._sweep("--count")
        self.assertEqual(out.strip(), "0", "entry should be dropped after successful run")

    # -- appends and sweep rewrites share one lock ------------------

    def test_append_queues_an_entry(self):
        """check-careful.sh's append path (`--append`, JSON on stdin)."""
        entry = {"ts": 7, "cwd": "/tmp", "cmd": "rm -rf /tmp/x", "reason": "unrecognized"}
        rc, _, err = run_python_hook(
            self.sbx, "cleanup-sweep.py", ("--append",), stdin_text=json.dumps(entry)
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._queue(), [entry])

    def test_concurrent_append_during_run_is_not_lost(self):
        """A delete queued mid-sweep must survive the sweep's rewrite.

        `--run` takes an in-memory snapshot, deletes (slowly), then rewrites the
        queue. Before the shared lock the rewrite wrote that stale snapshot back, so an
        entry appended by check-careful.sh in another session during the delete
        was silently erased.
        """
        target = os.path.join(self.sbx.dir, "victim")
        os.makedirs(target)
        queued = {"ts": 1, "cwd": "/tmp", "cmd": "rm -rf %s" % target, "reason": "unrecognized"}
        self._seed(queued)

        mod = self._load_sweep_module()
        raced = {"ts": 2, "cwd": "/tmp", "cmd": "rm -rf /tmp/raced", "reason": "unrecognized"}
        real_delete = mod.delete_targets

        def delete_then_race(targets):
            out = real_delete(targets)
            # A SECOND PROCESS queues a deferred delete while this sweep is
            # mid-run, through the same --append the hook uses. The timeout is
            # load-bearing: if a future change ever held the queue lock across
            # the deletions, this would deadlock, and the test says so.
            proc = subprocess.run(
                ["python3", self.sbx.hook_path("cleanup-sweep.py"), "--append"],
                input=json.dumps(raced), text=True, capture_output=True,
                env=self.sbx.env(), timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return out

        mod.delete_targets = delete_then_race
        self._run_main(mod, "--run", "0")

        self.assertFalse(os.path.exists(target), "the queued target should be deleted")
        self.assertEqual(
            self._queue(), [raced],
            "the concurrently appended entry must survive the sweep's rewrite",
        )

    def test_remove_preserves_a_concurrently_appended_entry(self):
        """`--remove` re-reads under the lock, so it drops only its own index."""
        first = {"ts": 1, "cwd": "/tmp", "cmd": "rm -rf /tmp/a", "reason": "x"}
        self._seed(first)
        raced = {"ts": 2, "cwd": "/tmp", "cmd": "rm -rf /tmp/b", "reason": "y"}
        rc, _, err = run_python_hook(
            self.sbx, "cleanup-sweep.py", ("--append",), stdin_text=json.dumps(raced)
        )
        self.assertEqual(rc, 0, err)
        rc, _, err = self._sweep("--remove", "0")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._queue(), [raced])
    def test_pre_existing_credentials_scrubbed_in_place(self):
        # Entries written before the careful hook redacted on write may hold a
        # raw token; any sweep must scrub the log in place and never echo it.
        pat = "github_pat_11FAKEFAKE0abcdefghijklmnopqrstuvwxyz0123456789"
        self._seed({
            "ts": 1, "cwd": "/tmp",
            "cmd": 'curl -H "Authorization: Bearer %s" x; rm -rf /tmp/scratch-x' % pat,
            "reason": "unrecognized",
        })
        rc, out, err = self._sweep()
        self.assertEqual(rc, 0)
        self.assertNotIn(pat, out, "report must not echo the raw token")
        self.assertNotIn(pat, err, "stderr must not echo the raw token")
        with open(self.sbx.cleanup_log) as fh:
            logged = fh.read()
        self.assertNotIn(pat, logged, "log must be scrubbed in place")
        self.assertIn("***REDACTED***", logged)
        rc, out, _ = self._sweep("--count")
        self.assertEqual(out.strip(), "1", "scrub must not drop entries")

    def test_pre_existing_aws_temporary_credential_scrubbed(self):
        # ASIA (AWS STS temporary access-key ID) must be scrubbed like AKIA.
        asia = "ASIA" + "Q" * 16
        self._seed({
            "ts": 1, "cwd": "/tmp",
            "cmd": "TOK=%s; rm -rf /tmp/scratch-y" % asia,
            "reason": "unrecognized",
        })
        rc, out, _ = self._sweep()
        self.assertEqual(rc, 0)
        self.assertNotIn(asia, out, "report must not echo the raw AWS temp credential")
        with open(self.sbx.cleanup_log) as fh:
            logged = fh.read()
        self.assertNotIn(asia, logged, "log must be scrubbed in place")
        self.assertIn("***REDACTED***", logged)

    def test_pre_existing_credential_in_cwd_scrubbed(self):
        # A credential-shaped cwd field must be scrubbed too, not just cmd/reason.
        pat = "github_pat_11FAKEFAKE0abcdefghijklmnopqrstuvwxyz0123456789"
        self._seed({
            "ts": 1, "cwd": "/tmp/%s" % pat,
            "cmd": "rm -rf /tmp/scratch-z",
            "reason": "unrecognized",
        })
        rc, out, _ = self._sweep()
        self.assertEqual(rc, 0)
        self.assertNotIn(pat, out, "report must not echo the raw token from cwd")
        with open(self.sbx.cleanup_log) as fh:
            logged = fh.read()
        self.assertNotIn(pat, logged, "cwd field must be scrubbed in place")
        self.assertIn("***REDACTED***", logged)

    def test_redacted_path_field_blocks_auto_run(self):
        # If the cwd (or cmd) was redacted, the entry's path-bearing fields no
        # longer match reality — auto-resolving --run against a redacted cwd
        # could silently clear the entry without deleting the real target, or
        # worse, resolve into an unrelated path. Such an entry must be kept
        # for manual review, never auto-run.
        pat = "github_pat_11FAKEFAKE0abcdefghijklmnopqrstuvwxyz0123456789"
        target = os.path.join(self.sbx.dir, "victim2")
        os.makedirs(target)
        self._seed({
            "ts": 1, "cwd": os.path.join(self.sbx.dir, pat),
            "cmd": "rm -rf %s" % target,
            "reason": "unrecognized",
        })
        # First touch (any subcommand) triggers load()'s self-heal redaction.
        rc, out, _ = self._sweep("--count")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "1")

        rc, out, _ = self._sweep("--run", "0")
        self.assertEqual(rc, 0)
        self.assertIn("redact", out.lower(), "must explain why it refused to auto-run")
        self.assertFalse(os.path.exists(os.path.join(target, "nonexistent")))
        self.assertTrue(os.path.isdir(target), "target must NOT be touched")

        rc, out, _ = self._sweep("--count")
        self.assertEqual(out.strip(), "1", "entry with a redacted path field must remain queued")

    def test_catastrophic_path_refused(self):
        # A parser slip pointing at "/" must be refused, and the entry kept.
        self._seed({"ts": 1, "cwd": "/", "cmd": "rm -rf /", "reason": "x"})
        rc, out, _ = self._sweep("--run", "0")
        self.assertEqual(rc, 0)
        self.assertIn("refused", out.lower())
        self.assertTrue(os.path.isdir("/"), "root must still exist")
        rc, out, _ = self._sweep("--count")
        self.assertEqual(out.strip(), "1", "refused entry must remain queued")


if __name__ == "__main__":
    unittest.main()
