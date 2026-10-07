"""The UNCONFIGURED path: with nothing set, an adopter gets Claude review plus
single-account CodeRabbit -- no Codex lane, no CodeRabbit seat pool, and no
GitHub commit status published on their repo.

Each optional lane in the PRlaunch stack is opt-in:
  * CodeRabbit seat pool   -- only when seats are registered under
                              ~/.claude/cr-seats (or $CR_SEATS_DIR).
  * review-gate/cr-cli     -- only when PRLAUNCH_PUBLISH_CR_STATUS=1.
    commit status
  * Codex second opinion   -- only when CODEX_REVIEW_ENABLED=1 AND a
                              codex-review.sh wrapper is installed; no model id
                              is ever defaulted.

These tests run the real hooks in a sandbox HOME with every one of those knobs
scrubbed from the environment, so an ambient export on the developer's machine
cannot make the default path look configured.
"""
import glob
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REAL_HOOKS, REPO_ROOT, HookSandbox, make_git_repo

OPT_IN_VARS = (
    "CR_SEATS_DIR", "CR_SEAT_HOURLY_MAX", "CR_SEAT_WAIT_MAX", "CR_SEAT_WAIT_REFRESH",
    "PRLAUNCH_PUBLISH_CR_STATUS", "CODEX_REVIEW_ENABLED", "CODEX_REVIEW_MODEL",
)

# A `coderabbit` stand-in: records the HOME it ran under and its argv, then
# answers per $CR_STUB_MODE ("ok" or "limit").
CR_STUB = r"""#!/bin/bash
printf 'HOME=%s ARGS=%s\n' "$HOME" "$*" >> "$CR_STUB_LOG"
if [ "${CR_STUB_MODE:-ok}" = "limit" ]; then
  echo "Review limit reached"
  exit 1
fi
echo "Review complete. 0 issues found."
"""

GH_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$GH_STUB_LOG"
exit 0
"""


def _clean_env(sbx, extra=None):
    env = sbx.env(shim_path=True)
    for k in OPT_IN_VARS:
        env.pop(k, None)
    env.update(extra or {})
    return env


def _lines(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [ln for ln in fh.read().splitlines() if ln.strip()]


class CodeRabbitSeatPoolOffByDefaultTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()
        self.sbx.add_shim("coderabbit", CR_STUB)
        self.log = os.path.join(self.sbx.dir, "cr-calls.log")

    def tearDown(self):
        self.sbx.close()

    def _wrap(self, mode="ok"):
        return subprocess.run(
            ["bash", os.path.join(REAL_HOOKS, "cr-review.sh"), "--base", "main"],
            capture_output=True, text=True,
            env=_clean_env(self.sbx, {"CR_STUB_LOG": self.log, "CR_STUB_MODE": mode}),
        )

    def test_unconfigured_runs_the_cli_once_as_the_user(self):
        p = self._wrap()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("falling back to the default identity", p.stderr)
        calls = _lines(self.log)
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0], "HOME=%s ARGS=review --base main" % self.sbx.home)
        self.assertIn("Review complete", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.sbx.claude, "cr-seats")),
                         "the default path must not create a seat pool")

    def test_unconfigured_rate_limit_is_the_authorized_skip(self):
        p = self._wrap(mode="limit")
        self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
        self.assertEqual(len(_lines(self.log)), 1, "no rotation without a pool")


class ReviewGateStatusOffByDefaultTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()
        self.repo = os.path.join(self.sbx.dir, "myrepo")
        make_git_repo(self.repo, "me/eng-7-x", self.sbx.env())
        subprocess.run(
            ["git", "-C", self.repo, "remote", "add", "origin",
             "https://github.com/example-org/example-repo.git"],
            check=True, env=self.sbx.env(), capture_output=True,
        )
        self.sbx.add_shim("gh", GH_STUB)
        self.gh_log = os.path.join(self.sbx.dir, "gh-calls.log")

    def tearDown(self):
        self.sbx.close()

    def _gate(self, *args, **extra):
        env = {"GH_STUB_LOG": self.gh_log}
        env.update(extra)
        # Not run_hook_args: that re-merges os.environ, which would let an
        # ambient opt-in export leak back into the "unconfigured" run.
        p = subprocess.run(
            ["bash", self.sbx.hook_path("prlaunch-gate.sh"), "--repo-dir", self.repo, *args],
            capture_output=True, text=True, env=_clean_env(self.sbx, env),
        )
        return p.returncode, p.stdout, p.stderr

    def test_record_cr_cli_writes_the_ledger_and_calls_no_github_api(self):
        rc, out, err = self._gate("record", "cr_cli")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(_lines(self.gh_log), [], "status published without opt-in")
        rc, out, err = self._gate("path")
        self.assertTrue(os.path.exists(out.strip()), "ledger not written")

    def test_publish_cr_cli_says_off_and_calls_no_github_api(self):
        self._gate("record", "cr_cli")
        rc, out, err = self._gate("publish-cr-cli")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("publishing is off", err)
        self.assertEqual(_lines(self.gh_log), [])

    def test_opting_in_does_publish(self):
        # Control: the stub and remote are wired correctly, so the silence
        # above is the default at work, not a broken rig.
        self._gate("record", "cr_cli")
        rc, out, err = self._gate("publish-cr-cli", PRLAUNCH_PUBLISH_CR_STATUS="1")
        self.assertEqual(rc, 0, out + err)
        calls = _lines(self.gh_log)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("repos/example-org/example-repo/statuses/", calls[0])


class CodexLaneOffByDefaultTest(unittest.TestCase):
    """The Codex lane exists only as a documented, opt-in step of deep-review.
    Nothing shipped here may run Codex, or name a model, by default."""

    def test_no_shipped_hook_invokes_codex(self):
        offenders = []
        for path in glob.glob(os.path.join(REAL_HOOKS, "*")):
            if not os.path.isfile(path) or path.endswith(".test.sh"):
                continue
            with open(path, errors="replace") as fh:
                if re.search(r"\bcodex\s+exec\b", fh.read()):
                    offenders.append(os.path.basename(path))
        self.assertEqual(offenders, [], "hooks that dispatch Codex: %r" % offenders)

    def test_deep_review_gates_the_lane_on_explicit_opt_in(self):
        with open(os.path.join(REPO_ROOT, "commands", "deep-review.md")) as fh:
            text = fh.read()
        m = re.search(r"### Codex second-opinion lane[^\n]*\n(.*?)\n### ", text, re.S)
        self.assertIsNotNone(m, "Codex lane section not found")
        lane = m.group(0)
        self.assertIn("OFF by default", lane)
        self.assertIn("CODEX_REVIEW_ENABLED=1", lane)
        self.assertIn("unset means the Codex account default", lane)

    def test_no_model_id_is_defaulted_anywhere(self):
        pat = re.compile(r"CODEX_REVIEW_MODEL=[\w.-]+|\bgpt-\d")
        hits = []
        for rel in ("commands/*.md", "hooks/*", "README.md"):
            for path in glob.glob(os.path.join(REPO_ROOT, rel)):
                if not os.path.isfile(path):
                    continue
                with open(path, errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if pat.search(line):
                            hits.append("%s:%d" % (os.path.relpath(path, REPO_ROOT), i))
        self.assertEqual(hits, [], "model ids / defaulted model: %r" % hits)


if __name__ == "__main__":
    unittest.main()
