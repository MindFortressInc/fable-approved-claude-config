#!/usr/bin/env python3
"""Regression tests for skills/bulldozer-reconcile/reconcile.py.

The root problem: a CR-deferred 1-off ticket filed under a board's standing
"Bulldozer 1-offs" epic can be fixed by a LATER, unrelated PR (often on the
very same parent PR that spawned it) with nothing ever closing the ticket.
`/bulldozer` otherwise rediscovers this manually, at the cost of a full
worker subagent per dead ticket. reconcile.py is the automated reconciler:
given a board's epic + a local repo clone, it best-effort-parses each open
child's recorded file:line + defect pattern from its prose, checks that
pattern against the repo's current default branch, and closes ONLY tickets
whose premise is confirmed gone (dry-run by default; --live required to
write).

Every test here uses a FAKE Linear client (FakeLinearClient below) and a REAL
throwaway git repo built in a tempdir — NO test in this file makes a network
call or writes to Linear. That is not incidental: the reconciler must never
write to Linear during tests.

  TestParseFinding           pure prose-parsing heuristics (no git, no I/O)
  TestCheckPremise           the git ancestry check against a real temp repo
  TestRunReconcile           end-to-end orchestration against FakeLinearClient
  TestRealTicketShapeFixture a real 1-off ticket's full description shape run
                             through the real parser + a repo reconstructed
                             to mirror hooks/pr-gate.sh before and after the
                             fixing PR merged.
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(os.path.dirname(HERE), "skills", "bulldozer-reconcile")
sys.path.insert(0, SKILL_DIR)

from reconcile import (  # noqa: E402
    Finding,
    Ticket,
    parse_finding,
    check_premise,
    run_reconcile,
    _assignee_eligible,
)

OWNER_EMAIL = "owner@example.com"


# --------------------------------------------------------------------------
# Fixture git repo helper
# --------------------------------------------------------------------------

def _run(repo, *args):
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _commit(repo, message, files):
    """Write `files` ({relpath: content}) and commit them."""
    for relpath, content in files.items():
        full = os.path.join(repo, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        _run(repo, "add", relpath)
    _run(repo, "commit", "-m", message, "--allow-empty")
    return _run(repo, "rev-parse", "HEAD").strip()


def make_repo():
    """A throwaway git repo with an isolated identity (never touches the
    real ~/.gitconfig) and a `main` branch. Returns the repo path."""
    tmp = tempfile.mkdtemp(prefix="reconcile-repo-")
    _run(tmp, "init", "-q", "-b", "main")
    _run(tmp, "config", "user.email", "test@example.com")
    _run(tmp, "config", "user.name", "Test")
    _run(tmp, "config", "commit.gpgsign", "false")
    return tmp


# --------------------------------------------------------------------------
# parse_finding — pure prose parsing, no I/O
# --------------------------------------------------------------------------

class TestParseFinding(unittest.TestCase):
    def test_files_line_with_pattern_and_lines(self):
        desc = (
            "The bug: `pr-gate.sh`'s trigger match strips quoted spans "
            "(`sed -E \"s/'[^']*'//g; s/\\\"[^\\\"]*\\\"//g\"`) before checking "
            "the literal phrase `gh pr create`.\n\n"
            "**Files: **`hooks/pr-gate.sh` (trigger match, ~lines 15-21), "
            "`tests/test_pr_gate.py`."
        )
        finding = parse_finding("ENG-329", desc)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.file, "hooks/pr-gate.sh")
        self.assertEqual(finding.line_hint, "15-21")
        # the longest code-shaped span wins over the short `gh pr create` one
        self.assertIn("sed -E", finding.pattern)
        self.assertIn("s/'[^']*'//g", finding.pattern)

    def test_no_files_line_is_unparseable(self):
        desc = "Something is wrong somewhere. No file or pattern named."
        self.assertIsNone(parse_finding("ENG-1", desc))

    def test_files_line_with_no_code_span_is_unparseable(self):
        desc = "**Files:** `hooks/pr-gate.sh` — just a path, no pattern quoted anywhere else."
        # Only the file path itself is backtick-quoted; excluded as a bare
        # path, so no pattern candidate remains.
        self.assertIsNone(parse_finding("ENG-2", desc))

    def test_empty_description_is_unparseable(self):
        self.assertIsNone(parse_finding("ENG-3", ""))
        self.assertIsNone(parse_finding("ENG-3", None))

    def test_bold_files_colon_inside_bold(self):
        desc = (
            "boom: `if (x == 'y') { doThing(); }` is unguarded.\n\n"
            "**Files:**`app/handler.js` (~line 42)"
        )
        finding = parse_finding("ENG-4", desc)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.file, "app/handler.js")
        self.assertEqual(finding.line_hint, "42")
        self.assertIn("doThing()", finding.pattern)

    def test_multiple_files_picks_first_as_primary(self):
        desc = (
            "broken: `while (true) { spin(); }` loops forever.\n\n"
            "Files: `src/loop.py` (~lines 3-5), `tests/test_loop.py`."
        )
        finding = parse_finding("ENG-5", desc)
        self.assertEqual(finding.file, "src/loop.py")


# --------------------------------------------------------------------------
# check_premise — real git repo, no network
# --------------------------------------------------------------------------

class TestCheckPremise(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def test_still_present(self):
        _commit(self.repo, "add buggy file", {
            "hooks/pr-gate.sh": "cleaned=$(sed -E \"s/'[^']*'//g\" <<<\"$cmd\")\n",
        })
        finding = Finding("ENG-X", "hooks/pr-gate.sh", "1", "sed -E \"s/'[^']*'//g\"")
        v = check_premise(self.repo, "main", finding)
        self.assertEqual(v.status, "STILL_PRESENT")

    def test_confirmed_gone_with_fix_sha(self):
        _commit(self.repo, "add buggy file", {
            "hooks/pr-gate.sh": "cleaned=$(sed -E \"s/'[^']*'//g\" <<<\"$cmd\")\n",
        })
        fix_sha = _commit(self.repo, "rewrite trigger as a lexer (fixes ENG-X)", {
            "hooks/pr-gate.sh": "cleaned=$(lex_command \"$cmd\")\n",
        })
        finding = Finding("ENG-X", "hooks/pr-gate.sh", "1", "sed -E \"s/'[^']*'//g\"")
        v = check_premise(self.repo, "main", finding)
        self.assertEqual(v.status, "CONFIRMED_GONE")
        self.assertEqual(v.fix_sha, fix_sha)

    def test_fix_only_on_unmerged_branch_counts_as_still_outstanding(self):
        _commit(self.repo, "add buggy file", {
            "hooks/pr-gate.sh": "cleaned=$(sed -E \"s/'[^']*'//g\" <<<\"$cmd\")\n",
        })
        _run(self.repo, "checkout", "-q", "-b", "fix/eng-x")
        _commit(self.repo, "fix on a branch that never merges", {
            "hooks/pr-gate.sh": "cleaned=$(lex_command \"$cmd\")\n",
        })
        _run(self.repo, "checkout", "-q", "main")
        finding = Finding("ENG-X", "hooks/pr-gate.sh", "1", "sed -E \"s/'[^']*'//g\"")
        # Checking against `main` (the default branch) must NOT see the fix
        # that only exists on the unmerged branch.
        v = check_premise(self.repo, "main", finding)
        self.assertEqual(v.status, "STILL_PRESENT")

    def test_file_missing_is_ambiguous_not_confirmed(self):
        _commit(self.repo, "init", {"README.md": "hello\n"})
        finding = Finding("ENG-X", "hooks/does-not-exist.sh", None, "some pattern here")
        v = check_premise(self.repo, "main", finding)
        self.assertEqual(v.status, "AMBIGUOUS")

    def test_pattern_absent_with_no_history_is_ambiguous_not_confirmed(self):
        """The pattern is simply not in the file and never was — evidence
        cannot cite a fix commit, so this must NOT be auto-closed."""
        _commit(self.repo, "init", {"hooks/pr-gate.sh": "echo hello\n"})
        finding = Finding("ENG-X", "hooks/pr-gate.sh", None, "totally unrelated pattern xyz")
        v = check_premise(self.repo, "main", finding)
        self.assertEqual(v.status, "AMBIGUOUS")
        self.assertEqual(v.fix_sha, "")


# --------------------------------------------------------------------------
# run_reconcile — end-to-end against a FakeLinearClient (no network, ever)
# --------------------------------------------------------------------------

class FakeLinearClient:
    """In-memory stand-in for the real Linear GraphQL client. Records every
    write so tests can assert on them; never touches a network socket."""

    def __init__(self, tickets):
        self._tickets = tickets  # list[Ticket]
        self.closed = []          # [(issue_id, comment_body)]
        self.resolve_calls = []

    def resolve_epic(self, identifier):
        self.resolve_calls.append(identifier)
        return identifier  # identity: tests pass the "epic" straight through

    def list_open_children(self, epic_uuid):
        return list(self._tickets)

    def close_with_comment(self, issue_id, body):
        self.closed.append((issue_id, body))


PATTERN = "sed -E \"s/'[^']*'//g\""
DESC_TEMPLATE = (
    "The bug: (`{pattern}`) is naive.\n\n"
    "**Files:** `hooks/pr-gate.sh` (~lines 1-3)"
)


class TestRunReconcile(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def _mk_ticket(self, identifier, assignee_email=None):
        return Ticket(
            id=f"uuid-{identifier}",
            identifier=identifier,
            title=f"title for {identifier}",
            description=DESC_TEMPLATE.format(pattern=PATTERN),
            assignee_email=assignee_email,
        )

    def test_dry_run_never_writes_regardless_of_verdict(self):
        _commit(self.repo, "add buggy file", {"hooks/pr-gate.sh": PATTERN + "\n"})
        fix_sha = _commit(self.repo, "fix it", {"hooks/pr-gate.sh": "lex_command\n"})
        del fix_sha  # confirmed-gone case, still must not write in dry-run
        client = FakeLinearClient([self._mk_ticket("ENG-100")])
        results = run_reconcile(
            client, epic="EPIC-1", repo=self.repo, default_branch="main",
            owner_email=OWNER_EMAIL, live=False,
        )
        self.assertEqual(client.closed, [])
        self.assertEqual(results[0].verdict, "CONFIRMED_GONE")
        self.assertEqual(results[0].action, "none")

    def test_live_run_closes_only_confirmed_gone(self):
        _commit(self.repo, "add buggy file", {"hooks/pr-gate.sh": PATTERN + "\n"})
        fix_sha = _commit(self.repo, "fix it", {"hooks/pr-gate.sh": "lex_command\n"})

        still_broken_ticket = Ticket(
            id="uuid-ENG-200", identifier="ENG-200", title="still broken",
            description=DESC_TEMPLATE.format(pattern=PATTERN) + "\nSTILL BROKEN MARKER",
            assignee_email=None,
        )
        # Make a genuinely still-present case by pointing at a DIFFERENT repo
        # state: reuse the same repo/pattern but assert on a ticket whose
        # pattern truly remains — simplest is a second file that was never
        # touched.
        _commit(self.repo, "second still-buggy file", {
            "hooks/other-gate.sh": PATTERN + "\n",
        })
        still_broken_ticket = Ticket(
            id="uuid-ENG-200", identifier="ENG-200", title="still broken",
            description=(
                f"The bug: (`{PATTERN}`) is naive.\n\n"
                "**Files:** `hooks/other-gate.sh` (~lines 1-3)"
            ),
            assignee_email=None,
        )
        confirmed_gone_ticket = self._mk_ticket("ENG-201")
        unparseable_ticket = Ticket(
            id="uuid-ENG-202", identifier="ENG-202", title="no shape",
            description="No files section, no pattern, nothing to parse.",
            assignee_email=None,
        )
        other_assignee_ticket = Ticket(
            id="uuid-ENG-203", identifier="ENG-203", title="someone else's",
            description=DESC_TEMPLATE.format(pattern=PATTERN),
            assignee_email="someone.else@example.com",
        )

        client = FakeLinearClient([
            still_broken_ticket, confirmed_gone_ticket,
            unparseable_ticket, other_assignee_ticket,
        ])
        results = run_reconcile(
            client, epic="EPIC-1", repo=self.repo, default_branch="main",
            owner_email=OWNER_EMAIL, live=True,
        )

        by_id = {r.identifier: r for r in results}
        self.assertEqual(by_id["ENG-200"].verdict, "STILL_PRESENT")
        self.assertEqual(by_id["ENG-200"].action, "none")
        self.assertEqual(by_id["ENG-201"].verdict, "CONFIRMED_GONE")
        self.assertEqual(by_id["ENG-201"].action, "closed")
        self.assertEqual(by_id["ENG-202"].verdict, "UNPARSEABLE")
        self.assertEqual(by_id["ENG-203"].verdict, "SKIPPED_ASSIGNEE")

        # Exactly one close, and it names the file:line + fix sha in the
        # comment — evidence-before-assertion, not a bare status flip.
        self.assertEqual(len(client.closed), 1)
        closed_id, comment = client.closed[0]
        self.assertEqual(closed_id, "uuid-ENG-201")
        self.assertIn("hooks/pr-gate.sh", comment)
        self.assertIn(fix_sha, comment)

    def test_owner_assignee_is_eligible_not_skipped(self):
        self.assertTrue(_assignee_eligible(
            Ticket("i", "ENG-1", "t", "d", OWNER_EMAIL), OWNER_EMAIL,
        ))
        self.assertTrue(_assignee_eligible(
            Ticket("i", "ENG-1", "t", "d", None), OWNER_EMAIL,
        ))
        self.assertFalse(_assignee_eligible(
            Ticket("i", "ENG-1", "t", "d", "other@example.com"), OWNER_EMAIL,
        ))

    def test_no_owner_configured_means_unassigned_only(self):
        self.assertTrue(_assignee_eligible(Ticket("i", "ENG-1", "t", "d", None), None))
        self.assertFalse(_assignee_eligible(
            Ticket("i", "ENG-1", "t", "d", OWNER_EMAIL), None,
        ))


# --------------------------------------------------------------------------
# A real 1-off ticket's shape — the reconciler's original dry-run target.
# Its full description text (ticket ids genericized), run through the real
# parser + a repo that reconstructs hooks/pr-gate.sh before/after the fixing
# PR (#115) merged.
# --------------------------------------------------------------------------

#
# FULL text, not a trimmed stand-in. This is deliberate: a trimmed fixture is
# what let the ORIGINAL implementation of `_select_pattern` ship with a real
# bug — "longest code-shaped span wins" silently picked the "Demonstrated
# live" section's 83-char repro quote ("# Scenario 2: shell comment
# mentioning gh pr create (never executed as a command)") over the actual
# ~40-char defect snippet quoted earlier in the SAME ticket, and every
# trimmed unit fixture in this file (which omits that section) was blind to
# it. Only running a real dry-run against production Linear caught it. The
# full text stays here so this exact regression can never come back silently.
REAL_TICKET_DESCRIPTION = (
    "Found incidentally while deep-reviewing ENG-421 (hooks/pr-arm-stamp.sh, "
    "a sibling PostToolUse hook that mirrors pr-gate.sh's PreToolUse "
    "command-match heuristic).\n\n"
    "**The bug: **`pr-gate.sh`'s trigger match strips single- and "
    "double-quoted spans (`sed -E \"s/'[^']*'//g; s/\\\"[^\\\"]*\\\"//g\"`) "
    "before checking whether the remaining text contains the literal phrase "
    "`gh pr create`. This does NOT account for the phrase appearing inside "
    "an escaped/nested double-quote context (e.g. a JSON string embedded in "
    "a heredoc, or a plain unquoted comment) — the stripping can leave the "
    "phrase exposed as \"unquoted\" even though it was never a real command "
    "invocation.\n\n"
    "**Demonstrated live, twice, during ENG-421's own review:**\n\n"
    "1. The owner's own session hit this directly — a `git commit` heredoc whose "
    "commit message mentioned `gh pr create` in backticks (not real shell "
    "quotes) got DENIED by the live pr-gate.sh hook with \"PR BLOCKED: no "
    "PRlaunch gate record...\", even though the command was `git commit`, "
    "not `gh pr create`.\n"
    "2. The ENG-421 security-review subagent reproduced it again: a Python "
    "file-writing command containing the comment "
    "`# Scenario 2: shell comment mentioning gh pr create (never executed as a command)` "
    "also matched and denied.\n\n"
    "**Impact:** for `pr-gate.sh` specifically this fails SAFE. That's why "
    "this is LOW priority and filed here rather than blocking ENG-421. "
    "Contrast with `hooks/pr-arm-stamp.sh` (ENG-421), which had the same "
    "underlying heuristic but a much worse consequence on a false match "
    "(silently mislabeling an unrelated PR) — ENG-421 fixed that specific "
    "hook with a `createdAt`-based freshness gate. `pr-gate.sh` itself was "
    "left untouched (out of scope for that PR — it wasn't modified there).\n\n"
    "**Suggested fix:** tighten the quote-stripping to also treat "
    "backtick-delimited spans and escaped-quote JSON-string content as "
    "\"quoted\", or require the trigger phrase to appear at what looks like "
    "true command-position (start of command, after `&&`/`;`/`|`/`(` ) "
    "rather than anywhere in the raw text. Add a regression test for "
    "\"phrase appears only in a comment/backtick span, never as a real "
    "invocation\" — pr-gate.sh's existing "
    "`test_quoted_mention_does_not_trigger` only covers real single-quote "
    "spans, not this shape.\n\n"
    "**Files: **`hooks/pr-gate.sh` (trigger match, ~lines 15-21), "
    "`tests/test_pr_gate.py`."
)

PRE_PR115_PR_GATE_SH = (
    "#!/bin/bash\n"
    "input=$(cat)\n"
    "cmd=$(jq -r '.tool_input.command // \"\"' <<<\"$input\")\n"
    "cleaned=$(sed -E \"s/'[^']*'//g; s/\\\"[^\\\"]*\\\"//g\" <<<\"$cmd\")\n"
    "case \"$cleaned\" in\n"
    "  *\"gh pr create\"*) ;;\n"
    "  *) exit 0 ;;\n"
    "esac\n"
)

POST_PR115_PR_GATE_SH = (
    "#!/bin/bash\n"
    "input=$(cat)\n"
    "cmd=$(jq -r '.tool_input.command // \"\"' <<<\"$input\")\n"
    "cleaned=$(lex_and_anchor \"$cmd\")\n"
    "case \"$cleaned\" in\n"
    "  *\"gh pr create\"*) ;;\n"
    "  *) exit 0 ;;\n"
    "esac\n"
)


class TestRealTicketShapeFixture(unittest.TestCase):
    """A dry-run must report the ticket as NOT-yet-closeable while its
    fixing PR (#115) is open, and closeable only once #115 merges."""

    def setUp(self):
        self.repo = make_repo()

    def test_parses_real_ticket_description(self):
        finding = parse_finding("ENG-329", REAL_TICKET_DESCRIPTION)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.file, "hooks/pr-gate.sh")
        self.assertEqual(finding.line_hint, "15-21")
        self.assertIn("sed -E", finding.pattern)
        self.assertIn("s/'[^']*'//g", finding.pattern)
        # Regression guard: the ticket's own "Demonstrated live" section
        # quotes a LONGER, later backtick span (the Scenario-2 repro
        # comment) that is not the defect pattern. Picking "longest
        # code-shaped span" instead of "first" would select that instead —
        # this is the exact bug the real dry-run caught.
        self.assertNotIn("Scenario 2", finding.pattern)

    def test_not_yet_closeable_while_pr115_open(self):
        """main still has the naive sed-based quote stripper — PR #115
        hasn't merged yet, so the premise still holds."""
        _commit(self.repo, "current pr-gate.sh (PR #115 still open)", {
            "hooks/pr-gate.sh": PRE_PR115_PR_GATE_SH,
        })
        finding = parse_finding("ENG-329", REAL_TICKET_DESCRIPTION)
        v = check_premise(self.repo, "main", finding)
        self.assertEqual(v.status, "STILL_PRESENT")

    def test_becomes_closeable_once_pr115_merges(self):
        _commit(self.repo, "current pr-gate.sh (PR #115 still open)", {
            "hooks/pr-gate.sh": PRE_PR115_PR_GATE_SH,
        })
        fix_sha = _commit(self.repo, "pr-gate: anchor to a real command position (#115)", {
            "hooks/pr-gate.sh": POST_PR115_PR_GATE_SH,
        })
        finding = parse_finding("ENG-329", REAL_TICKET_DESCRIPTION)
        v = check_premise(self.repo, "main", finding)
        self.assertEqual(v.status, "CONFIRMED_GONE")
        self.assertEqual(v.fix_sha, fix_sha)


if __name__ == "__main__":
    unittest.main()
