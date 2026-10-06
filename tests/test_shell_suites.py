"""CI gate for the repo's SHELL test suites — hooks/*.test.sh.

run-tests.sh runs pytest over tests/ and nothing else, so a hooks/<x>.test.sh
suite would otherwise run only when a human remembered to run it by hand. This
wrapper lives in tests/, which run-tests.sh already collects, so every shell
suite is gated the moment it lands — no second suite list to drift.

DISCOVERY IS A GLOB, NOT A LIST: a new hooks/<x>.test.sh is gated the day it is
added. Discovery is asserted non-empty so "everything passed" can never mean
"looked at nothing".
"""
import glob
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT

# A hang-catcher, not a budget: a suite that blocks on a lock or a stray `read`
# fails with its partial output instead of wedging the CI job.
SUITE_TIMEOUT = 300


def _shell_suites():
    return sorted(glob.glob(os.path.join(REPO_ROOT, "hooks", "*.test.sh")))


class ShellSuiteTests(unittest.TestCase):
    def test_shell_suites_are_discovered(self):
        names = {os.path.basename(p) for p in _shell_suites()}
        self.assertIn("bash-stdin-guard.test.sh", names,
                      "hooks/*.test.sh discovery found nothing it should")

    def test_every_shell_suite_passes(self):
        suites = _shell_suites()
        self.assertTrue(suites, "no hooks/*.test.sh discovered")
        for path in suites:
            rel = os.path.relpath(path, REPO_ROOT)
            with self.subTest(suite=rel):
                try:
                    proc = subprocess.run(["/bin/bash", path], capture_output=True,
                                          text=True, cwd=REPO_ROOT,
                                          timeout=SUITE_TIMEOUT)
                except subprocess.TimeoutExpired as exc:
                    # TimeoutExpired carries bytes even under text=True.
                    out, err = (v.decode(errors="replace") if isinstance(v, bytes) else (v or "")
                                for v in (exc.stdout, exc.stderr))
                    self.fail("%s did not finish within %ds\n%s%s"
                              % (rel, SUITE_TIMEOUT, out, err))
                self.assertEqual(proc.returncode, 0, "%s failed (exit %d)\n%s%s"
                                 % (rel, proc.returncode, proc.stdout, proc.stderr))


if __name__ == "__main__":
    unittest.main()
