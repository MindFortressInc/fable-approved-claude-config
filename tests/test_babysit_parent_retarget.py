"""Regression tests for the parent-merged RETARGET rung.

Once a stacked PR's parent merged, the child stayed gated on a "semantic"
conflict even though the OTHER SIDE of that conflict had stopped existing.
The planner now relabels such a PR's `rebase` as `retarget`, and
`plan_parent_merged_retarget` (exposed as `babysit_classify.py
retarget-plan`) proves, read-only, whether pointing the child straight at the
default branch is safe: the parent's merge is in the base AND the child's own
(THREE-dot) file set does not overlap the files that merge brought in. A rung
that retargets on overlap would silently drop the parent's changes from the
child's rendered diff, so every input is proven against REAL git repos here,
never mocked.

Tiers:
  * ThreeDotVsTwoDotTests / ParentMergedRetargetDecisionTests -- real git.
  * ParentResolutionTests / RetargetPlanningTests -- the planner half (which
    PRs become `retarget`), gh seams stubbed in-process.
  * RetargetPlanCliTests -- the CLI the runbook calls.
  * RetargetRungDocContractTests -- the shipped ladder text in
    commands/babysit-prs.md documents the rung's rules.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "babysit")
SCRIPT = os.path.join(SKILL_DIR, "babysit_classify.py")
RUNBOOK = os.path.join(REPO_ROOT, "commands", "babysit-prs.md")
sys.path.insert(0, SKILL_DIR)

import babysit_classify as bc  # noqa: E402
from babysit_classify import (  # noqa: E402
    RETARGET_FALLTHROUGH_CANNOT_DETERMINE,
    RETARGET_FALLTHROUGH_NOT_ANCESTOR,
    RETARGET_FALLTHROUGH_OVERLAP,
    RETARGET_OK,
    files_brought_in_by_commit,
    plan_parent_merged_retarget,
    three_dot_changed_files,
)

LADDER_RE = re.compile(r"^### `rebase`.*?(?=^### `ci_triage`)", re.M | re.S)


def _git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                        text=True, timeout=15)
    assert r.returncode == 0, f"git {' '.join(args)} failed in {repo}: {r.stderr}"
    return r.stdout


class GitFixtureRepo:
    """A REAL git repo in a temp dir. Every test below runs actual git
    plumbing (merge-base, diff, show) against it -- never a mock."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="babysit-retarget-")
        _git(self.dir, "init", "-q")
        _git(self.dir, "config", "user.email", "a@b.com")
        _git(self.dir, "config", "user.name", "a")

    def commit(self, filename, content, message=None):
        with open(os.path.join(self.dir, filename), "w") as fh:
            fh.write(content)
        _git(self.dir, "add", filename)
        _git(self.dir, "commit", "-q", "-m", message or f"add {filename}")
        return self.rev_parse("HEAD")

    def checkout(self, branch, create=False):
        args = ["checkout", "-q"]
        if create:
            args.append("-b")
        args.append(branch)
        _git(self.dir, *args)

    def rev_parse(self, ref):
        return _git(self.dir, "rev-parse", ref).strip()

    def rename_branch_to_main(self):
        _git(self.dir, "branch", "-M", "main")

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)



def _two_dot(repo, base, head):
    """Tip-to-tip file set, computed here only to prove how it diverges from
    the three-dot set on a moved base -- the classifier never uses it."""
    return {p for p in _git(repo, "diff", "--name-only", f"{base}..{head}").splitlines() if p}


class ThreeDotVsTwoDotTests(unittest.TestCase):
    """A fixture where two-dot on a moved base yields a wrong file set and
    three-dot yields the right one."""

    def setUp(self):
        self.repo = GitFixtureRepo()
        self.repo.commit("base.txt", "base")
        self.repo.rename_branch_to_main()
        self.repo.checkout("child", create=True)
        self.repo.commit("child.txt", "child work")
        self.repo.checkout("main")
        # main moves on, independently of the child, AFTER the fork point.
        self.repo.commit("unrelated1.txt", "main moved on")
        self.repo.commit("unrelated2.txt", "main moved on again")

    def tearDown(self):
        self.repo.cleanup()

    def test_three_dot_yields_only_the_childs_own_files(self):
        self.assertEqual(three_dot_changed_files(self.repo.dir, "main", "child"),
                         {"child.txt"})

    def test_two_dot_is_corrupted_by_the_moved_base(self):
        files = _two_dot(self.repo.dir, "main", "child")
        self.assertIn("unrelated1.txt", files,
                      "two-dot reports main's OWN churn as if the child touched it")
        self.assertNotEqual(files, three_dot_changed_files(self.repo.dir, "main", "child"))

    def test_an_unresolvable_ref_is_none_not_an_empty_set(self):
        self.assertIsNone(three_dot_changed_files(self.repo.dir, "main", "no-such-ref"))


