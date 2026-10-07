"""Reader/writer pairing guard for the PRlaunch phase-6 wrapup ledger read.

`commands/PRlaunch.md` phase 6 appends one `prlaunch`/`unit` row to the
automation ledger, pulling `cr_cli` / `outcome_eval` back out of the unit's gate
ledger. That gate ledger is WRITTEN by `hooks/prlaunch-gate.sh`, which owns its
path. The snippet used to RECONSTRUCT `<repo>--<branch-slug>` from the checkout
dirname, which in a worktree never matched anything (back when the gate keyed on
that dirname); `jq` failed, `2>/dev/null` swallowed it, `//` never fired (the
failure is no result, not a null result) and `cr_cli` landed as `""`. Every
PRlaunch reaches phase 6 from a worktree (committing from a primary clone is
blocked by the worktree hook), so the ledger's CR-CLI series measured nothing at
all.

These tests execute the ACTUAL snippet text out of the markdown against a real
worktree + a real `prlaunch-gate.sh record`, so the two sides cannot drift apart
again without a red test. Asserting on prose would not have caught the original
bug -- the prose was fine, the path was wrong.
"""
import glob
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT, HookSandbox, make_git_repo, run_hook_args

PRLAUNCH_MD = os.path.join(REPO_ROOT, "commands", "PRlaunch.md")

# The snippet slice under test: from the ledger-path resolution through the
# outcome_eval read. Both anchors must be present -- if the snippet is
# restructured so either disappears, this test FAILS rather than silently
# extracting nothing and passing vacuously.
START = "L=$(~/.claude/hooks/prlaunch-gate.sh path"
END = 'OE="<N> scenarios"; fi'


def extract_block():
    """Return the whole phase-6 wrapup bash block verbatim from commands/PRlaunch.md."""
    with open(PRLAUNCH_MD) as fh:
        text = fh.read()
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    blocks = [b for b in blocks if 'ledger-append.sh "$U"' in b]
    if len(blocks) != 1:
        raise AssertionError(
            "expected exactly 1 phase-6 wrapup bash block in %s, found %d"
            % (PRLAUNCH_MD, len(blocks))
        )
    return blocks[0]


def extract_snippet(block):
    """Slice the ledger-path resolution + the two gate reads out of `block`."""
    lines = block.splitlines()
    starts = [i for i, ln in enumerate(lines) if START in ln]
    ends = [i for i, ln in enumerate(lines) if END in ln]
    if len(starts) != 1 or len(ends) != 1:
        raise AssertionError(
            "phase-6 snippet no longer resolves the ledger path via "
            "`prlaunch-gate.sh path` (start=%d end=%d matches). If the "
            "resolution moved, move this test with it -- do NOT go back to "
            "reconstructing <repo>--<branch-slug>."
            % (len(starts), len(ends))
        )
    return "\n".join(lines[starts[0]:ends[0] + 1])


class WrapupLedgerPathTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()
        self.repo = os.path.join(self.sbx.dir, "api-svc")
        self.branch = "me/eng-9234-x"
        make_git_repo(self.repo, "main", self.sbx.env())
        # A worktree named the house way (<repo>.<slug>) -- its basename is NOT
        # the repo name, which is the whole bug.
        self.wt = os.path.join(self.sbx.dir, "api-svc.eng-9234")
        subprocess.run(
            ["git", "-C", self.repo, "worktree", "add", "-q", "-b", self.branch, self.wt],
            check=True, env=self.sbx.env(), capture_output=True, text=True,
        )
        self.block = extract_block()
        self.snippet = extract_snippet(self.block)

    def tearDown(self):
        self.sbx.close()

    # -- helpers ----------------------------------------------------------
    def gate(self, *args):
        return run_hook_args(self.sbx, "prlaunch-gate.sh", list(args), cwd=self.wt)

    def record_read_gates(self, *cr_args):
        """Record the two gates phase 6 reads back.

        Phase 6 only runs after `prlaunch-gate.sh check` passed, so BOTH are
        always present by then -- the snippet is entitled to demand them.
        """
        rc, out, err = self.gate("record", "cr_cli", *cr_args)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.gate("record", "outcome_eval", "--na", "no user-facing surface")
        self.assertEqual(rc, 0, out + err)

    def run_snippet(self):
        """Execute the extracted snippet with cwd=the worktree; report L/CR/OE."""
        script = "set -uo pipefail\n%s\nprintf 'L=%%s\\nCR=%%s\\nOE=%%s\\n' \"$L\" \"$CR\" \"$OE\"\n" % self.snippet
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, env=self.sbx.env(), cwd=self.wt,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def fields(self, stdout):
        return dict(
            ln.split("=", 1) for ln in stdout.strip().splitlines() if "=" in ln
        )

    def written_ledger(self):
        """The ledger prlaunch-gate.sh ACTUALLY wrote -- discovered, never rebuilt.

        Deliberately key-agnostic: what the key IS has changed before, and the
        invariant under test is that the reader finds the writer's file, whatever
        it is called.
        """
        found = sorted(glob.glob(os.path.join(self.sbx.prlaunch_ok, "*.json")))
        self.assertEqual(len(found), 1, "expected exactly one gate ledger, got %r" % found)
        return found[0]

    # -- the regression ---------------------------------------------------
    def test_snippet_reads_the_ledger_prlaunch_gate_actually_wrote(self):
        """A worktree run must read back the REAL cr_cli skip-reason, not ""."""
        self.record_read_gates("--skipped", "rate limited (exit 75)")
        written = self.written_ledger()
        # Repo-identity keying COLLAPSED THIS DRIFT ON PURPOSE. This assertion
        # used to be assertNotEqual: before the gate keyed on repo IDENTITY it
        # keyed on `basename(git rev-parse --show-toplevel)`, which inside this
        # worktree is "api-svc.eng-9234" -- so a naive <repo>--<branch> rebuild
        # guessed wrong, and that divergence was the original bug.
        # The gate now resolves the real repo, so the naive guess COINCIDES. The
        # contract under test is unchanged and still load-bearing: the snippet
        # must ASK the gate rather than rebuild (pinned independently by
        # test_snippet_asks_the_owner_instead_of_rebuilding_the_path, which
        # stays red if the snippet ever goes back to guessing).
        self.assertEqual(
            os.path.basename(written),
            "%s--%s.json" % (os.path.basename(self.repo), self.branch.replace("/", "-")),
            "the gate keys on repo identity, so a worktree run "
            "must land on the repo-keyed ledger, not a worktree-dir-keyed one",
        )

        rc, out, err = self.run_snippet()
        self.assertEqual(rc, 0, out + err)
        got = self.fields(out)
        self.assertEqual(got["L"], written,
                         "snippet resolved a different path than the gate wrote")
        self.assertEqual(got["CR"], "rate limited (exit 75)")

    def test_snippet_asks_the_owner_instead_of_rebuilding_the_path(self):
        """The bug class, pinned as prose: no hand-built ledger path.

        Key-agnostic on purpose -- what the key is may change again. What
        must never come back is a caller rebuilding a path prlaunch-gate.sh owns.
        """
        self.assertIn("prlaunch-gate.sh path", self.block)
        self.assertNotIn("<repo>--<branch-slug>", self.block)
        # and no `2>/dev/null` on a gate-ledger read -- that is what turned the
        # missing file into a silent "" in the first place.
        for ln in self.snippet.splitlines():
            if "jq" in ln:
                self.assertNotIn("2>/dev/null", ln, "silenced jq read: %s" % ln.strip())

    def test_clean_cr_cli_reads_clean_not_empty(self):
        self.record_read_gates()
        rc, out, err = self.run_snippet()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.fields(out)["CR"], "clean")

    def test_outcome_eval_na_is_reported_as_na(self):
        self.record_read_gates()
        rc, out, err = self.run_snippet()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.fields(out)["OE"], "na")

    # -- fail loud --------------------------------------------------------
    def test_missing_ledger_aborts_instead_of_appending_an_empty_value(self):
        """No ledger => abort. A row saying cr_cli:"" is worse than no row."""
        rc, out, err = self.run_snippet()
        self.assertNotEqual(rc, 0, "a missing gate ledger must abort the append")
        self.assertIn("gate ledger not found", err)
        self.assertNotIn("CR=", out)

    def test_absent_gate_entry_aborts_instead_of_defaulting_to_clean(self):
        """`.gates.cr_cli.skipped // "clean"` would report a gate that never ran
        as a clean review. An empty `gates` object must abort (CR CLI finding)."""
        self.record_read_gates()
        with open(self.written_ledger(), "w") as fh:
            fh.write('{"repo":"r","branch":"b","gates":{}}')
        rc, out, err = self.run_snippet()
        self.assertNotEqual(rc, 0, "an unrecorded gate must abort the append")
        self.assertIn("no cr_cli/outcome_eval gate", err)
        self.assertNotIn("CR=", out)

    def test_outcome_eval_recorded_with_scenarios_is_not_reported_as_na(self):
        rc, out, err = self.gate("record", "cr_cli")
        self.assertEqual(rc, 0, out + err)
        scen = os.path.join(self.sbx.dir, "scen.md")
        with open(scen, "w") as fh:
            fh.write("scenario 1: PASS if the row carries the real cr_cli value\n")
        self.assertEqual(self.gate("record", "scenarios", scen)[0], 0)
        self.assertEqual(self.gate("record", "outcome_eval")[0], 0)
        rc, out, err = self.run_snippet()
        self.assertEqual(rc, 0, out + err)
        self.assertNotEqual(self.fields(out)["OE"], "na")

    def test_corrupt_ledger_aborts_instead_of_appending_an_empty_value(self):
        self.record_read_gates()
        with open(self.written_ledger(), "w") as fh:
            fh.write("{not json")
        rc, out, err = self.run_snippet()
        self.assertNotEqual(rc, 0, "an unparseable gate ledger must abort the append")
        self.assertIn("NOT appending a row", err)
        self.assertNotIn("CR=", out)


if __name__ == "__main__":
    unittest.main()
