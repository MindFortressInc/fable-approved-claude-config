"""skills/execute/trial_merge.py: merge a batch's open PRs together, read-only.

Every test drives the real script against real git fixtures: a bare `origin`, a
clone, and PR branches. Only `gh` is a PATH shim, returning canned PR JSON.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT

SCRIPT = os.path.join(REPO_ROOT, "skills", "execute", "trial_merge.py")
GIT_ID = [
    "-c", "user.name=t", "-c", "user.email=t@example.com",
    "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="trial-merge-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.origin = os.path.join(self.root, "origin.git")
        self.clone = os.path.join(self.root, "clone")
        self.scratch = os.path.join(self.root, "scratch")
        os.mkdir(self.scratch)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        subprocess.run(["git", "clone", "-q", self.origin, self.clone],
                       check=True, capture_output=True)
        self.git("switch", "-q", "-c", "main")
        self.write("shared.txt", "base\n")
        self.commit("base")
        self.git("push", "-q", "origin", "main")
        self.base = self.git("rev-parse", "HEAD")
        self.prs = {}
        bin_dir = os.path.join(self.root, "bin")
        os.mkdir(bin_dir)
        self.gh_file = os.path.join(self.root, "prs.json")
        shim = os.path.join(bin_dir, "gh")
        with open(shim, "w") as fh:
            fh.write(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "prs = json.load(open(os.environ['FAKE_GH_PRS']))\n"
                "pr = prs.get(sys.argv[3])\n"
                "if pr is None:\n"
                "    sys.exit('no such PR')\n"
                "print(json.dumps(pr))\n"
            )
        os.chmod(shim, 0o755)
        self.env = {**os.environ, "PATH": bin_dir + os.pathsep + os.environ["PATH"],
                    "FAKE_GH_PRS": self.gh_file}

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *GIT_ID, "-C", cwd or self.clone, *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, path, text):
        with open(os.path.join(self.clone, path), "w") as fh:
            fh.write(text)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def pr(self, number, branch, files, base="main", state="OPEN"):
        """Push `branch` off `base` carrying `files`, and register it as PR `number`."""
        self.git("switch", "-q", "-c", branch, base)
        for path, text in files.items():
            self.write(path, text)
        self.commit(branch)
        self.git("push", "-q", "origin", branch)
        self.prs[str(number)] = {
            "number": number, "state": state, "baseRefName": base, "headRefName": branch,
            "headRefOid": self.git("rev-parse", "HEAD"),
        }
        self.git("switch", "-q", "main")

    def run_script(self, *args):
        with open(self.gh_file, "w") as fh:
            json.dump(self.prs, fh)
        return subprocess.run(
            [sys.executable, SCRIPT, "--repo", self.clone, "--scratch-dir", self.scratch, *args],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def state(self):
        """Everything shared a run must leave untouched."""
        return {
            "clone_refs": self.git("for-each-ref"),
            "origin_refs": self.git("for-each-ref", cwd=self.origin),
            "worktrees": self.git("worktree", "list", "--porcelain"),
            "head": self.git("rev-parse", "HEAD"),
            "status": self.git("status", "--porcelain"),
            "stash": self.git("stash", "list"),
            "prs": json.dumps(self.prs),
        }

    def assert_nothing_left(self, before):
        self.assertEqual(before, self.state())
        self.assertEqual([], os.listdir(self.scratch))


class CleanBatch(Fixture):
    def test_merges_all_runs_tests_on_the_combined_tree_and_leaves_nothing(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        self.pr(2, "two", {"b.txt": "b\n"})
        fetch_head = os.path.join(self.clone, ".git", "FETCH_HEAD")
        before = self.state()
        r = self.run_script("--unit-test", "1=test -f b.txt", "--unit-test", "2=test -f a.txt",
                            "--test", "test -f a.txt && test -f b.txt", "1", "2")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn("VERDICT: CLEAN", r.stdout)
        self.assertRegex(r.stdout, r"\| #1 \| \w{7} \| clean \| pass \|")
        self.assertRegex(r.stdout, r"\| #2 \| \w{7} \| clean \| pass \|")
        self.assert_nothing_left(before)
        self.assertFalse(os.path.exists(fetch_head), "must not write the clone's FETCH_HEAD")

    def test_a_stale_remote_tracking_ref_stays_stale(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        self.pr(2, "two", {"b.txt": "b\n"})
        self.git("switch", "-q", "two")
        old = self.git("rev-parse", "HEAD")
        self.write("c.txt", "c\n")
        self.commit("more")
        self.git("push", "-q", "origin", "two")
        self.prs["2"]["headRefOid"] = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "main")
        self.git("update-ref", "refs/remotes/origin/two", old)
        self.git("branch", "-q", "-D", "two")
        before = self.state()
        r = self.run_script("--test", "test -f c.txt", "1", "2")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assert_nothing_left(before)

    def test_stacked_prs_merge_in_stack_order_whatever_order_they_were_named(self):
        self.pr(1, "parent", {"a.txt": "a\n"})
        self.pr(2, "child", {"b.txt": "b\n"}, base="parent")
        r = self.run_script("--test", "true", "2", "1")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertLess(r.stdout.index("| #1 |"), r.stdout.index("| #2 |"))
        self.assertIn("base main@", r.stdout)

    def test_a_pr_no_longer_open_is_skipped_and_named(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        self.pr(2, "gone", {"b.txt": "b\n"}, state="MERGED")
        r = self.run_script("--test", "test ! -f b.txt", "1", "2")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn("skipped: MERGED", r.stdout)

    def test_a_batch_with_nothing_open_says_so(self):
        self.pr(1, "gone", {"a.txt": "a\n"}, state="CLOSED")
        r = self.run_script("--test", "false", "1")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn("VERDICT: NOTHING OPEN", r.stdout)

    def test_no_tests_at_all_is_not_a_clean_verdict(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        self.pr(2, "two", {"b.txt": "b\n"})
        r = self.run_script("1", "2")
        self.assertEqual(3, r.returncode, r.stdout + r.stderr)
        self.assertNotIn("VERDICT: CLEAN", r.stdout)
        self.assertIn("NO TESTS RUN", r.stdout)

    def test_repo_hooks_do_not_fire_and_foreign_prunable_worktrees_survive(self):
        marker = os.path.join(self.root, "hook-fired")
        hooks = os.path.join(self.clone, ".git", "hooks")
        for name in ("post-merge", "post-checkout", "pre-merge-commit"):
            path = os.path.join(hooks, name)
            with open(path, "w") as fh:
                fh.write(f"#!/bin/sh\ntouch {marker}\n")
            os.chmod(path, 0o755)
        foreign = os.path.join(self.root, "foreign")
        self.git("worktree", "add", "-q", "--detach", foreign)
        shutil.rmtree(foreign)
        self.pr(1, "one", {"a.txt": "a\n"})
        self.pr(2, "two", {"b.txt": "b\n"})
        if os.path.exists(marker):
            os.remove(marker)
        before = self.state()
        self.assertIn(foreign, before["worktrees"])
        r = self.run_script("--test", "true", "1", "2")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(marker), "a repo hook fired during the trial merge")
        self.assert_nothing_left(before)


class Reported(Fixture):
    def test_conflict_names_file_and_both_prs_and_the_rest_still_merge_and_test(self):
        self.pr(1, "one", {"shared.txt": "one\n"})
        self.pr(2, "two", {"shared.txt": "two\n"})
        self.pr(3, "three", {"c.txt": "c\n"})
        before = self.state()
        r = self.run_script("--unit-test", "2=true", "--unit-test", "3=test -f c.txt",
                            "--test", "grep -q one shared.txt", "1", "2", "3")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"\| #2 \| \w{7} \| CONFLICT \| not run \|.*shared\.txt.*#1")
        self.assertRegex(r.stdout, r"\| #1 \| \w{7} \| clean \|.*#2")
        self.assertRegex(r.stdout, r"\| #3 \| \w{7} \| clean \| pass \|")
        self.assertIn("VERDICT: FAILED", r.stdout)
        self.assert_nothing_left(before)

    def test_conflict_with_the_base_itself_is_attributed_to_the_base(self):
        self.pr(1, "one", {"shared.txt": "one\n"})
        self.write("shared.txt", "moved on\n")
        self.commit("main moved")
        self.git("push", "-q", "origin", "main")
        r = self.run_script("--test", "true", "1")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"\| #1 \| \w{7} \| CONFLICT \|.*shared\.txt.*base")

    def test_recorded_rerere_resolution_cannot_auto_resolve_the_conflict(self):
        self.pr(1, "one", {"shared.txt": "one\n"})
        self.pr(2, "two", {"shared.txt": "two\n"})
        self.git("config", "rerere.enabled", "true")
        self.git("config", "rerere.autoUpdate", "true")
        self.git("switch", "-q", "--detach", "one")
        subprocess.run(["git", *GIT_ID, "-C", self.clone, "merge", "two"], capture_output=True)
        self.write("shared.txt", "resolved\n")
        self.git("add", "-A")
        self.git("commit", "-q", "--no-edit")
        self.git("switch", "-q", "main")
        r = self.run_script("--test", "true", "1", "2")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"\| #2 \| \w{7} \| CONFLICT \|")

    def test_clean_merge_that_breaks_a_test_only_when_combined_fails_with_its_output(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        self.pr(2, "two", {"b.txt": "b\n"})
        before = self.state()
        cmd = "if test -f a.txt && test -f b.txt; then echo BOOM-COMBINED; exit 7; fi"
        r = self.run_script("--unit-test", f"2={cmd}", "--test", "true", "1", "2")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"\| #2 \| \w{7} \| clean \| FAIL \|")
        self.assertIn("BOOM-COMBINED", r.stdout)
        self.assertIn("exit 7", r.stdout)
        self.assert_nothing_left(before)

    def test_failing_batch_wide_test_fails_the_verdict(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        r = self.run_script("--test", "false", "1")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn("VERDICT: FAILED", r.stdout)

    def test_undecodable_test_output_is_still_reported_as_the_failure(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        r = self.run_script("--test", r"printf '\377\376 BAD\n'; exit 4", "1")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn("BAD", r.stdout)
        self.assertIn("exit 4", r.stdout)

    def test_a_test_past_its_limit_is_killed_and_reported(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        started = time.time()
        r = self.run_script("--test-timeout", "1", "--test", "sleep 30", "1")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn("timed out", r.stdout)
        self.assertLess(time.time() - started, 20)
        self.assertEqual([], os.listdir(self.scratch))


class CouldNotRun(Fixture):
    def test_unreadable_pr_is_exit_2_and_creates_nothing(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        before = self.state()
        r = self.run_script("--test", "true", "1", "99")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertNotIn("VERDICT: CLEAN", r.stdout)
        self.assert_nothing_left(before)

    def test_unparseable_gh_output_is_exit_2_not_a_python_crash_exit_1(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        self.prs["1"] = {"number": 1}
        r = self.run_script("--test", "true", "1")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertEqual([], os.listdir(self.scratch))

    def test_a_fork_pr_is_refused_before_anything_runs(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        self.prs["1"]["isCrossRepository"] = True
        marker = os.path.join(self.root, "ran")
        before = self.state()
        r = self.run_script("--test", f"touch {marker}", "1")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertIn("fork", r.stderr)
        self.assertFalse(os.path.exists(marker))
        self.assert_nothing_left(before)

    def test_test_commands_get_no_credentials(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        self.env["GH_TOKEN"] = "ghp_SHOULD_NOT_LEAK"
        self.env["LINEAR_API_KEY"] = "lin_api_SHOULD_NOT_LEAK"
        r = self.run_script("--test", "env; exit 1", "1")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn("PATH=", r.stdout)
        self.assertNotIn("SHOULD_NOT_LEAK", r.stdout)

    def test_prs_on_two_different_roots_are_refused(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        self.git("push", "-q", "origin", "main:release")
        self.git("fetch", "-q", "origin", "release:release")
        self.pr(2, "two", {"b.txt": "b\n"}, base="release")
        r = self.run_script("--test", "true", "1", "2")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertIn("release", r.stderr)

    def test_unit_test_for_a_pr_not_in_the_batch_is_refused(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        r = self.run_script("--unit-test", "2=true", "1")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)

    def test_sigterm_mid_test_kills_the_test_and_still_cleans_up(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        before = self.state()
        with open(self.gh_file, "w") as fh:
            json.dump(self.prs, fh)
        pid_file = os.path.join(self.root, "test.pid")
        proc = subprocess.Popen(
            [sys.executable, SCRIPT, "--repo", self.clone, "--scratch-dir", self.scratch,
             "--test", f"echo $$ > {pid_file}; exec sleep 60", "1"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(200):
            if os.path.exists(pid_file) and open(pid_file).read().strip():
                break
            time.sleep(0.05)
        test_pid = int(open(pid_file).read())
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(2, proc.returncode, out + err)
        self.assertNotIn("VERDICT: CLEAN", out)
        self.assert_nothing_left(before)
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(test_pid, 0)


class ScratchDirDefault(Fixture):
    """The scratch root is config, never a hardcoded workspace path."""

    def run_default(self, extra_env):
        with open(self.gh_file, "w") as fh:
            json.dump(self.prs, fh)
        env = {k: v for k, v in self.env.items() if k != "TRIAL_MERGE_SCRATCH_DIR"}
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, SCRIPT, "--repo", self.clone, "--test", "pwd; exit 1", "1"],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def test_default_puts_the_worktree_next_to_the_clone(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        before = self.state()
        r = self.run_default({})
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn(os.path.join(self.root, "clone.trial-merge-"), r.stdout)
        self.assertEqual(before, self.state())
        self.assertFalse([d for d in os.listdir(self.root) if ".trial-merge-" in d])

    def test_env_var_overrides_the_default(self):
        self.pr(1, "one", {"a.txt": "a\n"})
        r = self.run_default({"TRIAL_MERGE_SCRATCH_DIR": self.scratch})
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn(os.path.join(self.scratch, "clone.trial-merge-"), r.stdout)
        self.assertEqual([], os.listdir(self.scratch))

    def test_script_names_no_workspace_path(self):
        with open(SCRIPT) as fh:
            src = fh.read()
        self.assertNotIn('expanduser("~/', src)


class SkillStatesTheStep(unittest.TestCase):
    def test_execute_and_orchestrate_both_run_it(self):
        for skill in ("execute", "orchestrate"):
            with open(os.path.join(REPO_ROOT, "skills", skill, "SKILL.md")) as fh:
                self.assertIn("trial_merge.py", fh.read(), skill)


if __name__ == "__main__":
    unittest.main()
