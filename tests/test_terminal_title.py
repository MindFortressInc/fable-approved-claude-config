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


if __name__ == "__main__":
    unittest.main()