class ParentMergedRetargetDecisionTests(unittest.TestCase):
    """Real git fixtures for plan_parent_merged_retarget's three decision
    branches: not-an-ancestor, file overlap, and the safe/retarget case."""

    def setUp(self):
        self.repo = GitFixtureRepo()

    def tearDown(self):
        self.repo.cleanup()

    def _stack(self, *, child_touches_parent_file=False):
        """main -[base.txt]-> parent branch forks -[parentfile.txt]->
        child forks from the PARENT's pre-merge tip (the real stacked-PR
        shape: child was built ON TOP of parent) -[childfile.txt, and
        optionally ALSO an edit to parentfile.txt]-> main then merges
        parent in with a real 2-parent merge commit."""
        self.repo.commit("base.txt", "base")
        self.repo.rename_branch_to_main()
        self.repo.checkout("parent", create=True)
        self.repo.commit("parentfile.txt", "parent work")

        self.repo.checkout("child", create=True)  # forks from parent's tip
        self.repo.commit("childfile.txt", "child's own work")
        if child_touches_parent_file:
            with open(os.path.join(self.repo.dir, "parentfile.txt"), "a") as fh:
                fh.write("\nchild also edited this\n")
            _git(self.repo.dir, "add", "parentfile.txt")
            _git(self.repo.dir, "commit", "-q", "-m",
                 "child also touches parentfile.txt")

        self.repo.checkout("main")
        _git(self.repo.dir, "merge", "--no-ff", "-q", "parent",
             "-m", "merge parent into main")
        merge_sha = self.repo.rev_parse("HEAD")
        return merge_sha

    def test_zero_overlap_retargets_and_creates_no_merge_commit(self):
        merge_sha = self._stack(child_touches_parent_file=False)
        child_head_before = self.repo.rev_parse("child")
        commit_count_before = _git(
            self.repo.dir, "rev-list", "--count", "child").strip()

        result = plan_parent_merged_retarget(
            self.repo.dir, "main", "child", merge_sha)

        self.assertEqual(
            result["action"], RETARGET_OK,
            "zero file overlap with an ancestor parent merge must retarget")
        # THE SAFETY PROPERTY: this function performs NO git write at all.
        self.assertEqual(
            self.repo.rev_parse("child"), child_head_before,
            "plan_parent_merged_retarget must never create a commit on "
            "the child branch")
        self.assertEqual(
            _git(self.repo.dir, "rev-list", "--count", "child").strip(),
            commit_count_before,
            "no merge commit (or any commit) may be created by the "
            "retarget decision itself")

    def test_overlapping_file_set_does_not_retarget(self):
        """The stacked-child shape: the child independently touched a file the
        parent's merge ALSO touched. Assert EXPLICITLY that this does NOT
        retarget -- a rung that did would silently drop the parent's
        changes from the child's rendered diff."""
        merge_sha = self._stack(child_touches_parent_file=True)

        result = plan_parent_merged_retarget(
            self.repo.dir, "main", "child", merge_sha)

        self.assertEqual(
            result["action"], "fallthrough",
            "a non-empty file-set overlap must NOT retarget -- it must "
            "fall through to the existing worktree-merge path unchanged")
        self.assertEqual(result["reason"], RETARGET_FALLTHROUGH_OVERLAP)
        self.assertIn(
            "parentfile.txt", result["overlap_files"],
            "the overlapping file must be named, not just a bare refusal")

    def test_non_ancestor_parent_sha_falls_through(self):
        """A parent_merge_sha that is NOT actually in the base (a stale
        cache entry, a mismatched sha) must never be trusted -- fall
        through regardless of what the file sets say."""
        self._stack(child_touches_parent_file=False)
        self.repo.checkout("main")
        self.repo.checkout("orphan", create=True)
        orphan_sha = self.repo.commit("never_merged.txt", "never reaches main")
        self.repo.checkout("main")

        result = plan_parent_merged_retarget(
            self.repo.dir, "main", "child", orphan_sha)

        self.assertEqual(result["action"], "fallthrough")
        self.assertEqual(result["reason"], RETARGET_FALLTHROUGH_NOT_ANCESTOR)

    def test_a_real_git_failure_falls_through_never_reads_as_empty(self):
        """`git diff` exits 0
        for a genuinely EMPTY diff and only exit non-zero (128) when the
        diff could not be computed at all (a bad ref, a root commit with no
        `^1`). Conflating "empty" with "failed" would let a real git error
        masquerade as zero overlap and retarget anyway -- the same class of
        mistake this whole rung exists to prevent. Uses a REAL root commit
        (no parent at all) as the failure trigger, not a mock."""
        self.repo.commit("only.txt", "the repo's very first commit")
        root_sha = self.repo.rev_parse("HEAD")
        self.repo.rename_branch_to_main()
        self.repo.checkout("child", create=True)
        self.repo.commit("childfile.txt", "child's own work")

        # root_sha has no `^1` -- files_brought_in_by_commit must fail
        # loudly (None), not silently (empty set).
        self.assertIsNone(files_brought_in_by_commit(self.repo.dir, root_sha))

        result = plan_parent_merged_retarget(
            self.repo.dir, "main", "child", root_sha)
        self.assertEqual(result["action"], "fallthrough")
        self.assertEqual(result["reason"], RETARGET_FALLTHROUGH_CANNOT_DETERMINE)

    def test_files_brought_in_by_commit_reads_the_merges_own_diff(self):
        """The other operand of the overlap check, pinned directly: what
        the parent's merge commit actually changed on the base."""
        merge_sha = self._stack(child_touches_parent_file=False)
        self.assertEqual(
            files_brought_in_by_commit(self.repo.dir, merge_sha),
            {"parentfile.txt"})



NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


def entry(num, *, base="main", branch=None, title="", mss="DIRTY", lane="owner",
          parent_state=None, parent_pr=None, head_oid="a" * 40):
    return {
        "repo": "acme-api", "number": num, "branch": branch or f"feat/pr-{num}",
        "base": base, "blurb": title, "state": "CLEAN", "mss": mss,
        "mergeable": "CONFLICTING", "tier": "", "lane": lane,
        "failing_checks": [], "red_failing": [], "last_cr_activity": "",
        "created_at": "", "cr_inline_count": 0, "head_oid": head_oid,
        "parent_state": parent_state, "parent_pr": parent_pr,
    }


class ParentResolutionTests(unittest.TestCase):
    def test_title_marker_parses_and_garbage_does_not(self):
        self.assertEqual(bc.parse_stacked_on_pr("feat: x (stacked on #123)"), 123)
        self.assertEqual(bc.parse_stacked_on_pr("Stacked on #7"), 7)
        self.assertIsNone(bc.parse_stacked_on_pr("stacked on ENG-9661"))
        self.assertIsNone(bc.parse_stacked_on_pr(""))
        self.assertIsNone(bc.parse_stacked_on_pr(None))

    def test_title_wins_because_it_survives_the_post_merge_retarget(self):
        """After the parent merged, the child's base is already the default
        branch -- only the title still names the parent."""
        e = entry(11, base="main", title="child work (stacked on #10)")
        self.assertEqual(bc.resolve_parent_pr(e, {}), 10)

    def test_base_ref_resolves_to_a_sibling_entry_in_the_same_repo(self):
        e = entry(11, base="feat/parent")
        self.assertEqual(bc.resolve_parent_pr(e, {("acme-api", "feat/parent"): 10}), 10)
        self.assertIsNone(bc.resolve_parent_pr(e, {("acme-web", "feat/parent"): 10}))

    def test_a_default_base_without_a_marker_has_no_parent(self):
        self.assertIsNone(bc.resolve_parent_pr(entry(11, base="main"), {}))
        self.assertIsNone(bc.resolve_parent_pr(entry(11, base="develop"),
                                               {("acme-api", "develop"): 3}))

    def test_an_open_sibling_parent_costs_no_gh_call(self):
        with mock.patch.object(bc, "gh_json", side_effect=AssertionError("no gh")):
            self.assertEqual(bc.resolve_parent_state("gh", "your-org", "acme-api", 10,
                                                     {"acme-api": {10, 11}}),
                             ("OPEN", None))

    def test_a_merged_parent_is_read_from_gh(self):
        with mock.patch.object(bc, "gh_json", return_value={
                "state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}):
            self.assertEqual(bc.resolve_parent_state("gh", "your-org", "acme-api", 10, {}),
                             ("MERGED", "2026-01-01T00:00:00Z"))

    def test_an_unreadable_parent_fails_safe_to_unknown(self):
        with mock.patch.object(bc, "gh_json", return_value=None):
            self.assertEqual(bc.resolve_parent_state("gh", "your-org", "acme-api", 10, {}),
                             (None, None))


class RetargetPlanningTests(unittest.TestCase):
    def setUp(self):
        fd, self.store = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self.store, "w") as fh:
            json.dump({}, fh)
        self.addCleanup(os.remove, self.store)
        p = mock.patch.dict(os.environ, {"BABYSIT_PROGRESS": self.store})
        p.start()
        self.addCleanup(p.stop)

    def _actions(self, entries):
        actions, _ = bc.build_actions(entries, NOW, "no:test")
        return {(a["type"], a["pr"]) for a in actions}

    def test_a_merged_parent_relabels_rebase_as_retarget(self):
        got = self._actions([entry(11, parent_state="MERGED", parent_pr=10)])
        self.assertIn(("retarget", 11), got)
        self.assertNotIn(("rebase", 11), got)

    def test_an_open_or_unknown_parent_keeps_the_plain_rebase(self):
        got = self._actions([entry(11, parent_state="OPEN", parent_pr=10),
                             entry(12, parent_state=None, parent_pr=10)])
        self.assertIn(("rebase", 11), got)
        self.assertIn(("rebase", 12), got)
        self.assertFalse({t for t, _ in got} & {"retarget"})

    def test_retarget_shares_the_rebase_attempt_cap(self):
        """Same list, same gate: a capped PR at an unchanged head is not
        retargeted either -- its overlap fall-through would re-run the very
        merge that already failed."""
        with open(self.store, "w") as fh:
            json.dump({"fix_attempts": {"acme-api#11": {
                "count": 2, "last": "semantic conflict", "head": "a" * 40}}}, fh)
        got = self._actions([entry(11, parent_state="MERGED", parent_pr=10)])
        self.assertFalse(got)


class RetargetPlanCliTests(unittest.TestCase):
    def test_the_cli_prints_the_plan_as_json(self):
        repo = GitFixtureRepo()
        self.addCleanup(repo.cleanup)
        repo.commit("base.txt", "base")
        repo.rename_branch_to_main()
        repo.checkout("parent", create=True)
        repo.commit("parentfile.txt", "parent work")
        repo.checkout("child", create=True)
        repo.commit("childfile.txt", "child work")
        repo.checkout("main")
        _git(repo.dir, "merge", "--no-ff", "-q", "parent", "-m", "merge parent")
        merge_sha = repo.rev_parse("HEAD")
        repo.checkout("child")
        p = subprocess.run([sys.executable, SCRIPT, "retarget-plan", "--worktree", repo.dir,
                            "--default-ref", "main", "--parent-merge-sha", merge_sha],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["action"], RETARGET_OK)


class RetargetRungDocContractTests(unittest.TestCase):
    """The shipped ladder text, extracted rather than trusted to a copy that
    would keep passing after the runbook drifted."""

    def _ladder_text(self):
        with open(RUNBOOK) as fh:
            m = LADDER_RE.search(fh.read())
        self.assertIsNotNone(m, "the `rebase`/`retarget` ladder section moved or was renamed")
        return m.group(0)

    def test_the_heading_covers_both_action_types(self):
        self.assertRegex(self._ladder_text(), r"^### `rebase` */ *`retarget`")

    def test_the_rung_is_gated_on_parent_state_merged(self):
        text = self._ladder_text()
        self.assertIn('parent_state == "MERGED"', text)
        self.assertIn("mode: retarget", text)

    def test_the_rung_uses_the_tested_plan_and_its_rules(self):
        text = self._ladder_text()
        self.assertIn("babysit_classify.py retarget-plan", text)
        self.assertIn("merge-base --is-ancestor", text)
        self.assertIn("origin/<default>...HEAD", text)
        self.assertIn("never two-dot", text)
        self.assertIn("No cleverness on overlap", text)
        self.assertIn("gh pr edit <pr> --base <default>", text)
        self.assertIn("No merge, no push, no conflict resolution", text)
        self.assertIn("couldn't find remote ref", text)

    def test_the_merge_step_runs_the_guard_and_never_strips_by_hand(self):
        text = self._ladder_text()
        self.assertIn("babysit_merge_guard.py --worktree", text)
        self.assertIn("do NOT strip markers by hand", text)
        self.assertNotRegex(text, r"union-strip the markers\*\* .* from every conflicted file")

    def test_hard_validate_typechecks_touched_typescript(self):
        self.assertIn("npx tsc --noEmit -p tsconfig.json", self._ladder_text())

    def test_the_actions_union_documents_retarget_as_its_own_outcome(self):
        with open(RUNBOOK) as fh:
            text = fh.read()
        m = re.search(r"`actions\[\]`.*?\{type:\s*([a-z_|]+),", text, re.S)
        self.assertIsNotNone(m, "could not find the `actions[]` type union")
        self.assertIn("retarget", set(m.group(1).split("|")))


if __name__ == "__main__":
    unittest.main()
