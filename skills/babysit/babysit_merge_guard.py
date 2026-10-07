#!/usr/bin/env python3
"""Shape-gated union-strip for babysit's `rebase` step.

`commands/babysit-prs.md` used to tell the sweep, unconditionally, to
"union-strip the markers from every conflicted file", and justified it with
the ADD/ADD case (router include, `__init__.py` import, model registration).
Nothing between the justification and the operation restricted it to the case
it was justified by, so union-strip was applied to conflict shapes it is
simply wrong for.

WHAT UNION-STRIP ACTUALLY IS. Deleting the three markers keeps BOTH sides of
every hunk. That is correct only when both sides ADDED whole lines and neither
changed anything the other relied on -- two router includes, two imports. For
any other shape it emits both sides' code in sequence, which is not a merge of
two intents but a concatenation of them.

THE MEASURED CASE (a service repo, 2026-08): one branch re-indented a
~180-line span to wrap it in a reserve->commit try; the other edited inside
that span. After the reindent, the two blocks' leading whitespace was
byte-identical, git anchored them together, and union-strip kept both -- two
`await client.create_envelope_with_file(...)` call sites for one request, and
two calls to a billable e-signature API (two real envelopes minted).
Reproduced from scratch days later in a delimiter-BALANCED form, where the
emitted file parses clean and the `ast.parse` backstop never fires.

WHY PROSE COULD NOT FIX THIS. The gate that was supposed to stop a sibling
defect (a per-PR attempt cap) also lived in prose, and also went unread for
four days. Union-strip is a mechanical operation on a mechanical input; it
belongs in a tested script, which is what the rest of the babysit pipeline
already does with classification and planning.

TWO INDEPENDENT LAYERS, because either alone has a known hole:

  PRECONDITIONS decide whether a conflict may be union-stripped at all. They
  read stages :1/:2/:3 (base/ours/theirs) rather than parsing the markers, so
  "did this side only ADD lines?" is an exact question about the merge base
  instead of a guess about text between markers.

  PER-CONFLICT-HUNK, NOT PER-FILE. The question above used to be
  asked once for the WHOLE file: `base.splitlines()` vs `ours.splitlines()`.
  A `replace` anywhere in the file -- including a span BOTH sides left alone,
  already resolved cleanly by git, never touched by union-strip -- answered
  it "no" and refused the entire merge. Preconditions now re-derive git's OWN
  diff3 merge of the same three stage blobs (`_diff3_merge`) and judge each
  conflict hunk it draws on its own base/ours/theirs spans (`_check_region`),
  reusing `union_strip`'s marker walker (`_iter_conflict_regions`) rather than
  a second, Python-side guess at where a hunk starts and ends.

  POST-CONDITIONS re-read the emitted file and reject semantic damage the
  preconditions could have let through. Two measured merges produced shapes
  that `ast.parse` accepts happily: a duplicated keyword argument (a call-time
  TypeError, not a syntax error) and a name bound twice where last-wins
  silently DELETES a validation guard. Post-conditions compare against BOTH
  parents and flag only what is new in the merge, so a pattern already present
  in either parent is never blamed on the strip.

GENERATED FILES bypass both layers: a path in GENERATED_FILES is
resolved by rerunning its generator over the merged tree, after every other
conflict has been stripped, and is never union-stripped. The registry ships
EMPTY; see its comment for how to register a file.

Exit codes: 0 = stripped/regenerated, safe. 2 = refused, caller must `git merge --abort`
and flag NEEDS_HUMAN. 1 = usage/internal error.
"""
import argparse
import ast
import os
import subprocess
import sys
import tempfile

# Lines that may legitimately appear TWICE after a union-strip because running
# them twice is a no-op. This is the ADD/ADD case the rule exists for, and it
# is an allowlist rather than a denylist on purpose: a call this file has never
# heard of is assumed to have side effects, because the failure it is guarding
# against is a duplicated billable API call.
IDEMPOTENT_CALL_MARKERS = (
    "include_router(",        # FastAPI router registration
    "add_middleware(",        # FastAPI/Starlette middleware registration
    "register_blueprint(",    # Flask
    "add_url_rule(",          # Flask
    "target_metadata",        # alembic env.py
    "Base.metadata",          # SQLAlchemy model registration
    "__all__",                # export list
)

