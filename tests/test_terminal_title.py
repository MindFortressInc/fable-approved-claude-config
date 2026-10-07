"""Tests for skills/titles/terminal-title.py.

A real emit needs a live VS Code tab, so these cover what a script CAN verify:
title extraction from a transcript (latest aiTitle wins, reverse scan), control
characters stripped before the OSC sequence is written, and the hook path never
writing to stdout (a UserPromptSubmit hook's stdout is injected into Claude's
context). The tty is always a temp file or stubbed — no test writes to a real
terminal.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT


def _load():
    path = os.path.join(REPO_ROOT, "skills", "titles", "terminal-title.py")
    spec = importlib.util.spec_from_file_location("terminal_title_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tt = _load()


class TerminalTitleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="titles-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _transcript(self, records, name="t.jsonl"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            for r in records:
                fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        return path

    def test_latest_ai_title_wins(self):
        path = self._transcript([
            {"type": "user", "timestamp": "2026-07-01T00:00:00Z"},
            {"type": "ai-title", "aiTitle": "First topic"},
            {"type": "assistant"},
            {"type": "ai-title", "aiTitle": "Second topic"},
            {"type": "assistant"},
        ])
        self.assertEqual(tt.last_ai_title(path), "Second topic")

    def test_corrupt_line_is_skipped_not_fatal(self):
        path = self._transcript([
            {"type": "ai-title", "aiTitle": "Good title"},
            '{"aiTitle": broken json',
        ])
        self.assertEqual(tt.last_ai_title(path), "Good title")

    def test_no_title_or_empty_file_is_none(self):
        self.assertIsNone(tt.last_ai_title(self._transcript([{"type": "user"}])))
        self.assertIsNone(tt.last_ai_title(self._transcript([], name="empty.jsonl")))
        self.assertIsNone(tt.last_ai_title(os.path.join(self.tmp, "missing.jsonl")))

    def test_emit_strips_control_chars(self):
        fake_tty = os.path.join(self.tmp, "tty")
        self.assertTrue(tt.emit("evil\x1b]0;pwned\x07 title\x7f", fake_tty))
        with open(fake_tty) as fh:
            written = fh.read()
        self.assertEqual(written, "\x1b]0;%s evil]0;pwned title\x07" % tt.PREFIX)

    def test_emit_strips_c1_controls(self):
        # ST (\x9c) and CSI (\x9b) terminate/start sequences on some terminals.
        fake_tty = os.path.join(self.tmp, "tty")
        self.assertTrue(tt.emit("a\x9cb\x9b2Jc\x85d é", fake_tty))
        with open(fake_tty) as fh:
            written = fh.read()
        self.assertEqual(written, "\x1b]0;%s ab2Jcd é\x07" % tt.PREFIX)

    def test_emit_without_tty_is_a_noop(self):
        self.assertFalse(tt.emit("title", None))

    def test_hook_path_never_writes_stdout(self):
        path = self._transcript([{"type": "ai-title", "aiTitle": "Hook topic"}])
        emitted = []
        orig_own, orig_emit = tt.own_tty, tt.emit
        tt.own_tty = lambda: os.path.join(self.tmp, "tty")
        tt.emit = lambda title, tty: emitted.append((title, tty)) or True
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                tt.run_hook({"transcript_path": path, "cwd": self.tmp})
        finally:
            tt.own_tty, tt.emit = orig_own, orig_emit
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(emitted, [("Hook topic", os.path.join(self.tmp, "tty"))])

    def test_missing_exact_transcript_does_not_fall_through(self):
        # A supplied transcript_path that no longer exists must not resolve to
        # some other session's newest transcript.
        self._transcript([{"type": "ai-title", "aiTitle": "Other session"}], name="other.jsonl")
        data = {"transcript_path": os.path.join(self.tmp, "gone.jsonl"), "cwd": self.tmp}
        self.assertIsNone(tt.resolve_transcript(data))

    def _run_all(self, tabs, sessions):
        """tabs: [(pid, tty, start)]; sessions: {name: (first_ts, aiTitle)}."""
        for name, (ts, title) in sessions.items():
            self._transcript([
                {"type": "user", "timestamp": "1970-01-01T00:00:%02dZ" % ts},
                {"type": "ai-title", "aiTitle": title},
            ], name=name)
        emitted = []
        saved = (tt.claude_tabs, tt.cwds_for_pids, tt.project_dir_for_cwd, tt.emit)
        tt.claude_tabs = lambda: tabs
        tt.cwds_for_pids = lambda pids: {p: "/home/u/proj" for p in pids}
        tt.project_dir_for_cwd = lambda cwd: self.tmp
        tt.emit = lambda title, tty: emitted.append((tty, title)) or True
        try:
            done = tt.run_all()
        finally:
            tt.claude_tabs, tt.cwds_for_pids, tt.project_dir_for_cwd, tt.emit = saved
        return {tty: (title, fb) for tty, title, _, fb in done}

    def test_run_all_ambiguous_nearby_starts_fall_back_to_cwd(self):
        # Starts at 0 and 1; their sessions' first records at 2 and 1.5. Nearest
        # timestamp would swap them — ambiguity must keep the cwd fallback.
        res = self._run_all(
            [("10", "ttys001", 0.0), ("11", "ttys002", 1.0)],
            {"a.jsonl": (2, "Tab A topic"), "b.jsonl": (1, "Tab B topic")},
        )
        self.assertEqual(res["ttys001"], ("proj", True))
        self.assertEqual(res["ttys002"], ("proj", True))

    def test_run_all_unambiguous_match_uses_ai_title(self):
        res = self._run_all(
            [("10", "ttys001", 0.0), ("11", "ttys002", 50.0)],
            {"a.jsonl": (2, "Tab A topic"), "b.jsonl": (51, "Tab B topic")},
        )
        self.assertEqual(res["ttys001"], ("Tab A topic", False))
        self.assertEqual(res["ttys002"], ("Tab B topic", False))

    def test_all_summary_sanitizes_titles(self):
        saved = tt.run_all, sys.argv
        tt.run_all = lambda: [("ttys001", "evil\x1b]0;pwned\x07", True, False)]
        sys.argv = ["terminal-title.py", "--all"]
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                tt.main()
        finally:
            tt.run_all, sys.argv = saved
        self.assertNotIn("\x1b", err.getvalue())
        self.assertNotIn("\x07", err.getvalue())


if __name__ == "__main__":
    unittest.main()
