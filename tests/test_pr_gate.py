"""Tests for hooks/pr-gate.sh — the PR gate + Linear link gate.

pr-gate.sh validates a per-gate JSON ledger and only falls back
to the LEGACY plain-sha marker (with a migration warning) when no ledger exists.
These tests cover both paths plus the Linear link gate and escape hatches.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (
    HookSandbox, decision, load_json, make_git_repo, make_worktree, run_hook,
    run_hook_args, set_remote,
)

# Built from fragments so the literal token never trips the live pr-gate hook
# while THIS test file is being edited by an agent. At runtime pytest just reads
# it as a Python string.
GHPR = "gh pr " + "create"


class PrGateTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()
        self.repo = os.path.join(self.sbx.dir, "myrepo")
        self.branch = "me/eng-1234-x"
        self.head = make_git_repo(self.repo, self.branch, self.sbx.env())
        self.repo_name = os.path.basename(self.repo)

    def tearDown(self):
        self.sbx.close()

    def _run(self, cmd):
        return run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": cmd}, "cwd": self.repo},
        )

    def _gate_entry(self, sha):
        return {"sha": sha, "ts": "2026-07-06T00:00:00Z"}

    def _valid_gates(self):
        return {g: self._gate_entry(self.head)
                for g in ("deep_review", "cr_cli", "outcome_eval", "tests")}

    def _reason(self, out):
        return load_json(out)["hookSpecificOutput"]["permissionDecisionReason"]

    # -- no evidence at all ----------------------------------------------
    def test_no_marker_denies(self):
        rc, out, _ = self._run(GHPR + " --fill")
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_no_ledger_no_legacy_denies(self):
        # Explicit: neither a JSON ledger nor a legacy marker file present.
        rc, out, _ = self._run(GHPR + " --fill")
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    # -- JSON ledger (authoritative) -------------------------------------
    def test_valid_ledger_allows(self):
        self.sbx.write_ledger(self.repo_name, self.branch, self._valid_gates())
        rc, out, _ = self._run(GHPR + " --fill")
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))  # silent allow

    def test_ledger_stale_deep_review_denies_naming_it(self):
        gates = self._valid_gates()
        gates["deep_review"] = self._gate_entry("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        self.sbx.write_ledger(self.repo_name, self.branch, gates)
        rc, out, _ = self._run(GHPR + " --fill")
        self.assertEqual(decision(out), "deny")
        reason = self._reason(out)
        self.assertIn("deep_review", reason)
        self.assertIn("STALE", reason)

    def test_ledger_missing_gate_denies_naming_it(self):
        gates = self._valid_gates()
        del gates["cr_cli"]
        self.sbx.write_ledger(self.repo_name, self.branch, gates)
        rc, out, _ = self._run(GHPR + " --fill")
        self.assertEqual(decision(out), "deny")
        self.assertIn("cr_cli", self._reason(out))

    # -- LEGACY plain-sha marker (back-compat) ---------------------------
    def test_legacy_marker_matches_head_warns_and_allows(self):
        # No JSON ledger, only a legacy plain-sha marker at HEAD → allowed with
        # a migration warning (the systemMessage back-compat notice).
        self.sbx.write_marker(self.repo_name, self.branch, self.head)
        rc, out, _ = self._run(GHPR + " --fill")
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))  # a bare notice, not a deny/allow decision
        self.assertIn("legacy PRlaunch marker accepted", load_json(out)["systemMessage"])

    def test_legacy_marker_mismatch_denies(self):
        self.sbx.write_marker(self.repo_name, self.branch, "deadbeefdeadbeef")
        rc, out, _ = self._run(GHPR + " --fill")
        self.assertEqual(decision(out), "deny")
        self.assertIn("changed since PRlaunch", self._reason(out))

    def test_ledger_beats_legacy_marker(self):
        # Both present: a VALID ledger wins over a stale legacy marker → allow.
        self.sbx.write_ledger(self.repo_name, self.branch, self._valid_gates())
        self.sbx.write_marker(self.repo_name, self.branch, "deadbeefdeadbeef")
        rc, out, _ = self._run(GHPR + " --fill")
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    # -- escape hatches & link gate --------------------------------------
    def test_prlaunch_skip_allows(self):
        # No evidence at all, but the escape hatch bypasses everything.
        rc, out, _ = self._run("PRLAUNCH_SKIP=1 " + GHPR + " --fill")
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    def test_link_gate_denies_without_dev_token(self):
        # Branch has no ticket token and the command carries no ticket id.
        repo = os.path.join(self.sbx.dir, "nolink")
        make_git_repo(repo, "feature/nolink", self.sbx.env())
        rc, out, _ = run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": GHPR + " --fill"}, "cwd": repo},
        )
        self.assertEqual(decision(out), "deny")
        self.assertIn("won't link to a tracker ticket", self._reason(out))

    def test_linear_skip_bypasses_link_gate(self):
        # Un-linkable branch, but LINEAR_SKIP=1 clears the link gate; a valid
        # ledger then lets it through — proving the link gate was bypassed.
        repo = os.path.join(self.sbx.dir, "nolink2")
        head = make_git_repo(repo, "feature/nolink2", self.sbx.env())
        gates = {g: {"sha": head, "ts": "2026-07-06T00:00:00Z"}
                 for g in ("deep_review", "cr_cli", "outcome_eval", "tests")}
        self.sbx.write_ledger(os.path.basename(repo), "feature/nolink2", gates)
        rc, out, _ = run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": "LINEAR_SKIP=1 " + GHPR + " --fill"}, "cwd": repo},
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    # -- repo dir resolution ---------------------------------------------
    def test_repo_dir_takes_the_last_cd_before_the_trigger(self):
        # `cd /elsewhere && cd <repo> && gh pr create` lands in <repo>, so the
        # gate must key the ledger off <repo>. Taking the FIRST cd consults the
        # decoy's (nonexistent) ledger and denies a properly gated PR.
        decoy = os.path.join(self.sbx.dir, "decoy")
        make_git_repo(decoy, "me/eng-9999-decoy", self.sbx.env())
        self.sbx.write_ledger(self.repo_name, self.branch, self._valid_gates())
        cmd = "cd " + decoy + " && cd " + self.repo + " && " + GHPR + " --fill"
        rc, out, _ = run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": cmd}, "cwd": self.sbx.dir},
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out), "should resolve the target repo, not the decoy")

    def test_repo_dir_prefers_the_last_cd_over_a_later_git_dash_c(self):
        # `git -C <dir>` runs one command elsewhere; it does NOT move the
        # shell. The PR is still created from the last cd, so a `git -C` on
        # another repo must not hijack the resolution.
        decoy = os.path.join(self.sbx.dir, "decoy3")
        make_git_repo(decoy, "me/eng-7777-decoy", self.sbx.env())
        self.sbx.write_ledger(self.repo_name, self.branch, self._valid_gates())
        cmd = ("cd " + self.repo + " && git -C " + decoy + " fetch && "
               + GHPR + " --fill")
        rc, out, _ = run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": cmd}, "cwd": self.sbx.dir},
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out), "cd wins over a later git -C")

    def test_repo_dir_falls_back_to_git_dash_c_when_there_is_no_cd(self):
        # With no cd at all, `git -C <dir>` is the only signal for which repo
        # the PR belongs to — keep honouring it.
        self.sbx.write_ledger(self.repo_name, self.branch, self._valid_gates())
        cmd = "git -C " + self.repo + " push && " + GHPR + " --fill"
        rc, out, _ = run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": cmd}, "cwd": self.sbx.dir},
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    def test_repo_dir_ignores_a_cd_after_the_trigger(self):
        # A trailing `&& cd /elsewhere` runs only after the PR is created; it
        # must not decide which ledger is checked.
        decoy = os.path.join(self.sbx.dir, "decoy2")
        make_git_repo(decoy, "me/eng-8888-decoy", self.sbx.env())
        self.sbx.write_ledger(self.repo_name, self.branch, self._valid_gates())
        cmd = "cd " + self.repo + " && " + GHPR + " --fill && cd " + decoy
        rc, out, _ = run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": cmd}, "cwd": self.sbx.dir},
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))

    def test_quoted_mention_does_not_trigger(self):
        # The trigger token appears only inside a single-quoted string, so the
        # gate must strip it and never fire (even though there's no marker).
        cmd = "echo 'remember to " + GHPR + " later'"
        rc, out, _ = self._run(cmd)
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out))


    # -- the trigger is a COMMAND POSITION, not a substring -------------------
    # Direction 1 — a command that merely MENTIONS the phrase must pass through.
    # Direction 2 (further down) — a command that really opens a PR must still
    # be blocked. A fix that only satisfies direction 1 is a security regression.

    def test_heredoc_body_mention_does_not_trigger(self):
        # The live repro: writing a brief file whose CONTENTS mention the phrase
        # on an unquoted line. The quote-stripping sed is per-line and matches
        # PAIRS, so it cannot help here — and because the write happens outside
        # any repo, the old hook denied with "cannot resolve a git repo from
        # '$HOME'": an error naming an action nobody attempted.
        outside = os.path.join(self.sbx.dir, "no-repo-here")
        os.makedirs(outside, exist_ok=True)
        cmd = (
            "cat > %s/brief.md <<'EOF'\n"
            "- Ship it: re-gate, push, then open a READY PR with %s --base main\n"
            "EOF"
        ) % (outside, GHPR)
        rc, out, _ = run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": cmd}, "cwd": outside},
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out), out)

    def test_commit_message_heredoc_mention_does_not_trigger(self):
        # The original repro: `git commit -m "$(cat <<'EOF' … EOF)"`
        # whose message body mentions the phrase. The commit was refused.
        cmd = (
            "git commit -m \"$(cat <<'EOF'\n"
            "docs: PRlaunch phase 5 calls it between git push and %s\n"
            "EOF\n"
            ")\""
        ) % GHPR
        rc, out, _ = self._run(cmd)
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out), out)

    def test_unquoted_prose_mention_does_not_trigger(self):
        # Not quoted at all — only the position anchor can reject this one.
        rc, out, _ = self._run("echo remember to " + GHPR + " later")
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out), out)

    def test_grep_pattern_mention_does_not_trigger(self):
        rc, out, _ = self._run('grep -rn "%s" commands/' % GHPR)
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out), out)

    def test_directory_resolves_from_the_matched_position(self):
        # The `cd` search must be cut where the trigger MATCHED, not at the first
        # textual mention. With the old `${cmd%%…*}` split the mention truncated
        # the search before the real `cd`, so the hook resolved the hook cwd and
        # denied with "cannot resolve a git repo" — an error about the wrong
        # action entirely, which is the expensive part of this bug.
        outside = os.path.join(self.sbx.dir, "not-a-repo")
        os.makedirs(outside, exist_ok=True)
        cmd = "echo 'next step: %s' && cd %s && %s --fill" % (
            GHPR, self.repo, GHPR)
        rc, out, _ = run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": cmd}, "cwd": outside},
        )
        self.assertEqual(decision(out), "deny")
        reason = self._reason(out)
        self.assertNotIn("cannot resolve a git repo", reason)
        self.assertIn("no PRlaunch gate record", reason)
        self.assertIn(self.repo_name, reason)

    # -- Direction 2: FAIL-CLOSED — real invocations are still blocked --------

    def test_invocation_on_its_own_line_still_denied(self):
        rc, out, _ = self._run("cd %s\n%s --fill" % (self.repo, GHPR))
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_invocation_after_separator_still_denied(self):
        rc, out, _ = self._run("cd %s && %s --fill" % (self.repo, GHPR))
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_invocation_with_heredoc_body_still_denied(self):
        # PRlaunch's own shape: the invocation carries a heredoc PR body.
        cmd = (
            "%s --title \"t\" --body \"$(cat <<'EOF'\n"
            "Closes ENG-1234\n"
            "EOF\n"
            ")\""
        ) % GHPR
        rc, out, _ = self._run(cmd)
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_invocation_after_a_heredoc_still_denied(self):
        # Truncating at the FIRST `<<` (the idiom branch-name-gate.sh uses) would
        # drop this invocation entirely and fail OPEN.
        cmd = (
            "cat > %s/body.md <<'EOF'\n"
            "Closes ENG-1234\n"
            "EOF\n"
            "%s --title t --body-file %s/body.md"
        ) % (self.sbx.dir, GHPR, self.sbx.dir)
        rc, out, _ = self._run(cmd)
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_invocation_after_a_bogus_heredoc_marker_still_denied(self):
        # `<<` inside a quoted string registers a delimiter that never closes.
        # The stripper must re-emit what it skipped rather than swallow the rest
        # of the command — otherwise this is a one-line gate bypass.
        cmd = 'echo "compare << and >> here"\n%s --fill' % GHPR
        rc, out, _ = self._run(cmd)
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_quoted_heredoc_marker_cannot_swallow_the_invocation(self):
        # CR CLI find: a `<<` inside a QUOTED string used to register a real
        # delimiter, so the lines up to a matching terminator — including the
        # actual invocation — were eaten as a "heredoc body". A one-command
        # bypass. Quote state has to be tracked before heredocs are detected.
        cmd = 'echo "note << EOF"\n%s --fill\nEOF' % GHPR
        rc, out, _ = self._run(cmd)
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_reserved_word_prefixed_invocation_still_denied(self):
        # CR CLI find: `if <cmd>; then …` is a real command position.
        cmd = "cd %s; if %s --fill; then :; fi" % (self.repo, GHPR)
        rc, out, _ = self._run(cmd)
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_leading_redirection_invocation_still_denied(self):
        # CR CLI find: a leading redirection sits before the command word.
        for redir in (">/tmp/pr.out", "> /tmp/pr.out", "2>&1"):
            with self.subTest(redir=redir):
                cmd = "cd %s && %s %s --fill" % (self.repo, redir, GHPR)
                rc, out, _ = self._run(cmd)
                self.assertEqual(decision(out), "deny", cmd)
                self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_command_substitution_invocation_still_denied(self):
        cmd = "cd %s && url=$(%s --fill)" % (self.repo, GHPR)
        rc, out, _ = self._run(cmd)
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_wrapped_or_path_invocations_still_denied(self):
        # Wrapper words with operands, an absolute path to gh, and an escaped
        # command word all run the real thing -- each must still be gated.
        for pre in ("/opt/homebrew/bin/", "timeout 60 ", "nice ", "nice -n 5 ",
                    "sudo -E ", "env -i ", "\\"):
            with self.subTest(prefix=pre):
                cmd = "cd %s && %s%s --fill" % (self.repo, pre, GHPR)
                rc, out, _ = self._run(cmd)
                self.assertEqual(decision(out), "deny", cmd)
                self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_ordinary_command_mentioning_the_verb_still_passes(self):
        for cmd in ("echo gh pr create later", "git log --grep 'gh pr create'",
                    "printf '%s\\n' gh pr create"):
            with self.subTest(cmd=cmd):
                rc, out, _ = self._run(cmd)
                self.assertEqual(rc, 0)
                self.assertIsNone(decision(out), out)

    def test_shell_comment_mention_does_not_trigger(self):
        rc, out, _ = self._run("cd %s   # then run %s\ngit status" % (
            self.repo, GHPR))
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out), out)

    def test_skip_hatch_quoted_in_body_does_not_bypass(self):
        # "PRLAUNCH_SKIP=1" inside the PR body is prose ABOUT the hatch, not a
        # use of it, and must not disarm the gate on a real invocation.
        cmd = GHPR + ' --title t --body "PRLAUNCH_SKIP=1 is for emergencies"'
        rc, out, _ = self._run(cmd)
        self.assertEqual(decision(out), "deny")
        self.assertIn("no PRlaunch gate record", self._reason(out))

    def test_missing_shell_lib_fails_closed(self):
        # pr-gate.sh sources shell-code-only.sh for the code-only projection.
        # Without the library it must scan the WHOLE command: a quoted mention
        # may then over-trigger, but a real invocation can never slip through.
        import shutil
        import subprocess
        lone = os.path.join(self.sbx.dir, "lone-hooks")
        os.makedirs(lone)
        for name in ("pr-gate.sh", "prlaunch-gate.sh"):
            shutil.copy(os.path.join(os.path.dirname(self.sbx.hook_path(name)),
                                     os.readlink(self.sbx.hook_path(name))), lone)
        payload = '{"tool_input": {"command": "cd %s && %s --fill"}, "cwd": "%s"}' % (
            self.repo, GHPR, self.repo)
        proc = subprocess.run(["bash", os.path.join(lone, "pr-gate.sh")],
                              input=payload, capture_output=True, text=True,
                              env=self.sbx.env())
        self.assertEqual(decision(proc.stdout), "deny", proc.stdout + proc.stderr)


class PrGateWorktreeIdentityTest(unittest.TestCase):
    """End-to-end: pr-gate must read the ledger prlaunch-gate WROTE.

    Both scripts used to derive the path independently from
    `basename "$(git rev-parse --show-toplevel)"`. In a worktree that is the
    worktree's directory name, so two repos whose worktrees were both named
    after the ticket shared one ledger -- and the hook that decides whether a
    PR may open was reading another repository's evidence.
    """

    BRANCH = "me/eng-9254-y"

    def setUp(self):
        self.sbx = HookSandbox()
        self.e = self.sbx.env()
        self.svc = os.path.join(self.sbx.dir, "api-svc")
        self.fe = os.path.join(self.sbx.dir, "web-app")
        make_git_repo(self.svc, "trunk", self.e)
        make_git_repo(self.fe, "trunk", self.e)
        set_remote(self.svc, "git@github.com:Org/api-svc.git", self.e)
        set_remote(self.fe, "git@github.com:Org/web-app.git", self.e)
        # colliding worktree basename, identical branch -> identical old key
        self.svc_wt = os.path.join(self.sbx.dir, "a-worktrees", "eng-9254")
        self.fe_wt = os.path.join(self.sbx.dir, "b-worktrees", "eng-9254")
        make_worktree(self.svc, self.svc_wt, self.BRANCH, self.e, "svc work")
        make_worktree(self.fe, self.fe_wt, self.BRANCH, self.e, "fe work")

    def tearDown(self):
        self.sbx.close()

    def _gate(self, wt, *args):
        return run_hook_args(self.sbx, "prlaunch-gate.sh", ["--repo-dir", wt, *args])

    def _run_from(self, wt):
        return run_hook(
            self.sbx, "pr-gate.sh",
            {"tool_input": {"command": GHPR + " --fill"}, "cwd": wt},
        )

    def _record_all(self, wt):
        scen = os.path.join(self.sbx.dir, "scen.md")
        with open(scen, "w") as fh:
            fh.write("scenario 1: user sees X, PASS if Y\n")
        for args in (("deep_review",), ("cr_cli",), ("scenarios", scen),
                     ("outcome_eval",), ("tests", "--cmd", "x")):
            rc, out, err = self._gate(wt, "record", *args)
            self.assertEqual(rc, 0, out + err)

    def test_gated_worktree_is_allowed(self):
        self._record_all(self.svc_wt)
        rc, out, _ = self._run_from(self.svc_wt)
        self.assertEqual(rc, 0)
        self.assertIsNone(decision(out), out)  # silent allow

    def test_other_repo_gates_do_not_unlock_this_repo(self):
        """The whole defect, at the point that matters: api-svc runs a
        full PRlaunch; web-app must still be blocked."""
        self._record_all(self.svc_wt)
        rc, out, _ = self._run_from(self.fe_wt)
        self.assertEqual(decision(out), "deny", out)
        reason = load_json(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("no PRlaunch gate record", reason)
        self.assertIn("web-app", reason)

    def test_deny_reason_names_the_real_repo_not_the_worktree_dir(self):
        rc, out, _ = self._run_from(self.svc_wt)
        reason = load_json(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("api-svc", reason)
        self.assertNotIn("eng-9254/", reason)


if __name__ == "__main__":
    unittest.main()
