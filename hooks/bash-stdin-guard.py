#!/usr/bin/env python3
"""Restore the missing `< /dev/null` guard on heredoc Bash commands.

Claude Code appends `< /dev/null` to every Bash tool command EXCEPT when the
command contains a heredoc -- see the CLI's own H5n()/xSt() pair:

    function H5n(e,n=!0){ if(xSt(e)||pds(e)){ let d=quote(e);
                          if(xSt(e)) return d;            // <-- no redirect
                          return n?`${d} < /dev/null`:d } ... }

Without it the tool shell inherits the CLI's own stdin, a unix socketpair that
never reaches EOF.  Any command in that block that reads stdin with no file
argument (`jq -r .`, `sort`, `cat`, `grep pat`, `python3 -`, `read`) blocks
forever, the Bash tool call never returns, and the terminal -- plain zsh tab or
tmux pane -- looks wedged.  Measured 2026-09-15: a `jq -r` missing its filename
held a session for 6h10m.

Prepending `exec < /dev/null` gives the shell the same stdin the CLI would have
given it anyway.  Heredocs are unaffected: a heredoc redirect overrides the
shell's fd 0 for its own command.
"""
import json
import re
import sys

# Ported verbatim from the CLI's xSt(): the same test that suppresses the guard.
_BITSHIFT = (
    re.compile(r"\d\s*<<\s*\d"),
    re.compile(r"\[\[\s*\d+\s*<<\s*\d+\s*\]\]"),
    re.compile(r"\$\(\(.*<<.*\)\)"),
)
# re.ASCII so \w means [A-Za-z0-9_] exactly as it does in the CLI's JS regex;
# Python's default unicode \w would classify a non-ASCII delimiter as a heredoc
# the CLI does not, and we would guard a command it already guarded.
_HEREDOC = re.compile(r"<<-?\s*(?:(['\"]?)(\w+)\1|\\(\w+))", re.ASCII)

GUARD = "exec < /dev/null"


def has_heredoc(cmd: str) -> bool:
    if any(p.search(cmd) for p in _BITSHIFT):
        return False
    return bool(_HEREDOC.search(cmd))


def needs_guard(cmd: str) -> bool:
    if not has_heredoc(cmd):
        return False                      # CLI already appends < /dev/null
    head = cmd.lstrip()
    return not head.startswith(GUARD)     # idempotent


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not needs_guard(cmd):
        return 0

    updated = dict(tool_input)
    updated["command"] = f"{GUARD}\n{cmd}"
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
