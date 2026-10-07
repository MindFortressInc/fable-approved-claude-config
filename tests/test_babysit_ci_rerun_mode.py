#!/usr/bin/env python3
"""Tests for skills/babysit/ci_rerun_mode.py: a ci_triage transient re-run does
a FULL `gh run rerun` only when a failed job ran on GitHub-hosted AND the run
has a router job AND the PR lacks the fast-lane label (`ci:fast`); `--failed`
otherwise.

Job fixtures mirror the two real shapes that motivated the module:
  HOSTED_RED_RUN -- shards failed on ubuntu-latest; `--failed` re-runs kept
                    them on hosted for 4-5 attempts.
  FLEET_RED_RUN  -- a shard failed on a self-hosted runner.
The CLI tests drive main() against a fake `gh` on PATH; the real gh is never called.
"""
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "babysit")
SCRIPT = os.path.join(SKILL_DIR, "ci_rerun_mode.py")

sys.path.insert(0, SKILL_DIR)
import ci_rerun_mode  # noqa: E402
from ci_rerun_mode import decide, is_hosted, is_router  # noqa: E402

REPO = "your-org/acme-api"
RUN = "123456789"


def job(name, conclusion, label, group=None):
    if group is None:
        group = "GitHub Actions" if label.startswith("ubuntu-") else "default"
    return {"name": name, "conclusion": conclusion, "labels": [label], "runner_group_name": group}


ROUTER = job("pick runner backend", "success", "ubuntu-latest")
HOSTED_RED_RUN = [
    job("pytest (postgres-gated)", "success", "build-box-8c"),
    ROUTER,
    job("smoke", "success", "ubuntu-latest"),
    job("pytest (shard 3)", "failure", "ubuntu-latest"),
    job("pytest (shard 0)", "failure", "ubuntu-latest"),
    job("pytest (shard 1)", "failure", "ubuntu-latest"),
    job("pytest (shard 2)", "cancelled", "ubuntu-latest"),
]
FLEET_RED_RUN = [
    ROUTER,
    job("route-acme-ci / pick runner backend", "success", "ubuntu-latest"),
    job("pytest (shard 0)", "failure", "self-hosted-8c"),
    job("pytest (shard 1)", "success", "self-hosted-8c"),
]


class DecideTests(unittest.TestCase):
    def test_hosted_failure_with_router_is_a_full_rerun(self):
        mode, _, hosted = decide(HOSTED_RED_RUN, [])
        self.assertEqual(mode, "full")
        self.assertEqual(hosted, ["pytest (shard 3)", "pytest (shard 0)", "pytest (shard 1)", "pytest (shard 2)"])

    def test_self_hosted_failure_keeps_failed(self):
        self.assertEqual(decide(FLEET_RED_RUN, [])[0], "failed")

    def test_no_router_job_keeps_failed(self):
        jobs = [j for j in HOSTED_RED_RUN if not is_router(j)]
        mode, reason, _ = decide(jobs, [])
        self.assertEqual(mode, "failed")
        self.assertIn("no router", reason)

    def test_ci_fast_keeps_failed(self):
        mode, reason, _ = decide(HOSTED_RED_RUN, ["ci:fast", "arm:prlaunch"])
        self.assertEqual(mode, "failed")
        self.assertIn("ci:fast", reason)

    def test_unreadable_labels_keep_failed(self):
        self.assertEqual(decide(HOSTED_RED_RUN, None)[0], "failed")

    def test_no_failed_job_keeps_failed(self):
        green = [dict(j, conclusion="success") for j in HOSTED_RED_RUN]
        self.assertEqual(decide(green, [])[0], "failed")

    def test_hosted_router_alone_does_not_count_as_a_hosted_failure(self):
        # The router itself always runs on ubuntu-latest; only FAILED jobs count.
        self.assertEqual(decide(FLEET_RED_RUN, [])[0], "failed")

    def test_cancelled_only_hosted_job_still_reroutes(self):
        jobs = [ROUTER, job("pytest (shard 2)", "cancelled", "ubuntu-latest")]
        self.assertEqual(decide(jobs, [])[0], "full")

    def test_reusable_router_name_alone_is_a_router(self):
        jobs = [job("route-acme-ci / pick runner backend", "success", "ubuntu-latest"),
                job("quality", "failure", "ubuntu-latest")]
        self.assertEqual(decide(jobs, [])[0], "full")


