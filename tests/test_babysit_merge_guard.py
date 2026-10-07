#!/usr/bin/env python3
"""Regression tests for skills/babysit/babysit_merge_guard.py.

Every test builds a REAL git repo and a REAL conflicted merge, then asks the
guard for a verdict. Nothing is simulated: the fixtures produce the same
`git ls-files -u` stages the live rebase step reads, so a test that passes
here is a statement about what git actually does, not about what we think it
does. That matters because this whole ticket exists because a rule was written
against an imagined conflict shape (ADD/ADD) and applied to real ones.

Written as unittest.TestCase so it runs under both pytest and unittest, like
the rest of the babysit suite.
"""
import ast
import os
import subprocess
import sys
import tempfile
import unittest

from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "babysit")
sys.path.insert(0, SKILL_DIR)

import babysit_merge_guard as g  # noqa: E402
from babysit_merge_guard import (  # noqa: E402
    _is_marker,
    check_postconditions,
    check_preconditions,
    conflicted_paths,
    main as guard_main,
    union_strip,
)


def git(repo, *args):
    p = subprocess.run(["git"] + list(args), cwd=repo, capture_output=True, text=True)
    return p


class ConflictRepo:
    """A repo with one real conflicted merge in progress.

    `base` -> `ours` on the default branch, `theirs` on a side branch, then
    `git merge`. The guard is then pointed at the resulting index.
    """

    def __init__(self, tmp, path, base, ours, theirs, conflict_style="merge"):
        self.repo = tempfile.mkdtemp(dir=tmp)
        self.path = path
        git(self.repo, "init", "-q", "-b", "main", ".")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        # Pin the conflict FORMAT like the rest of the babysit suite pins every
        # environment input: a runner with `merge.conflictStyle=diff3` (or
        # zdiff3) emits an extra `|||||||` base section, which changes both the
        # marker set and the exact line counts these fixtures assert on. The
        # diff3 shape gets its own explicit fixture below rather than arriving
        # by accident on one developer's machine.
        git(self.repo, "config", "merge.conflictStyle", conflict_style)
        # hooks/branch-name-gate.sh (if installed) requires a ticket token in
        # new branch names; it must not apply to a throwaway fixture whose
        # branches are never pushed. LINEAR_SKIP=1 is its documented bypass.
        os.environ.setdefault("LINEAR_SKIP", "1")
        self._write(base)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "tag", "b0")
        self._write(ours)
        git(self.repo, "commit", "-qam", "ours")
        git(self.repo, "checkout", "-q", "-b", "side", "b0")
        self._write(theirs)
        git(self.repo, "commit", "-qam", "theirs")
        git(self.repo, "checkout", "-q", "main")
        self.merge = git(self.repo, "merge", "side", "--no-edit")

    def _write(self, text):
        full = os.path.join(self.repo, self.path)
        os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(full) else None
        with open(full, "w") as fh:
            fh.write(text)

    def read(self):
        with open(os.path.join(self.repo, self.path)) as fh:
            return fh.read()


class PreconditionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mergeguard-")

    # -- the case the rule EXISTS for; the fix must not disarm it -----------
    def test_add_add_router_includes_still_union_strips_and_merges(self):
        """Acceptance: 'An ADD/ADD fixture (two router includes) still
        union-strips and still merges -- the fix must not disarm the case the
        rule exists for.' A shape gate that refuses everything would 'fix' the
        ticket by making every conflict a NEEDS_HUMAN, which is a regression
        wearing a fix's clothes."""
        r = ConflictRepo(
            self.tmp, "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(users)\n",
            "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(users)\napp.include_router(billing)\n",
            "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(users)\napp.include_router(reports)\n")
        self.assertEqual(conflicted_paths(r.repo), ["main.py"])
        self.assertEqual(check_preconditions(r.repo, "main.py"), [],
                         "two router includes are the justified ADD/ADD case")
        self.assertEqual(guard_main(["--worktree", r.repo]), 0)
        merged = r.read()
        self.assertIn("include_router(billing)", merged)
        self.assertIn("include_router(reports)", merged)
        self.assertNotIn("<<<<<<<", merged, "markers must be gone")
        # The merge must actually be COMMITTABLE. Writing the worktree alone
        # leaves the index holding the unmerged stages, so `git commit` fails
        # with "you have unmerged files" -- resolved on disk, unresolved to
        # git. Step 4 of the rebase rule commits straight after this, so a
        # guard that does not stage produces a merge nobody can finish.
        self.assertEqual(conflicted_paths(r.repo), [],
                         "the guard must leave NO unmerged stages behind")
        c = git(r.repo, "commit", "--no-edit", "-q")
        self.assertEqual(c.returncode, 0,
                         "git commit must succeed after a permitted strip: %s"
                         % (c.stderr or c.stdout))

    def test_add_add_imports_still_union_strips(self):
        """The other named ADD/ADD case: two `__init__.py` imports."""
        r = ConflictRepo(
            self.tmp, "pkg.py",
            "from .a import A\n",
            "from .a import A\nfrom .b import B\n",
            "from .a import A\nfrom .c import C\n")
        self.assertEqual(check_preconditions(r.repo, "pkg.py"), [])

    # -- the measured defect ------------------------------------------------
    def test_reindent_vs_edit_is_refused_and_named(self):
        """Acceptance: 'A fixture reproducing the measured shape (one side
        re-indents a span containing a call, the other edits inside it) is
        planned as NEEDS_HUMAN, NOT union-stripped.'

        One branch re-indents the `_client()`-through-return span to wrap it
        in a try/finally; the other edits inside that span. Union-strip keeps
        both and emits two `create_envelope_with_file` call sites for one
        request -- on the real PR, two calls to a billable e-signature API."""
        r = ConflictRepo(
            self.tmp, "svc.py",
            "async def send(req):\n"
            "    client = _client()\n"
            "    created = await client.create_envelope_with_file(req.file)\n"
            "    return created\n",
            "async def send(req):\n"
            "    try:\n"
            "        client = _client()\n"
            "        created = await client.create_envelope_with_file(req.file)\n"
            "        return created\n"
            "    finally:\n"
            "        _release()\n",
            "async def send(req):\n"
            "    client = _client()\n"
            "    if req.fields is None:\n"
            "        created = await client.create_envelope_with_file(req.file)\n"
            "    return created\n")
        reasons = check_preconditions(r.repo, "svc.py")
        self.assertTrue(reasons, "the measured reindent shape must be refused")
        self.assertIn("RE-INDENT", reasons[0],
                      "the reason must NAME the shape -- a generic 'not "
                      "ADD/ADD' sends a human hunting for a rewrite that was "
                      "never made. Got: %s" % reasons[0])
        self.assertEqual(guard_main(["--worktree", r.repo, "--dry-run"]), 2,
                         "exit 2 == caller aborts the merge")

    def test_the_refused_strip_would_have_duplicated_the_billable_call(self):
        """Acceptance: 'Assert on DUPLICATE CALL-SITE COUNT, not on parse
        failure -- parse failure is the incidental symptom here, not the
        defect.'

        This is the mutation check: it proves the fixture really does encode
        the defect, so a build that union-stripped it anyway would redden.
        Note the stripped output PARSES CLEAN -- the rebase step's `ast.parse`
        backstop would NOT have caught this shape."""
        r = ConflictRepo(
            self.tmp, "svc.py",
            "async def send(req):\n"
            "    client = _client()\n"
            "    created = await client.create_envelope_with_file(req.file)\n"
            "    return created\n",
            "async def send(req):\n"
            "    try:\n"
            "        client = _client()\n"
            "        created = await client.create_envelope_with_file(req.file)\n"
            "        return created\n"
            "    finally:\n"
            "        _release()\n",
            "async def send(req):\n"
            "    client = _client()\n"
            "    if req.fields is None:\n"
            "        created = await client.create_envelope_with_file(req.file)\n"
            "    return created\n")
        stripped = union_strip(r.read())
        self.assertEqual(stripped.count("create_envelope_with_file"), 2,
                         "the unguarded rule emits TWO call sites for one request")
        ast.parse(stripped)   # must NOT raise -- that is the whole point

    def test_a_non_idempotent_call_added_by_both_sides_is_refused(self):
        """Pure ADD/ADD is necessary but not sufficient. Both sides adding a
        line is the justified shape ONLY when running it twice is a no-op. A
        duplicated registration is fine; a duplicated charge is the failure
        mode, and nothing about the diff shape distinguishes them."""
        r = ConflictRepo(
            self.tmp, "bill.py",
            "def run(o):\n    pass\n",
            "def run(o):\n    charge_customer(o, 500)\n    pass\n",
            "def run(o):\n    charge_customer(o, 500)\n    log(o)\n    pass\n")
        reasons = check_preconditions(r.repo, "bill.py")
        self.assertTrue(reasons, "a duplicated non-idempotent call must refuse")
        self.assertIn("charge_customer", reasons[0])

    def test_add_add_of_a_new_file_with_differing_content_is_refused(self):
        """No merge base means every line is an 'addition' on both sides, so
        the ADD/ADD test passes vacuously while union-strip duplicates the
        ENTIRE file. Refused explicitly rather than left to the shape test."""
        repo = tempfile.mkdtemp(dir=self.tmp)
        git(repo, "init", "-q", "-b", "main", ".")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        open(os.path.join(repo, "seed"), "w").write("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "seed")
        git(repo, "tag", "b0")
        open(os.path.join(repo, "new.py"), "w").write("def f():\n    return 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "ours")
        git(repo, "checkout", "-q", "-b", "side", "b0")
        open(os.path.join(repo, "new.py"), "w").write("def f():\n    return 2\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "theirs")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "side", "--no-edit")
        reasons = check_preconditions(repo, "new.py")
        self.assertTrue(reasons)
        self.assertIn("whole file", reasons[0])

    def test_refusal_is_all_or_nothing_across_the_conflict_set(self):
        """A merge is ONE unit. Stripping the safe files and leaving the
        unsafe one would commit a mixture of resolved and marker-laden files.
        The rebase step already required aborting the whole merge; this makes
        it mechanical rather than a thing the executor must remember."""
        r = ConflictRepo(
            self.tmp, "ok.py",
            "from .a import A\n", "from .a import A\nfrom .b import B\n",
            "from .a import A\nfrom .c import C\n")
        # add a SECOND conflicted file that is unsafe, in the same merge
        git(r.repo, "merge", "--abort")
        open(os.path.join(r.repo, "bad.py"), "w").write("def f():\n    charge(1)\n")
        git(r.repo, "add", "-A")
        git(r.repo, "commit", "-qm", "bad-base")
        open(os.path.join(r.repo, "bad.py"), "w").write("def f():\n    charge(1)\n    charge(2)\n")
        git(r.repo, "commit", "-qam", "bad-ours")
        git(r.repo, "checkout", "-q", "-b", "side2", "HEAD~1")
        open(os.path.join(r.repo, "bad.py"), "w").write("def f():\n    charge(1)\n    charge(3)\n")
        # ok.py must conflict AGAIN in this same merge. Without it the
        # conflict set holds one file and the test proves only "a refused
        # file is not written" -- not the thing its name claims, which is
        # that a SAFE file in the same set is left alone too. Code review
        # caught that the first version was strictly weaker than its own
        # docstring.
        open(os.path.join(r.repo, "ok.py"), "w").write(
            "from .a import A\nfrom .b import B\nfrom .c import C\n")
        git(r.repo, "commit", "-qam", "bad-theirs")
        git(r.repo, "checkout", "-q", "main")
        open(os.path.join(r.repo, "ok.py"), "w").write(
            "from .a import A\nfrom .b import B\nfrom .d import D\n")
        git(r.repo, "commit", "-qam", "ok-ours-2")
        git(r.repo, "merge", "side2", "--no-edit")
        self.assertEqual(sorted(conflicted_paths(r.repo)), ["bad.py", "ok.py"],
                         "the set must hold one safe AND one unsafe file")
        before = open(os.path.join(r.repo, "bad.py")).read()
        ok_before = open(os.path.join(r.repo, "ok.py")).read()
        self.assertEqual(guard_main(["--worktree", r.repo]), 2)
        self.assertEqual(open(os.path.join(r.repo, "bad.py")).read(), before,
                         "a refused run must write NOTHING")
        self.assertEqual(open(os.path.join(r.repo, "ok.py")).read(), ok_before,
                         "the SAFE file in the same conflict set must ALSO stay "
                         "unwritten -- a half-stripped merge commits a mixture "
                         "of resolved and marker-laden files")


class MarkerAndPathTests(unittest.TestCase):
    """Defects found deep-reviewing this file's own first draft."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mergeguard-mp-")

    def test_a_markdown_underline_is_not_treated_as_a_conflict_marker(self):
        """`=======` is git's separator AND an rst/markdown section underline
        AND a common comment rule. The first draft matched markers by PREFIX,
        so union-stripping any conflicted doc would have silently deleted its
        underlines -- a content change nobody asked for, inside the one
        operation that is supposed to be purely additive."""
        self.assertTrue(_is_marker("=======\n"))
        self.assertFalse(_is_marker("========\n"), "8 chars is an underline")
        self.assertFalse(_is_marker("== Heading ==\n"))
        self.assertTrue(_is_marker("<<<<<<< HEAD\n"))
        self.assertTrue(_is_marker(">>>>>>> origin/main\n"))
        self.assertFalse(_is_marker("<<<<<<<<<<<< shifted\n"))
        # This assertion originally read the other way -- it asserted the
        # underline WAS stripped, encoding the bug as the spec. Code review
        # caught it. `_is_marker` answers "does this line look like a marker", which
        # is not the same question as "is this line acting as one": outside an
        # open conflict region a 7-char `=======` is an rst/markdown section
        # underline, and union_strip is region-scoped precisely so it survives.
        doc = "Title\n=======\nprose\n"
        self.assertEqual(union_strip(doc), doc,
                         "an underline OUTSIDE a conflict region must survive "
                         "untouched -- union-strip is additive, not a filter")
        conflicted = "A\n<<<<<<< HEAD\nX\n=======\nY\n>>>>>>> s\nZ\n"
        self.assertEqual(union_strip(conflicted), "A\nX\nY\nZ\n",
                         "the same bytes INSIDE a region are the separator")

    def test_crlf_line_endings_survive_a_safe_strip(self):
        """Python's default newline=None translates CRLF->LF on read and
        LF->os.linesep on write, so a SAFE merge could silently rewrite every
        line ending in the file. Union-strip has to be additive in bytes, not
        just in statements."""
        r = ConflictRepo(
            self.tmp, "m.py",
            "from . import routers\r\napp.include_router(routers.a)\r\n",
            "from . import routers\r\napp.include_router(routers.a)\r\napp.include_router(routers.b)\r\n",
            "from . import routers\r\napp.include_router(routers.a)\r\napp.include_router(routers.c)\r\n")
        self.assertEqual(guard_main(["--worktree", r.repo]), 0)
        with open(os.path.join(r.repo, "m.py"), newline="") as fh:
            out = fh.read()
        self.assertIn("routers.b", out)
        self.assertIn("routers.c", out)
        self.assertEqual(out.count("\r\n"), out.count("\n"),
                         "every newline must still be CRLF -- a safe strip "
                         "must not rewrite line endings. Got: %r" % out)

    def test_a_non_ascii_path_resolves_its_merge_stages(self):
        """With core.quotePath on (the default) `git ls-files -u` C-quotes any
        path holding a non-ASCII byte: measured, `café/b.py` comes back as
        `"caf\\303\\251/b.py"`. That form does not resolve as `:2:<path>`, so
        without -z every stage lookup missed and the file was refused as an
        'add/delete conflict' -- a safe verdict reached for an entirely wrong
        reason, which is the kind of thing nobody debugs.

        (A plain space is NOT quoted -- checked, because the first version of
        this test assumed it was and therefore proved nothing.)"""
        r = ConflictRepo(
            self.tmp, "caf\u00e9/routes.py",
            "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(a)\n",
            "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(a)\napp.include_router(b)\n",
            "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(a)\napp.include_router(c)\n")
        self.assertEqual(conflicted_paths(r.repo), ["caf\u00e9/routes.py"],
                         "the path must come back UNQUOTED and usable")
        self.assertEqual(check_preconditions(r.repo, "caf\u00e9/routes.py"), [],
                         "a non-ASCII path must reach the SAME verdict as any "
                         "other ADD/ADD, not a spurious add/delete refusal")


    def test_diff3_style_does_not_leak_the_base_section_into_the_output(self):
        """`merge.conflictStyle` is PER-USER git config, so the guard cannot
        assume the default. Under diff3/zdiff3 git emits a third section
        between `|||||||` and `=======` holding the MERGE BASE's version of
        the region -- lines belonging to NEITHER side. Deleting only the
        marker line and keeping what follows emits base + ours + theirs, and
        a duplicated plain statement is one of the few shapes the
        post-conditions do NOT catch.

        Asserted on `union_strip` directly, and honestly: the only conflict
        shape that produces a NON-EMPTY base section is replace/replace
        (measured below -- an ADD/ADD conflict's base section is empty), and
        preconditions already refuse replace/replace. So this is
        defence-in-depth for a path currently closed upstream, not a live
        leak. It is kept because the two layers are independent by design:
        the whole architecture of this guard is that neither layer may assume
        the other is intact, which is the lesson of three measured merges
        that aborted only because an unrelated file happened not to parse."""
        # Build the shape by hand, from git's real diff3 output (verified
        # 2026-08-24 against `merge.conflictStyle=diff3`).
        raw = ("A\n"
               "<<<<<<< HEAD\n"
               "OURS1\n"
               "||||||| cb8e017\n"
               "OLD1\n"
               "OLD2\n"
               "=======\n"
               "THEIRS1\n"
               ">>>>>>> s\n"
               "Z\n")
        out = union_strip(raw)
        self.assertNotIn("OLD1", out,
                         "the merge BASE's content belongs to neither side and "
                         "must not survive the strip. Got:\n%s" % out)
        self.assertNotIn("OLD2", out)
        self.assertNotIn("|||||||", out)
        self.assertEqual(out, "A\nOURS1\nTHEIRS1\nZ\n")

    def test_an_add_add_conflict_under_diff3_still_strips_correctly(self):
        """The reachable half: an ADD/ADD conflict's base section is empty, so
        diff3 changes only the marker set. The permitted case must survive a
        developer whose git is configured that way -- which is why
        ConflictRepo pins the style rather than inheriting it."""
        base = "from x import a\napp.include_router(a)\napp.include_router(z)\n"
        r = ConflictRepo(
            self.tmp, "m.py", base,
            base.replace("app.include_router(z)\n",
                         "app.include_router(b)\napp.include_router(z)\n"),
            base.replace("app.include_router(z)\n",
                         "app.include_router(c)\napp.include_router(z)\n"),
            conflict_style="diff3")
        self.assertIn("<<<<<<<", r.read(), "fixture must actually conflict")
        self.assertEqual(check_preconditions(r.repo, "m.py"), [],
                         "ADD/ADD is permitted regardless of conflict style")
        self.assertEqual(guard_main(["--worktree", r.repo]), 0)
        out = r.read()
        for line in ("from x import a", "app.include_router(a)",
                     "app.include_router(z)"):
            self.assertEqual(out.count(line), 1, "base line %r duplicated" % line)
        self.assertIn("app.include_router(b)", out)
        self.assertIn("app.include_router(c)", out)

    def test_an_unterminated_conflict_region_raises_instead_of_dropping_content(self):
        """Code-review finding on the region-scoping change's own first
        draft: `_iter_conflict_regions` BUFFERS each region's lines and yields them only on the
        closing `>>>>>>>`. The whole-file `union_strip` this replaced had no
        such buffer -- it streamed every non-base line straight into its
        output as it scanned, so it could never lose a tail. A malformed or
        truncated conflict (no closing marker) must raise, not silently emit
        a file missing everything after the opener."""
        text = "A\n<<<<<<< HEAD\nX\nY\n=======\nZ\n"   # no >>>>>>> ever appears
        with self.assertRaises(RuntimeError):
            union_strip(text)

    def test_main_refuses_cleanly_on_an_unterminated_region_on_disk(self):
        """The same defect exercised through `main()`'s actual call path:
        an uncaught exception from `union_strip` must not crash the whole
        batch mid-loop -- it must become a refusal like any other unsafe
        verdict, so a genuinely malformed on-disk file (however it got that
        way) still exits 2 with REFUSED, not a raw traceback."""
        r = ConflictRepo(
            self.tmp, "m.py",
            "from .a import A\n", "from .a import A\nfrom .b import B\n",
            "from .a import A\nfrom .c import C\n")
        # check_preconditions judges its OWN diff3 re-derivation (always
        # well-formed) and will say this is safe ADD/ADD -- then main()
        # reads the REAL on-disk file, which we corrupt here by deleting its
        # closing marker, to prove the two are independent and the second
        # stage is still guarded.
        mangled = r.read().replace(">>>>>>> side\n", "")
        self.assertNotIn(">>>>>>>", mangled, "sanity: marker actually removed")
        with open(os.path.join(r.repo, "m.py"), "w") as fh:
            fh.write(mangled)
        rc = guard_main(["--worktree", r.repo, "--dry-run"])
        self.assertEqual(rc, 2, "a malformed on-disk conflict must refuse, "
                                "not crash")


class RegionScopedPreconditionTests(unittest.TestCase):
    """Preconditions used to read `base.splitlines()` vs
    `ours.splitlines()` for the WHOLE FILE, so a `replace` -- or a reindent
    -- in a span BOTH sides left alone (already resolved cleanly by git,
    never touched by union-strip) refused the entire merge, including a
    conflict region that is legitimately ADD/ADD. All four pre-existing
    refusal fixtures above are hunk == whole-file by construction (2-7 line
    fixtures), so none of them exercise this: these are the regression tests
    that do. Preconditions now re-derive git's OWN diff3 merge of the same
    stage blobs (`_diff3_merge`) and judge each conflict hunk on its own
    spans (`_check_region`), via the marker walker `union_strip` already
    used (`_iter_conflict_regions`) -- not a second, Python-side guess at
    hunk boundaries.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mergeguard-region-")

    # -- fixture 1: the measured clean-replace shape ------------------------
    def test_a_replace_in_a_cleanly_merged_region_no_longer_refuses_the_add_add(self):
        """The measured shape: one branch ADDS a module-level helper constant
        in conflict with main's own addition at the SAME anchor (a real,
        marked ADD/ADD conflict), AND separately REPLACES a call site
        elsewhere in the same file (`old_transform` -> `new_transform`,
        cleanly auto-merged by git -- theirs never touched that line, so
        there is no marker there at all). The whole-file version of this
        check read `ours REPLACES lines relative to the merge base` from
        that untouched, already-resolved call site and refused the entire
        file -- including the safe ADD/ADD conflict it never even reached.
        Expected: stripped, both helpers present, the replaced call site
        intact."""
        r = ConflictRepo(
            self.tmp, "mod.py",
            "def process(x):\n"
            "    result = old_transform(x)\n"
            "    return result\n",
            'HELPER_B = "b-value"\n'
            "\n"
            "\n"
            "def process(x):\n"
            "    result = new_transform(x)\n"
            "    return result\n",
            'HELPER_C = "c-value"\n'
            "\n"
            "\n"
            "def process(x):\n"
            "    result = old_transform(x)\n"
            "    return result\n")
        self.assertIn("<<<<<<<", r.read(), "fixture must actually conflict")
        reasons = check_preconditions(r.repo, "mod.py")
        self.assertEqual(reasons, [],
                         "a replace OUTSIDE the conflict region must not "
                         "refuse a safe ADD/ADD conflict. Got: %s" % reasons)
        self.assertEqual(guard_main(["--worktree", r.repo]), 0)
        merged = r.read()
        self.assertIn('HELPER_B = "b-value"', merged)
        self.assertIn('HELPER_C = "c-value"', merged)
        self.assertIn("new_transform(x)", merged,
                      "the clean replace outside the conflict must survive")
        self.assertNotIn("<<<<<<<", merged)
        c = git(r.repo, "commit", "--no-edit", "-q")
        self.assertEqual(c.returncode, 0, c.stderr or c.stdout)

    # -- fixture 2: counter-fixture -- a replace INSIDE the region ----------
    def test_a_replace_inside_the_conflict_region_still_refuses(self):
        """Same file shape as above, but this time the two sides make
        DIFFERING edits to the SAME base line (a real replace/replace
        conflict, not an addition) -- the rescoping must not have widened
        what counts as safe. Region-scoped or whole-file, this must still
        refuse."""
        r = ConflictRepo(
            self.tmp, "mod.py",
            "def unrelated():\n"
            "    return 0\n"
            "\n"
            "\n"
            "def process(x):\n"
            "    result = transform(x)\n"
            "    return result\n",
            "def unrelated():\n"
            "    return 0\n"
            "\n"
            "\n"
            "def process(x):\n"
            "    result = transform_v2(x)\n"
            "    return result\n",
            "def unrelated():\n"
            "    return 0\n"
            "\n"
            "\n"
            "def process(x):\n"
            "    result = transform_v3(x)\n"
            "    return result\n")
        reasons = check_preconditions(r.repo, "mod.py")
        self.assertTrue(reasons, "a replace INSIDE the conflict must still refuse")
        self.assertIn("REPLACES", reasons[0])
        self.assertEqual(guard_main(["--worktree", r.repo, "--dry-run"]), 2)

    # -- fixture 3: multi-region, names the failing region -------------------
    def test_a_multi_region_file_names_the_failing_region(self):
        """Two separate, independently-conflicting hunks in one file: hunk 1
        is a clean ADD/ADD (two router includes -- safe in isolation); hunk 2
        both edits a guard's message AND drops a second guard entirely (a
        real replace, unsafe). Refusal is still all-or-nothing for this path
        -- the whole file is refused -- but the message must NAME hunk 2, not
        make a generic whole-file claim that would send a human hunting
        through the safe hunk too."""
        r = ConflictRepo(
            self.tmp, "mod.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "app.include_router(users)\n"
            "\n"
            "\n"
            "def guarded(x):\n"
            "    if x is None:\n"
            "        raise ValueError()\n"
            "    if x < 0:\n"
            "        raise ValueError()\n"
            "    return x\n",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "app.include_router(users)\n"
            "app.include_router(billing)\n"
            "\n"
            "\n"
            "def guarded(x):\n"
            "    if x is None:\n"
            "        raise ValueError()\n"
            "    if x < 0:\n"
            "        raise ValueError('negative')\n"
            "    return x\n",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "app.include_router(users)\n"
            "app.include_router(reports)\n"
            "\n"
            "\n"
            "def guarded(x):\n"
            "    if x is None:\n"
            "        raise ValueError()\n"
            "    return x\n")
        reasons = check_preconditions(r.repo, "mod.py")
        self.assertTrue(reasons, "hunk 2's unsafe replace must refuse the file")
        self.assertIn("hunk 2/2", reasons[0],
                      "the message must name the FAILING region, not claim "
                      "the whole file is the problem. Got: %s" % reasons[0])
        self.assertNotIn("hunk 1", reasons[0],
                         "hunk 1 was the safe ADD/ADD -- it must not be "
                         "named as part of the refusal")
        self.assertEqual(guard_main(["--worktree", r.repo, "--dry-run"]), 2)

    # -- fixture 4: _whitespace_only scoped to the region -------------------
    def test_a_reindent_outside_the_conflict_region_does_not_drive_the_refusal(self):
        """The `_whitespace_only` treatment gets the same rescoping as
        `_only_insertions`. Here `ours` re-indents an unrelated function's
        body (an `if` block moved from 8 to 12 spaces, content unchanged) --
        `theirs` never touches that function, so git auto-merges the
        reindent cleanly with no conflict marker there at all. Elsewhere,
        the two sides ALSO have a genuine, marked ADD/ADD conflict (two
        router includes). Whole-file scoping compared the ENTIRE base
        against the ENTIRE ours/theirs for reindent/whitespace-only shape and
        (via the same opcode walk fixture 1 exercises) refused the file over
        a reindent the actual conflict never contained. Region-scoping must
        judge only the marked hunk and permit this."""
        r = ConflictRepo(
            self.tmp, "mod.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "app.include_router(users)\n"
            "\n"
            "\n"
            "def unrelated(x):\n"
            "    if x:\n"
            "        y = x + 1\n"
            "        z = y * 2\n"
            "        return z\n"
            "    return 0\n",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "app.include_router(users)\n"
            "app.include_router(billing)\n"
            "\n"
            "\n"
            "def unrelated(x):\n"
            "    if x:\n"
            "            y = x + 1\n"
            "            z = y * 2\n"
            "            return z\n"
            "    return 0\n",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "app.include_router(users)\n"
            "app.include_router(reports)\n"
            "\n"
            "\n"
            "def unrelated(x):\n"
            "    if x:\n"
            "        y = x + 1\n"
            "        z = y * 2\n"
            "        return z\n"
            "    return 0\n")
        raw = r.read()
        region = raw[raw.index("<<<<<<<"):raw.index(">>>>>>>")]
        self.assertNotIn("if x:", region,
                         "sanity: the reindent must be auto-merged, not "
                         "part of any marked conflict. Got:\n%s" % region)
        reasons = check_preconditions(r.repo, "mod.py")
        self.assertEqual(reasons, [],
                         "a reindent OUTSIDE the conflict region must not "
                         "drive the refusal decision. Got: %s" % reasons)
        self.assertEqual(guard_main(["--worktree", r.repo]), 0)
        merged = r.read()
        self.assertIn("app.include_router(billing)", merged)
        self.assertIn("app.include_router(reports)", merged)


class PostconditionTests(unittest.TestCase):
    """Post-conditions: semantic damage `ast.parse` accepts.

    Acceptance: 'a duplicate-kwarg strip and a duplicate-assignment strip must
    each be rejected by hard-validate ON THEIR OWN, without relying on a
    sibling file's parse failure to trigger the abort.' All three measured
    instances aborted only because an unrelated file in the same conflict set
    failed to parse -- three times the gate never recognised the defect.
    """

    def test_duplicate_keyword_argument_is_rejected_though_it_parses(self):
        """`ast.parse('f(a=1, a=2)')` does NOT raise: Python rejects a repeated
        keyword at CALL time, so a syntax-only gate is structurally blind.
        Measured on a real merge -- union-strip emitted `plan_summary=` twice
        in one call and the file passed hard-validate."""
        ours = "def go(x):\n    return build(plan_summary=x)\n"
        theirs = "def go(x):\n    return build(plan_summary=f'{x}!')\n"
        merged = "def go(x):\n    return build(plan_summary=x, plan_summary=f'{x}!')\n"
        ast.parse(merged)          # proves the syntax gate is blind to it
        reasons = check_postconditions("chat_tools.py", merged, ours, theirs)
        self.assertTrue(reasons, "a duplicate kwarg must be rejected")
        self.assertIn("DUPLICATE KEYWORD ARGUMENT", reasons[0])
        self.assertIn("plan_summary", reasons[0])

    def test_a_binding_clobbered_by_the_merge_is_rejected(self):
        """The strictly worse shape: the strip emits LESS behaviour
        than either parent had. HEAD coerced `book_title`; base coerced AND
        length-checked it. Both assignments survive, last-wins, and the
        bounded-reject guard becomes dead code -- valid Python, no syntactic
        trace, a validation rule deleted by a merge tool."""
        ours = ("def go(raw):\n"
                "    book_title = raw if isinstance(raw, str) else ''\n"
                "    return book_title\n")
        theirs = ("def go(raw):\n"
                  "    book_title = raw[:_MAX_TITLE_LEN] if isinstance(raw, str) else ''\n"
                  "    return book_title\n")
        merged = ("def go(raw):\n"
                  "    book_title = raw if isinstance(raw, str) else ''\n"
                  "    book_title = raw[:_MAX_TITLE_LEN] if isinstance(raw, str) else ''\n"
                  "    return book_title\n")
        ast.parse(merged)
        reasons = check_postconditions("chat_tools.py", merged, ours, theirs)
        self.assertTrue(reasons, "a clobbered binding must be rejected")
        self.assertTrue(any("book_title" in r for r in reasons), reasons)

    def test_a_binding_clobbered_INSIDE_a_conditional_is_still_caught(self):
        """The first draft only scanned top-level bodies, so a double-bind
        inside an `if` -- which is where merged code most often lands, since
        that is what both sides were editing -- went undetected. Each
        statement list is now scanned independently."""
        ours = ("def go(raw):\n"
                "    if raw:\n"
                "        title = raw.strip()\n"
                "        return title\n")
        theirs = ("def go(raw):\n"
                  "    if raw:\n"
                  "        title = raw.strip()[:MAX]\n"
                  "        return title\n")
        merged = ("def go(raw):\n"
                  "    if raw:\n"
                  "        title = raw.strip()\n"
                  "        title = raw.strip()[:MAX]\n"
                  "        return title\n")
        ast.parse(merged)
        reasons = check_postconditions("x.py", merged, ours, theirs)
        self.assertTrue(any("title" in r for r in reasons),
                        "a clobbered binding in a nested block must be caught too: %s"
                        % reasons)

    def test_the_same_name_bound_once_in_each_arm_is_NOT_flagged(self):
        """The counterpart false-positive guard: `if/else` each binding the
        same name is completely ordinary. Scanning arms independently is what
        makes the check above safe to turn on."""
        code = ("def go(x):\n"
                "    if x:\n        v = 1\n"
                "    else:\n        v = 2\n"
                "    return v\n")
        self.assertEqual(check_postconditions("x.py", code, code, code), [])

    def test_a_pattern_ALREADY_in_a_parent_is_not_blamed_on_the_merge(self):
        """The false-positive guard, and the reason every check differences
        against BOTH parents. Ordinary reassignment is everywhere in real
        code; a gate that flagged it would cry wolf until someone disabled it,
        which is a worse outcome than the bug."""
        parent = ("def go(raw):\n"
                  "    t = raw\n"
                  "    t = t.strip()\n"
                  "    return t\n")
        reasons = check_postconditions("x.py", parent, parent, parent)
        self.assertEqual(reasons, [],
                         "a shape present in the parents is the parents' "
                         "business, not something the strip introduced")

    def test_a_merge_with_fewer_guards_than_a_parent_is_rejected(self):
        """The generalising invariant: a merge of two branches must never
        emit fewer guards/branches than either parent had. A net reduction in
        check count is the signature of the validation-deleting shape and
        catches cases the two specific checks above do not enumerate."""
        ours = ("def go(x):\n"
                "    if x is None:\n        raise ValueError()\n"
                "    if x > 10:\n        raise ValueError()\n"
                "    return x\n")
        theirs = ("def go(x):\n"
                  "    if x is None:\n        raise ValueError()\n"
                  "    return x\n")
        merged = "def go(x):\n    return x\n"
        reasons = check_postconditions("x.py", merged, ours, theirs)
        self.assertTrue(any("FEWER guards" in r for r in reasons), reasons)

    def test_an_undecodable_byte_refuses_instead_of_crashing_the_guard(self):
        """Files are read with errors="surrogateescape" so an undecodable byte
        survives round-trip rather than exploding on read -- but a lone
        surrogate reaching `ast.parse` raises UnicodeEncodeError, a ValueError
        subclass and NOT a SyntaxError. Verified on 3.14. Uncaught it crashes
        the guard mid-merge, which turns "this file is undecodable" into "the
        gate is down" -- the one outcome a safety gate must never have."""
        merged = 'x = "\udcff"\ny = 1\n'
        reasons = check_postconditions("x.py", merged, "y = 1\n", "y = 1\n")
        self.assertTrue(reasons, "an unparseable merge must be REFUSED")
        self.assertIn("does not parse", reasons[0])

    def test_a_non_python_file_is_not_semantically_judged(self):
        """Post-conditions are Python-shaped. A .toml/.txt conflict gets the
        shape preconditions only -- the file-TYPE axis is a separate concern,
        kept deliberately apart so neither change half-implements the other."""
        self.assertEqual(check_postconditions("pyproject.toml", "a\na\n", "a\n", "a\n"), [])


DOC = "docs/inventory.md"
GEN_SCRIPT = "scripts/gen_inventory.py"
# A tiny fake generator, written into each fixture repo. Like a real generated
# inventory it stamps HEAD and numbers its rows, which is exactly what makes
# every regeneration conflict. GEN_FAIL in the env makes it exit non-zero.
GEN = """import os, subprocess, sys
if os.environ.get("GEN_FAIL"):
    sys.exit("generator boom")
sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
rows = ["| %d | `%s` |" % (i, n) for i, n in enumerate(sorted(os.listdir("models")), 1)]
open("docs/inventory.md", "w").write("Generated at commit `%s`\\n\\n" % sha + "\\n".join(rows) + "\\n")
"""


class GeneratedFileRegistryDefaultTests(unittest.TestCase):
    def test_the_registry_ships_empty(self):
        """The public default registers nothing: every conflicted path goes
        through the shape gate until a repo opts a generated file in."""
        self.assertEqual(g.GENERATED_FILES, {})


class GeneratedFileTests(unittest.TestCase):
    """A registered generated file is regenerated, not stripped.

    GENERATED_FILES ships empty, so every test here registers the fixture's
    fake generator for the duration of the test only.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mergeguard-gen-")
        os.environ.setdefault("LINEAR_SKIP", "1")
        os.environ.pop("GEN_FAIL", None)
        patcher = mock.patch.dict(g.GENERATED_FILES,
                                  {DOC: ["{python}", GEN_SCRIPT]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _repo(self, with_generator=True, base_extra=None, ours_extra=None, theirs_extra=None):
        repo = tempfile.mkdtemp(dir=self.tmp)
        git(repo, "init", "-q", "-b", "main", ".")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        git(repo, "config", "merge.conflictStyle", "merge")

        def write(path, text):
            full = os.path.join(repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(text)

        def commit(msg):
            if with_generator:
                subprocess.run([sys.executable, GEN_SCRIPT], cwd=repo, check=True)
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", msg)

        write(GEN_SCRIPT, GEN)
        write("models/base.py", "x = 1\n")
        write(DOC, "stale\n")
        for path, text in (base_extra or {}).items():
            write(path, text)
        if not with_generator:
            write(DOC, "| 1 | `base.py` |\n")
        commit("base")
        git(repo, "tag", "b0")
        write("models/a.py", "a = 1\n")
        if not with_generator:
            write(DOC, "| 1 | `a.py` |\n| 2 | `base.py` |\n")
        for path, text in (ours_extra or {}).items():
            write(path, text)
        commit("ours")
        git(repo, "checkout", "-q", "-b", "side", "b0")
        write("models/b.py", "b = 1\n")
        if not with_generator:
            write(DOC, "| 1 | `b.py` |\n| 2 | `base.py` |\n")
        for path, text in (theirs_extra or {}).items():
            write(path, text)
        commit("theirs")
        git(repo, "checkout", "-q", "main")
        if not with_generator:
            os.remove(os.path.join(repo, GEN_SCRIPT))
            git(repo, "commit", "-qam", "drop generator")
        git(repo, "merge", "side", "--no-edit")
        return repo

    def _doc(self, repo):
        with open(os.path.join(repo, DOC)) as fh:
            return fh.read()

    def test_conflicted_generated_file_is_regenerated_and_committable(self):
        repo = self._repo()
        self.assertEqual(conflicted_paths(repo), [DOC])
        self.assertEqual(guard_main(["--worktree", repo]), 0)
        doc = self._doc(repo)
        self.assertIn("| 1 | `a.py` |\n| 2 | `b.py` |\n| 3 | `base.py` |", doc,
                      "both sides' rows, renumbered by the generator")
        self.assertNotIn("<<<<<<<", doc)
        self.assertEqual(conflicted_paths(repo), [])
        self.assertEqual(git(repo, "commit", "--no-edit", "-q").returncode, 0)

    def test_regeneration_runs_after_the_strip_over_the_merged_tree(self):
        """An ADD/ADD code conflict alongside the doc: both resolve, and the
        generator sees the stripped file (it scans the tree)."""
        base_main = "app.include_router(users)\n"
        repo = self._repo(
            base_extra={"main.py": base_main},
            ours_extra={"main.py": base_main + "app.include_router(billing)\n"},
            theirs_extra={"main.py": base_main + "app.include_router(reports)\n"})
        self.assertEqual(sorted(conflicted_paths(repo)), sorted([DOC, "main.py"]))
        self.assertEqual(guard_main(["--worktree", repo]), 0)
        self.assertEqual(conflicted_paths(repo), [])
        self.assertEqual(git(repo, "commit", "--no-edit", "-q").returncode, 0)

    def test_refused_code_conflict_refuses_all_and_skips_generator(self):
        repo = self._repo(
            base_extra={"svc.py": "def f():\n    return charge(0)\n"},
            ours_extra={"svc.py": "def f():\n    return charge(1)\n"},
            theirs_extra={"svc.py": "def f():\n    return charge(2)\n"})
        self.assertIn("svc.py", conflicted_paths(repo))
        self.assertEqual(guard_main(["--worktree", repo]), 2)
        self.assertIn(DOC, conflicted_paths(repo), "generator must not have run")
        self.assertIn("<<<<<<<", self._doc(repo))

    def test_generator_failure_refuses(self):
        repo = self._repo()
        os.environ["GEN_FAIL"] = "1"
        try:
            self.assertEqual(guard_main(["--worktree", repo]), 2)
        finally:
            os.environ.pop("GEN_FAIL", None)

    def test_generator_timeout_refuses(self):
        repo = self._repo()
        with open(os.path.join(repo, GEN_SCRIPT), "w") as fh:
            fh.write("import time\ntime.sleep(30)\n")
        old, g.GENERATOR_TIMEOUT_S = g.GENERATOR_TIMEOUT_S, 1
        try:
            self.assertEqual(guard_main(["--worktree", repo]), 2)
        finally:
            g.GENERATOR_TIMEOUT_S = old

    def test_dry_run_does_not_regenerate(self):
        repo = self._repo()
        self.assertEqual(guard_main(["--worktree", repo, "--dry-run"]), 0)
        self.assertEqual(conflicted_paths(repo), [DOC])
        self.assertIn("<<<<<<<", self._doc(repo))

    def test_registered_path_without_its_generator_goes_through_the_shape_gate(self):
        repo = self._repo(with_generator=False)
        self.assertEqual(conflicted_paths(repo), [DOC])
        self.assertEqual(guard_main(["--worktree", repo]), 2,
                         "a replace-shaped hunk with no generator is still refused")

    def test_an_unregistered_generated_path_goes_through_the_shape_gate(self):
        """The shipped default: with the registry empty, the same conflicted
        doc -- generator script present -- is NOT regenerated; its
        replace-shaped hunk is judged by the shape gate and refused."""
        repo = self._repo()
        with mock.patch.dict(g.GENERATED_FILES, {}, clear=True):
            self.assertEqual(guard_main(["--worktree", repo]), 2)
        self.assertEqual(conflicted_paths(repo), [DOC])
        self.assertIn("<<<<<<<", self._doc(repo))


if __name__ == "__main__":
    unittest.main()