# Files a repo GENERATES, keyed by path -> generator argv (run in the
# worktree; "{python}" is this interpreter). A conflict in one of these is
# never a shape question: a typical generated inventory records the commit it
# was built from and renumbers rows, so every regeneration on main conflicts
# with every PR that regenerated it too, and union-strip would refuse every
# time (measured: a dozen PRs in a row stuck on one generated doc). The
# resolution is the generator's own output over the merged tree. An entry
# only applies when the generator script (argv[1], relative to the worktree
# root) exists in the worktree, so an unrelated repo with a same-named path
# still goes through the shape gate.
#
# Ships EMPTY. To register a file, add one entry per generated path:
#
#     GENERATED_FILES = {
#         "path/in/repo": ["{python}", "scripts/generator.py"],
#     }
#
# The generator must overwrite the path in place, exit 0, and leave no
# conflict markers behind; anything else refuses the merge (exit 2).
GENERATED_FILES = {}
GENERATOR_TIMEOUT_S = 300


def _run(args, cwd, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p


def conflicted_paths(wt):
    """Paths git reports as unmerged, de-duplicated across their stage rows."""
    # -z, because with core.quotePath on (the default) git C-quotes any path
    # holding a non-ASCII byte, a quote or a backslash -- measured:
    # `café/b.py` comes back as `"caf\303\251/b.py"`, while a plain space is
    # NOT quoted. The quoted form does not resolve as `:2:<path>`, so every
    # stage lookup would miss and the file would be refused as an add/delete
    # conflict: a safe verdict reached for an entirely wrong reason, which is
    # the kind of thing nobody debugs.
    out = _run(["git", "ls-files", "-u", "-z"], wt).stdout
    seen = []
    for rec in out.split("\0"):
        if "\t" not in rec:
            continue
        path = rec.split("\t", 1)[1]
        if path not in seen:
            seen.append(path)
    return seen


def stage(wt, num, path):
    """Content of one merge stage: 1=base, 2=ours, 3=theirs. None if absent.

    A missing stage is meaningful, not an error: stage 1 is absent for an
    add/add conflict (the file exists on neither side's ancestor).
    """
    p = _run(["git", "show", ":%d:%s" % (num, path)], wt, check=False)
    return p.stdout if p.returncode == 0 else None


def _generator(wt, path):
    """argv that regenerates `path` in `wt`, or None if it is not generated
    here (unregistered, or the registered generator script is absent)."""
    argv = GENERATED_FILES.get(path)
    if not argv or not os.path.isfile(os.path.join(wt, argv[1])):
        return None
    return [sys.executable if a == "{python}" else a for a in argv]


def regenerate(wt, path, argv):
    """Resolve a generated file by rerunning its generator over the merged
    tree. Returns a refusal reason, or None once the path is staged."""
    # Either side works as a starting point -- the generator overwrites the
    # file -- but the index must hold a resolved blob before `git add`.
    _run(["git", "checkout", "--theirs", "--", path], wt, check=False)
    try:
        # Bounded: a hung generator would otherwise wedge the whole sweep.
        p = subprocess.run(argv, cwd=wt, capture_output=True, text=True,
                           timeout=GENERATOR_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "%s: generator %s timed out after %ds" % (
            path, " ".join(argv[1:]), GENERATOR_TIMEOUT_S)
    if p.returncode != 0:
        return "%s: generator %s exited %d: %s" % (
            path, " ".join(argv[1:]), p.returncode,
            (p.stderr or p.stdout).strip()[-500:])
    with open(os.path.join(wt, path), encoding="utf-8",
              errors="surrogateescape") as fh:
        if any(_is_marker(line) for line in fh.read().splitlines(True)):
            return "%s: generator output still holds conflict markers" % path
    _run(["git", "add", "--", path], wt)
    return None


def _only_insertions(base_lines, side_lines):
    """Did `side` reach its content from `base` by ADDING whole lines only?

    The exact question union-strip's ADD/ADD justification depends on. Any
    delete or replace means this side changed something the other side may
    have been relying on, so keeping both sides is no longer a merge.

    Returns (inserted_lines, verdict). `verdict` is None when the side is a
    pure addition, "reindent" when a changed span is byte-different but
    IDENTICAL under whitespace normalisation, else the raw opcode. The
    reindent case gets its own name because it is the measured defect and
    "REPLACES lines" would send a human looking for a rewrite that isn't
    there -- the span is unchanged, it just moved one level in.
    """
    import difflib
    sm = difflib.SequenceMatcher(a=base_lines, b=side_lines, autojunk=False)
    inserted = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            inserted.extend(side_lines[j1:j2])
            continue
        if tag == "replace" and _is_reindent(base_lines[i1:i2],
                                             side_lines[j1:j2]):
            return None, "reindent"
        return None, tag          # 'delete' or a real 'replace' -> not ADD/ADD
    return inserted, None


def _whitespace_only(a_lines, b_lines):
    """Do these differ ONLY in leading whitespace? (the `git diff -w` test)

    The reindent-vs-edit signature. Cheap, and it catches the measured case
    exactly: a side that re-indented a span has identical content under
    whitespace normalisation but is byte-different, which is what lets git
    anchor it against an unrelated block at the same new indent level.
    """
    if a_lines == b_lines:
        return False
    return [ln.strip() for ln in a_lines] == [ln.strip() for ln in b_lines]


def _is_reindent(base_span, side_span):
    """Did this span keep all of base's content and only move it in/out?

    The measured case's signature, and it is NOT simply "identical under `diff -w`":
    that branch re-indented a ~180-line span *in order to wrap it* in a
    reserve->commit try, so the span also gained `try:` / `finally:` lines. A
    plain whitespace-equality test misses exactly the real case and reports
    the generic "REPLACES lines", which sends a human hunting for a rewrite
    that was never made.

    The precise question is: is every base line still present, in order, with
    its content untouched -- and did any of them move? If so the side did not
    rewrite this span, it re-indented it, and the danger is that git will
    anchor the moved block against an unrelated block now sharing its indent.
    """
    b = [ln.strip() for ln in base_span if ln.strip()]
    t = [ln.strip() for ln in side_span if ln.strip()]
    if not b or len(t) <= len(b):
        return False
    it = iter(t)
    if not all(line in it for line in b):     # base content preserved, in order
        return False
    base_indents = [len(ln) - len(ln.lstrip()) for ln in base_span if ln.strip()]
    side_by_content = {}
    for ln in side_span:
        if ln.strip():
            side_by_content.setdefault(ln.strip(), len(ln) - len(ln.lstrip()))
    moved = any(side_by_content.get(ln.strip()) not in (None, ind)
                for ln, ind in zip([x for x in base_span if x.strip()], base_indents))
    return moved


def _offending_call(line):
    """The non-idempotent call on this line, or None.

    Deliberately crude: anything with a `(` that is not an import, not a
    decorator, and not on the allowlist counts. A duplicated DECLARATION is
    the justified case; a duplicated CALL is the failure mode, and this errs
    toward refusing.
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith(("import ", "from ", "@")):
        return None
    if "(" not in s:
        return None
    if any(m in s for m in IDEMPOTENT_CALL_MARKERS):
        return None
    return s


def _diff3_merge(wt, base, ours, theirs):
    """Re-derive git's OWN 3-way merge of these blobs, diff3 markers forced
    on -- the hunk boundaries `check_preconditions` judges against.

    Not the working-tree file: `merge.conflictStyle` is a per-user git
    config (see `union_strip`'s docstring), so a plain `merge`-style
    checkout has no `|||||||` base section to read hunks from. Re-running
    `git merge-file --diff3` on the three STAGE blobs -- the same content
    `stage()` already reads for these checks -- makes the base section
    unconditionally available and lets git's own xdiff merge algorithm draw
    the hunk boundaries. That is the ratified design: extend the existing
    marker parser, do not stand up a second, Python-side reconstruction of
    "the conflict region" via difflib correlation -- that would invent a
    second definition of the conflict region inside a safety gate.

    `-p` prints the result to stdout instead of overwriting the "current"
    file, so this never touches the real worktree or index.
    """
    with tempfile.TemporaryDirectory() as td:
        def write(name, content):
            p = os.path.join(td, name)
            with open(p, "w", encoding="utf-8", errors="surrogateescape",
                      newline="") as fh:
                fh.write(content)
            return p
        ours_f = write("ours", ours)
        base_f = write("base", base)
        theirs_f = write("theirs", theirs)
        p = _run(["git", "merge-file", "--diff3", "-p",
                  ours_f, base_f, theirs_f], wt, check=False)
        # Per `git help merge-file`: exit value is the CONFLICT COUNT
        # (0 = merged clean, truncated to 127 if there are more), and
        # NEGATIVE on error -- e.g. binary content. A negative C return
        # surfaces to Python as the wrapped unsigned byte (measured:
        # binary content -> 255), so 0-127 is success regardless of how
        # many conflicts, and only 128-255 is a real error the caller
        # cannot derive hunks from and must not guess at.
        if not (0 <= p.returncode <= 127):
            raise RuntimeError("git merge-file failed (rc=%d): %s"
                               % (p.returncode, p.stderr.strip()))
        return p.stdout


def _check_region(path, label, base_span, ours_span, theirs_span):
    """Preconditions for ONE conflict hunk -- the region-scoped form of the
    whole-file checks `check_preconditions` used to run. Same
    logic, one span at a time instead of one whole file at a time.
    """
    reasons = []

    # Reindent first: it is a strict subset of "not ADD/ADD", but naming it
    # explicitly is the difference between a human reading "shape mismatch"
    # and reading the actual defect.
    for name, side in (("ours", ours_span), ("theirs", theirs_span)):
        if _whitespace_only(base_span, side):
            reasons.append(
                "%s: %s -- %s is a pure RE-INDENT of the base (identical "
                "under `diff -w`) -- the measured reindent shape: git anchors the "
                "reindented block against an unrelated block at the same "
                "new indent and union-strip then keeps both"
                % (path, label, name))
    if _whitespace_only(ours_span, theirs_span):
        reasons.append(
            "%s: %s -- the two sides differ ONLY in leading whitespace -- "
            "a pure re-indentation collision, not two additions"
            % (path, label))
    if reasons:
        return reasons

    inserted = []
    for name, side in (("ours", ours_span), ("theirs", theirs_span)):
        ins, bad = _only_insertions(base_span, side)
        if bad == "reindent":
            return ["%s: %s -- %s RE-INDENTS a span (byte-different, "
                    "identical under `diff -w`) -- the measured reindent shape. git "
                    "anchors the reindented block against a block at the "
                    "same new indent level and union-strip then keeps "
                    "BOTH, duplicating whatever executable code the span "
                    "contained" % (path, label, name)]
        if bad is not None:
            return ["%s: %s -- %s %sS lines relative to the merge base, "
                    "so this is not ADD/ADD -- union-strip's justification "
                    "does not hold and it would emit two contradictory "
                    "versions" % (path, label, name, bad.upper())]
        inserted.extend(ins)

    for line in inserted:
        call = _offending_call(line)
        if call is not None:
            return ["%s: %s -- an added line contains a call that is not "
                    "a known idempotent registration -- duplicating it is "
                    "the failure mode (the measured case called a billable API twice): "
                    "%s" % (path, label, call.strip()[:120])]
    return reasons


def check_preconditions(wt, path):
    """Reasons this file must NOT be union-stripped. Empty list == permitted.

    Judges ONLY the conflict hunks git itself drew: a `replace`
    or a reindent in a span BOTH sides left untouched -- already resolved
    cleanly by git, and never something union-strip will duplicate -- must
    not refuse a merge over content the strip never even reaches. Refusal is
    still all-or-nothing FOR THIS PATH: the first hunk that fails wins, same
    as the whole-file version this replaces (it also returned on the first
    bad opcode); `main`'s all-or-nothing ACROSS the conflict SET is unchanged.
    """
    ours, theirs = stage(wt, 2, path), stage(wt, 3, path)
    if ours is None or theirs is None:
        return ["%s: add/delete conflict (a side is missing entirely) -- "
                "union-strip cannot express 'keep both' here" % path]

    base = stage(wt, 1, path)
    if base is None:
        # Both sides created this file. There is no common ancestor -- no
        # base blob to diff3-merge against -- so every line is an
        # "addition" on both sides and a hunk-level ADD/ADD test would
        # vacuously pass while union-strip duplicated the entire file. This
        # branch was already correctly scoped to "the whole new file" (there
        # is no narrower unit to judge it against); nothing to rescope.
        if ours != theirs:
            return ["%s: add/add of a NEW file with differing content -- "
                    "union-strip would duplicate the whole file" % path]
        return []

    try:
        merged = _diff3_merge(wt, base, ours, theirs)
        regions = _conflict_regions(merged)
    except RuntimeError as e:
        return ["%s: could not derive conflict regions via git merge-file: "
                "%s" % (path, e)]

    if not regions:
        # git reports this path unmerged, so a hunk exists in the index. A
        # re-derivation that finds none means the two disagree, and permitting
        # here would strip a file no check ever judged. Refuse on doubt.
        return ["%s: git reports this path unmerged, but the diff3 "
                "re-derivation of its stage blobs found no conflict hunk -- "
                "the two disagree, so no hunk was judged" % path]

    total = len(regions)
    for idx, (b_span, o_span, t_span, start, end) in enumerate(regions, 1):
        label = ("hunk %d/%d @ diff3 lines %d-%d (git merge-file "
                 "re-derivation, not the worktree file)"
                 % (idx, total, start, end))
        why = _check_region(path, label, b_span, o_span, t_span)
        if why:
            return why
    return []


# ---------------------------------------------------------------------------
# post-conditions -- semantic damage `ast.parse` accepts
# ---------------------------------------------------------------------------
def _dup_kwargs(tree):
    """{call-ish label: kwarg} for every repeated keyword in one call.

    `ast.parse('f(a=1, a=2)')` does NOT raise -- Python rejects a repeated
    keyword at CALL time, so a syntax-only gate is structurally blind to it.
    Measured live on a real merge: union-strip emitted `plan_summary=` twice
    in one call and hard-validate passed the file.
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        seen = set()
        for kw in node.keywords:
            if kw.arg is None:
                continue
            if kw.arg in seen:
                out.add((getattr(node.func, "attr", None)
                         or getattr(node.func, "id", "call"), kw.arg))
            seen.add(kw.arg)
    return out


def _dup_dict_keys(tree):
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen = set()
        for k in node.keys:
            if not isinstance(k, ast.Constant):
                continue
            if k.value in seen:
                out.add(repr(k.value))
            seen.add(k.value)
    return out


def _shadowed_bindings(tree):
    """Names bound twice in one statement list with no read in between.

    The measured shape. HEAD coerced `book_title`; the base coerced AND
    length-checked it. Union-strip emitted both assignments, last-wins, and
    the bounded-reject guard became dead code -- valid Python, no syntactic
    trace, a validation rule deleted by a merge tool. This is strictly worse
    than a duplicated call: it leaves LESS behaviour than either parent had.

    Every statement list is scanned independently -- a function body, an `if`
    body, an `else`, a `finally`. Independently, because a name bound once in
    each arm of an `if` is ordinary code, not a clobbered binding; and each
    list, because scanning only top-level bodies would miss a double-bind
    inside a conditional, which is where merged code most often lands.
    """
    out = set()

    def scan(body, scope):
        pending = set()
        for stmt in body:
            # A read of a pending name clears it: `x = f(); use(x); x = g()`
            # is reassignment, not a clobbered binding.
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    pending.discard(sub.id)
            targets = []
            if isinstance(stmt, ast.Assign):
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                if isinstance(stmt.target, ast.Name):
                    targets = [stmt.target.id]
            for name in targets:
                if name in pending:
                    out.add("%s.%s" % (scope, name))
                pending.add(name)

    def walk(node, scope):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                scan(block, scope)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                walk(child, "%s.%s" % (scope, child.name))
            else:
                walk(child, scope)

    walk(tree, "<module>")
    return out


GUARD_NODES = (ast.If, ast.Try, ast.Assert, ast.Raise, ast.While)


def _guard_count(tree):
    return sum(1 for n in ast.walk(tree) if isinstance(n, GUARD_NODES))


def check_postconditions(path, merged, ours, theirs):
    """Reasons the union-stripped output must not ship.

    Every check compares the merge against BOTH parents and reports only what
    is NEW in the merge. A duplicate kwarg that one parent already had is that
    parent's bug, not something union-strip introduced, and blaming the strip
    for it would make this gate cry wolf until someone disabled it.
    """
    if not path.endswith(".py"):
        return []
    # ValueError as well as SyntaxError: files are read with
    # errors="surrogateescape" (so an undecodable byte survives round-trip
    # rather than exploding), and a lone surrogate reaching ast.parse raises
    # UnicodeEncodeError -- a ValueError subclass, NOT a SyntaxError.
    # Verified on 3.14: ast.parse('x = "\udcff"') -> UnicodeEncodeError.
    # Uncaught, that crashes the guard instead of refusing the merge, which
    # turns "this file is undecodable" into "the gate is down".
    try:
        m = ast.parse(merged)
    except (SyntaxError, ValueError) as e:
        return ["%s: union-stripped output does not parse: %s: %s"
                % (path, type(e).__name__, e)]
    try:
        o, t = ast.parse(ours), ast.parse(theirs)
    except (SyntaxError, ValueError):
        # A parent that does not parse is not this merge's problem, and we
        # cannot difference against it. Refuse rather than guess.
        return ["%s: a parent revision does not parse -- cannot establish "
                "what this merge introduced" % path]

    reasons = []
    new_kw = _dup_kwargs(m) - _dup_kwargs(o) - _dup_kwargs(t)
    for func, arg in sorted(new_kw):
        reasons.append("%s: merge introduced a DUPLICATE KEYWORD ARGUMENT "
                       "`%s=` in a call to `%s` -- ast.parse accepts this; "
                       "Python raises TypeError at call time"
                       % (path, arg, func))

    new_keys = _dup_dict_keys(m) - _dup_dict_keys(o) - _dup_dict_keys(t)
    for k in sorted(new_keys):
        reasons.append("%s: merge introduced a duplicate dict key %s -- "
                       "last-wins, silently" % (path, k))

    new_binds = _shadowed_bindings(m) - _shadowed_bindings(o) - _shadowed_bindings(t)
    for b in sorted(new_binds):
        reasons.append("%s: merge bound `%s` twice with no read in between -- "
                       "last-wins, so one parent's version of that value "
                       "(and any guard on it) is now dead code"
                       % (path, b))

    gm, go, gt = _guard_count(m), _guard_count(o), _guard_count(t)
    if gm < max(go, gt):
        reasons.append("%s: merge has FEWER guards/branches (%d) than a parent "
                       "(ours %d, theirs %d) -- a merge of two branches must "
                       "never emit less checking than either had"
                       % (path, gm, go, gt))
    return reasons


def _is_marker(line):
    """Is this line one of git's three conflict markers -- and only that?

    Matched EXACTLY, not by prefix. `=======` is also an rst/markdown section
    underline and a common comment rule, and a prefix test would silently
    delete those lines out of any conflicted doc it touched. git writes the
    separator bare and the other two as 7 chars + space + label (or bare at
    EOF), so the exact forms are cheap to state and there is no reason to
    accept anything looser.
    """
    ln = line.rstrip("\r\n")
    if ln == "=======":
        return True
    for m in ("<<<<<<<", ">>>>>>>", "|||||||"):
        if ln == m or ln.startswith(m + " "):
            return True
    return False


def _iter_conflict_regions(text):
    """Walk `text` (diff3-marker conflict output), yielding text/conflict
    segments in file order. The one marker state machine shared by
    `union_strip` (flattens each conflict to ours+theirs) and
    `_conflict_regions` (needs each conflict's own base/ours/
    theirs spans so preconditions judge only what actually conflicted,
    instead of the whole file). Extending the existing parser rather than
    writing a second one, per the ratified design for that change.

    REGION-SCOPED, not a whole-file filter. `=======` under a 7-character
    title is a byte-identical rst/markdown section underline, and a bare
    filter would delete it anywhere in the file -- the exact damage
    _is_marker's exact-match was introduced to prevent, arriving one layer
    up. check_postconditions cannot catch it either: it returns early for
    non-.py paths, which is precisely where underlines live. So the
    separator and the base marker are only meaningful once `<<<<<<<` has
    opened a region.

    Yields ("text", lines) for ordinary (non-conflict) content, and
    ("conflict", base_lines, ours_lines, theirs_lines, start_line, end_line)
    for each conflict hunk -- 1-indexed, inclusive, spanning the `<<<<<<<`
    through `>>>>>>>` lines. `base_lines` is `[]` when the diff3 base
    section (`|||||||`..`=======`) never opened -- any style but diff3/
    zdiff3, and also the ADD/ADD shape even under diff3, whose base section
    is empty by construction.
    """
    out_text = []
    side = None                    # None | "ours" | "base" | "theirs"
    base = ours = theirs = None
    start_line = None
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        bare = line.rstrip("\r\n")
        if side is None:
            if bare == "<<<<<<<" or bare.startswith("<<<<<<< "):
                if out_text:
                    yield ("text", out_text)
                    out_text = []
                side, base, ours, theirs = "ours", [], [], []
                start_line = lineno
                continue
            out_text.append(line)     # ordinary content: `=======` included
            continue
        if bare == "|||||||" or bare.startswith("||||||| "):
            side = "base"
            continue
        if bare == "=======":
            side = "theirs"
            continue
        if bare == ">>>>>>>" or bare.startswith(">>>>>>> "):
            yield ("conflict", base, ours, theirs, start_line, lineno)
            side = None
            continue
        if side == "base":
            base.append(line)
        elif side == "ours":
            ours.append(line)
        else:
            theirs.append(line)
    if side is not None:
        # Buffered conflict lines are yielded only on `>>>>>>>`. Falling off
        # the end with a region still open would silently DROP every line
        # after the opener -- the union_strip this replaced streamed every
        # non-base line straight into its output as it scanned, so it could
        # never lose a tail this way; buffering per-region content (needed
        # so preconditions can see each region's own spans)
        # traded that safety away unless guarded explicitly here. Raise
        # rather than silently emit a truncated file (a code-review finding).
        raise RuntimeError(
            "unterminated conflict region opened at line %d (no matching "
            "`>>>>>>>`)" % start_line)
    if out_text:
        yield ("text", out_text)


def _conflict_regions(text):
    """The (base, ours, theirs, start_line, end_line) tuples for each
    conflict hunk in `text`, in file order. `text` must carry diff3 markers
    (see `_diff3_merge`) so `base` is populated even when the repo's own
    `merge.conflictStyle` has no base section to read.
    """
    return [seg[1:] for seg in _iter_conflict_regions(text) if seg[0] == "conflict"]


def union_strip(text):
    """Drop conflict markers, keeping both sides -- the operation the rebase
    step names.

    `merge.conflictStyle` is a PER-USER git config, so this cannot assume the
    default `merge` style. Under `diff3`/`zdiff3` git emits a third section
    between `|||||||` and `=======` holding the MERGE BASE's version of the
    region. Those lines belong to neither side: removing only the marker line
    and keeping what follows emits base + ours + theirs, duplicating the
    ancestor's content -- precisely the class of damage this guard exists to
    prevent, and a duplicated plain statement is one of the few shapes the
    post-conditions do NOT catch.

    So the base section is dropped as a section, not just its marker. Under
    the default style there is no such section and this is a plain filter.
    """
    out = []
    for seg in _iter_conflict_regions(text):
        if seg[0] == "text":
            out.extend(seg[1])
        else:
            _, base, ours, theirs, _start, _end = seg
            out.extend(ours)
            out.extend(theirs)
    return "".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--worktree", default=".", help="the conflicted worktree")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the verdict without writing files")
    a = ap.parse_args(argv)
    wt = os.path.abspath(a.worktree)

    try:
        paths = conflicted_paths(wt)
    except RuntimeError as e:
        print("merge-guard: %s" % e, file=sys.stderr)
        return 1
    if not paths:
        print("merge-guard: no conflicted files")
        return 0

    refusals, staged, generated = [], [], []
    for path in paths:
        argv = _generator(wt, path)
        if argv:
            generated.append((path, argv))
            continue
        why = check_preconditions(wt, path)
        if why:
            refusals.extend(why)
            continue
        full = os.path.join(wt, path)
        # newline="" on BOTH handles: the default translates CRLF->LF on
        # read and LF->os.linesep on write, so a safe merge could silently
        # rewrite every line ending in the file. Union-strip must be additive
        # in bytes, not just in statements.
        with open(full, encoding="utf-8", errors="surrogateescape",
                  newline="") as fh:
            text = fh.read()
        try:
            merged = union_strip(text)
        except RuntimeError as e:
            # check_preconditions already judged this path SAFE from its own
            # (always well-formed) diff3 re-derivation -- if the actual
            # on-disk file is malformed anyway, refuse rather than let an
            # uncaught exception take down the whole batch mid-loop.
            refusals.append("%s: %s" % (path, e))
            continue
        why = check_postconditions(path, merged, stage(wt, 2, path) or "",
                                   stage(wt, 3, path) or "")
        if why:
            refusals.extend(why)
            continue
        staged.append((full, merged))

    # ALL-OR-NOTHING. A merge is one unit: strip half the files and the commit
    # is a mixture of resolved and marker-laden ones. Refusing the whole merge
    # is also what the rebase step already required, so this only makes it
    # mechanical.
    if refusals:
        print("REFUSED: union-strip is not valid for this conflict")
        for r in refusals:
            print("  - %s" % r)
        print("\n-> git merge --abort, then NEEDS_HUMAN with the reasons above.")
        return 2

    stripped = [p for p in paths if p not in dict(generated)]
    if a.dry_run:
        for path, _argv in generated:
            print("would regenerate %s" % path)
    else:
        for full, merged in staged:
            with open(full, "w", encoding="utf-8", errors="surrogateescape",
                      newline="") as fh:
                fh.write(merged)
        # STAGE what we wrote. Writing the worktree alone leaves the index
        # holding the unmerged stages, so the caller's `git commit --no-edit`
        # fails with "you have unmerged files" -- the resolution looks done
        # on disk and is not done to git. Staging here rather than in prose
        # for this ticket's whole reason: a step the executor must remember
        # is a step that eventually goes unremembered.
        if stripped:
            _run(["git", "add", "--"] + stripped, wt)
        # Generators run LAST, over a tree whose other conflicts are already
        # resolved -- a generator typically scans the very files the strip
        # just wrote.
        for path, argv in generated:
            why = regenerate(wt, path, argv)
            if why:
                refusals.append(why)
        if refusals:
            print("REFUSED: could not regenerate a generated file")
            for r in refusals:
                print("  - %s" % r)
            print("\n-> git merge --abort, then NEEDS_HUMAN with the reasons above.")
            return 2
    if stripped:
        print("OK: union-strip valid for %d file(s): %s"
              % (len(staged), ", ".join(stripped)))
    if generated:
        print("OK: regenerated %d generated file(s): %s"
              % (len(generated), ", ".join(p for p, _ in generated)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