class ClassifierTests(unittest.TestCase):
    def test_hosted_labels(self):
        for label in ("ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "windows-latest", "macos-14"):
            self.assertTrue(is_hosted({"labels": [label]}), label)
        for label in ("self-hosted-8c", "build-box-8c", "dev-mac-1"):
            self.assertFalse(is_hosted({"labels": [label]}), label)

    def test_hosted_runner_group_counts_even_with_a_custom_label(self):
        self.assertTrue(is_hosted({"labels": ["big-linux"], "runner_group_name": "GitHub Actions"}))

    def test_self_hosted_runner_group_beats_a_hosted_looking_label(self):
        # A self-hosted runner may carry a custom ubuntu-latest label.
        self.assertFalse(is_hosted({"labels": ["ubuntu-latest"], "runner_group_name": "default"}))

    def test_self_hosted_label_wins(self):
        self.assertFalse(is_hosted({"labels": ["self-hosted", "macos-14"]}))

    def test_router_names(self):
        self.assertTrue(is_router({"name": "pick runner backend"}))
        self.assertTrue(is_router({"name": "route-acme-ci / pick runner backend"}))
        self.assertTrue(is_router({"name": "route / anything"}))
        self.assertFalse(is_router({"name": "pytest (shard 0)"}))
        self.assertFalse(is_router({"name": "route"}))


class EnvOverrideTests(unittest.TestCase):
    """The router job name / caller prefixes / fast label are repo-specific, so
    they are env-configurable. They are read at import, so each test reloads
    the module under the patched env and reloads it again afterwards."""

    def _reload_with(self, **env):
        with mock.patch.dict(os.environ, env):
            importlib.reload(ci_rerun_mode)
        self.addCleanup(importlib.reload, ci_rerun_mode)

    def test_router_job_env_override_changes_is_router(self):
        self.assertFalse(ci_rerun_mode.is_router({"name": "choose runners"}))
        self._reload_with(BABYSIT_CI_ROUTER_JOB="choose runners")
        self.assertTrue(ci_rerun_mode.is_router({"name": "choose runners"}))
        self.assertTrue(ci_rerun_mode.is_router({"name": "route / choose runners"}))
        self.assertFalse(ci_rerun_mode.is_router({"name": "pick runner backend"}))


FAKE_GH = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    a = sys.argv[1:]
    fx = json.load(open(os.environ["FAKE_GH_FIXTURE"]))
    if a[0] == "pr":
        if fx.get("labels") is None:
            sys.exit(1)
        print(json.dumps(fx["labels"]))
    elif "/attempts/" in a[2]:
        if fx.get("jobs") is None:
            sys.stderr.write("HTTP 502"); sys.exit(1)
        for j in fx["jobs"]:
            print(json.dumps(j))
    else:
        print(fx["attempt"])
    ''')


class CliTests(unittest.TestCase):
    def run_cli(self, fixture, *extra):
        with tempfile.TemporaryDirectory() as d:
            gh = os.path.join(d, "gh")
            with open(gh, "w") as f:
                f.write(FAKE_GH)
            os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
            fx = os.path.join(d, "fx.json")
            with open(fx, "w") as f:
                json.dump(fixture, f)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"], FAKE_GH_FIXTURE=fx)
            for k in ("BABYSIT_CI_ROUTER_JOB", "BABYSIT_CI_ROUTER_CALLERS", "BABYSIT_CI_FAST_LABEL"):
                env.pop(k, None)
            p = subprocess.run([sys.executable, SCRIPT, "--repo", REPO,
                                "--run", RUN, "--pr", "42", *extra],
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_full_rerun_command_has_no_failed_flag(self):
        out = self.run_cli({"attempt": 1, "jobs": HOSTED_RED_RUN, "labels": []})
        self.assertEqual(out["mode"], "full")
        self.assertEqual(out["cmd"], "gh run rerun %s -R %s" % (RUN, REPO))
        self.assertEqual(out["attempt"], 1)

    def test_self_hosted_failure_command_keeps_failed_flag(self):
        out = self.run_cli({"attempt": 1, "jobs": FLEET_RED_RUN, "labels": []})
        self.assertEqual(out["cmd"], "gh run rerun %s -R %s --failed" % (RUN, REPO))

    def test_live_ci_fast_label_keeps_failed(self):
        self.assertEqual(self.run_cli({"attempt": 1, "jobs": HOSTED_RED_RUN, "labels": ["ci:fast"]})["mode"], "failed")

    def test_unreadable_jobs_fall_back_to_failed(self):
        out = self.run_cli({"attempt": 1, "jobs": None, "labels": []})
        self.assertEqual(out["mode"], "failed")
        self.assertIn("unreadable", out["reason"])

    def test_unreadable_labels_fall_back_to_failed(self):
        self.assertEqual(self.run_cli({"attempt": 1, "jobs": HOSTED_RED_RUN, "labels": None})["mode"], "failed")

    def test_explicit_attempt_is_honoured(self):
        self.assertEqual(self.run_cli({"attempt": 6, "jobs": HOSTED_RED_RUN, "labels": []}, "--attempt", "1")["attempt"], 1)


if __name__ == "__main__":
    unittest.main()
