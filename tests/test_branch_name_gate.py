"""Tests for hooks/branch-name-gate.sh — branch-creation Linear link gate.

The gate (read from the script):
  * a branch-creation verb (checkout -b/-B, switch -c/-C, worktree add -b,
    bare `git branch <new>`) with NO ticket token   -> DENY (hard floor).
  * LINEAR_SKIP=1                                    -> ALLOW (early exit).
  * token present, canonical name matches / API down / key missing -> ALLOW
    (fail-open — never blocks real work on a missing dep or API error).
  * token present but != Linear's canonical branchName -> DENY (off-slug).

The sandbox pins LINEAR_BRANCH_PREFIX=eng (see harness._LINEAR_ENV), so the
token under test is `eng-NNN`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import CURL_SHIM, HookSandbox, decision, load_json, run_hook


class BranchNameGateTest(unittest.TestCase):
    def tearDown(self):
        self.sbx.close()

    def _run(self, cmd, extra_env=None, shim=False):
        return run_hook(
            self.sbx, "branch-name-gate.sh",
            {"tool_input": {"command": cmd}},
            extra_env=extra_env, shim_path=shim,
        )

    def test_no_ticket_token_denies(self):
        # No API key configured -> the token floor is still enforced.
        self.sbx = HookSandbox()
        rc, out, _ = self._run("git checkout -b me/2242-image-fix")
        self.assertEqual(decision(out), "deny")
        self.assertIn("no eng-NNN token", load_json(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_linear_skip_allows(self):
        self.sbx = HookSandbox()
        rc, out, _ = self._run("git checkout -b me/anything LINEAR_SKIP=1")
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    def test_api_failure_fails_open(self):
        # token floor passes; the Linear query errors -> hook must NOT block.
        self.sbx = HookSandbox(linear_key="lin_fake")
        self.sbx.add_shim("curl", CURL_SHIM)
        rc, out, _ = self._run(
            "git checkout -b me/eng-2242-x",
            extra_env={"FAKE_MODE": "error"}, shim=True,
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    def test_canonical_match_allows(self):
        # token present and equals Linear's canonical name -> allow.
        self.sbx = HookSandbox(linear_key="lin_fake")
        self.sbx.add_shim("curl", CURL_SHIM)
        rc, out, _ = self._run(
            "git checkout -b me/eng-2242-image-fix",
            extra_env={"FAKE_CANONICAL": "me/eng-2242-image-fix", "FAKE_NUM": "2242"},
            shim=True,
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    def test_off_slug_denies(self):
        # token present but the name differs from Linear's canonical -> deny.
        self.sbx = HookSandbox(linear_key="lin_fake")
        self.sbx.add_shim("curl", CURL_SHIM)
        rc, out, _ = self._run(
            "git checkout -b me/eng-2242-quickfix",
            extra_env={"FAKE_CANONICAL": "me/eng-2242-image-fix", "FAKE_NUM": "2242"},
            shim=True,
        )
        self.assertEqual(decision(out), "deny")
        self.assertIn("exact branch name", load_json(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_no_key_fails_open_with_ticket_token(self):
        # token present, no API key at all -> fail-open allow (can't verify).
        self.sbx = HookSandbox()  # no key file
        rc, out, _ = self._run("git checkout -b me/eng-2242-x")
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))


    # -- heredoc BODIES are data, the code around them is not -----------------
    # The gate used to strip heredocs with `scan="${cmd%%<<*}"`, which truncates
    # the command at the FIRST `<<`, so everything after a heredoc was invisible:
    # the branch was created with no token enforcement at all. It failed OPEN.
    # Both directions are asserted — a fix that merely stopped truncating would
    # satisfy the first and break the second.

    def test_create_after_heredoc_is_gated(self):
        self.sbx = HookSandbox()
        rc, out, _ = self._run(
            "cat > notes.md <<'EOF'\n"
            "just some notes\n"
            "EOF\n"
            "git checkout -b me/no-ticket-here"
        )
        self.assertEqual(decision(out), "deny")
        self.assertIn("no eng-NNN token",
                      load_json(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_create_inside_heredoc_body_is_not_gated(self):
        self.sbx = HookSandbox()
        rc, out, _ = self._run(
            "cat > notes.md <<'EOF'\n"
            "then run: git checkout -b me/whatever\n"
            "EOF"
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    def test_quoted_heredoc_operator_does_not_swallow_the_create(self):
        self.sbx = HookSandbox()
        rc, out, _ = self._run(
            'echo "shift left: a << b"\n'
            "git checkout -b me/no-ticket-here"
        )
        self.assertEqual(decision(out), "deny")

    def test_unterminated_heredoc_fails_closed(self):
        self.sbx = HookSandbox()
        rc, out, _ = self._run(
            "cat <<EOF_NEVER_CLOSED\n"
            "git checkout -b me/no-ticket-here"
        )
        self.assertEqual(decision(out), "deny")

    def test_linear_skip_mentioned_in_heredoc_does_not_disarm(self):
        # The escape hatch is a USE of LINEAR_SKIP=1, not a mention of it.
        self.sbx = HookSandbox()
        rc, out, _ = self._run(
            "cat > notes.md <<'EOF'\n"
            "Use LINEAR_SKIP=1 for genuinely ticket-less branches.\n"
            "EOF\n"
            "git checkout -b me/no-ticket-here"
        )
        self.assertEqual(decision(out), "deny")

    def test_off_slug_create_after_heredoc_denies(self):
        self.sbx = HookSandbox(linear_key="lin_fake")
        self.sbx.add_shim("curl", CURL_SHIM)
        rc, out, _ = self._run(
            "cat > notes.md <<'EOF'\n"
            "notes\n"
            "EOF\n"
            "git checkout -b me/eng-2242-quickfix",
            extra_env={"FAKE_CANONICAL": "me/eng-2242-image-fix", "FAKE_NUM": "2242"},
            shim=True,
        )
        self.assertEqual(decision(out), "deny")
        self.assertIn("exact branch name",
                      load_json(out)["hookSpecificOutput"]["permissionDecisionReason"])

    # -- a QUOTED branch name is still the branch name -------------------------
    # Both strippers drop a quoted span whole, so quoting the branch name hid it
    # from the extraction: with a start point after it the gate blamed
    # `origin/main`; with nothing after it no create was detected and ANY quoted
    # name sailed through un-gated.

    def test_quoted_worktree_branch_is_not_read_as_the_start_point(self):
        self.sbx = HookSandbox()
        rc, out, _ = self._run('git worktree add ~/code/x -b "me/no-ticket-here" origin/main')
        self.assertEqual(decision(out), "deny")
        reason = load_json(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("me/no-ticket-here", reason)
        self.assertNotIn("origin/main", reason)

    def test_quoted_worktree_branch_with_token_is_allowed(self):
        self.sbx = HookSandbox(linear_key="lin_fake")
        self.sbx.add_shim("curl", CURL_SHIM)
        rc, out, _ = self._run(
            'git worktree add ~/code/x -b "me/eng-2242-image-fix" origin/main',
            extra_env={"FAKE_CANONICAL": "me/eng-2242-image-fix", "FAKE_NUM": "2242"},
            shim=True,
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    def test_quoted_branch_name_does_not_disarm_the_gate(self):
        self.sbx = HookSandbox()
        for cmd in (
            'git checkout -b "me/no-ticket-here"',
            "git checkout -b 'me/no-ticket-here'",
            'git switch -c "me/no-ticket-here"',
            'git branch "me/no-ticket-here"',
            'git checkout -b "me/no-ticket-here" && git push',
        ):
            with self.subTest(cmd=cmd):
                rc, out, _ = self._run(cmd)
                self.assertEqual(decision(out), "deny")
                reason = load_json(out)["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("me/no-ticket-here", reason)

    def test_quoted_suffix_fragment_is_part_of_the_branch_name(self):
        # `me/eng-2242"-quickfix"` is ONE bash word: the branch
        # `me/eng-2242-quickfix`, not the unquoted prefix.
        self.sbx = HookSandbox(linear_key="lin_fake")
        self.sbx.add_shim("curl", CURL_SHIM)
        for cmd in ('git checkout -b me/eng-2242"-quickfix"',
                    'git worktree add ~/x -b me/eng-2242"-quickfix" origin/main'):
            with self.subTest(cmd=cmd):
                rc, out, _ = self._run(
                    cmd, extra_env={"FAKE_CANONICAL": "me/eng-2242", "FAKE_NUM": "2242"},
                    shim=True,
                )
                self.assertEqual(decision(out), "deny")
                reason = load_json(out)["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("me/eng-2242-quickfix", reason)

    def test_quoted_suffix_fragment_matching_canonical_allows(self):
        self.sbx = HookSandbox(linear_key="lin_fake")
        self.sbx.add_shim("curl", CURL_SHIM)
        rc, out, _ = self._run(
            'git checkout -b me/eng-2242"-quickfix"',
            extra_env={"FAKE_CANONICAL": "me/eng-2242-quickfix", "FAKE_NUM": "2242"},
            shim=True,
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    def test_quoted_create_inside_a_message_is_still_data(self):
        # Un-quoting the OPERAND must not un-quote the data span AROUND a
        # create-looking phrase.
        self.sbx = HookSandbox()
        for cmd in (
            'git commit -m "then run git checkout -b me/no-ticket-here"',
            """git commit -m 'see: git checkout -b "me/no-ticket-here"'""",
        ):
            with self.subTest(cmd=cmd):
                rc, out, _ = self._run(cmd)
                self.assertEqual(rc, 0)
                self.assertIsNone(decision(out))


if __name__ == "__main__":
    unittest.main()
