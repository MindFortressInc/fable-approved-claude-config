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

Second job: under zsh, deny `$VAR:<path>` and hand back the braced
command.  zsh reads `:t` after an unbraced parameter as a history modifier, so
`git show $SHA:tests/x` runs `git show <sha>ests/x`, even inside double quotes.
Switching the tool shell to /bin/bash was piloted and rejected: bash 3.2 cannot
parse an apostrophe in `-m "$(cat <<'EOF' ...)"`, the commit and PR body shape.
"""
import json
import os
import re
import shutil
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

# Modifier letters tabulated against real zsh 5.9; the test suite spot-checks them
# when zsh is installed.  A word character must follow, which is what separates
# `$SHA:tests/x` from a deliberate `$f:t`, `$f:t:r`, `$f:h/lib`, `$f:r.jpg` or
# `$f:s/a/b/`: bracing those would change what they mean.  So a one-letter directory
# (`$SHA:t/x`) stays undetected, as do `:F` and `:W`, which take a delimiter argument
# and so depend on the whole path.  The 7-day replay lost no real command to this.
_MODIFIER = re.compile(
    r"(?<!\$)\$(?P<var>[A-Za-z_]\w*|\d+):"
    r"(?:[APQacehlqrstu]|[fgw][APQacehlqrstu])"
    r"[\w-][\w./-]*",
    re.ASCII,
)
# Wider than _HEREDOC: a delimiter like 'PY-EOF' is not a heredoc to the CLI, but
# it is to the shell, and its literal body must not be scanned as code.
_HEREDOC_OPEN = re.compile(r"<<(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|\\([\w.-]+)|([A-Za-z_][\w.-]*))", re.ASCII)


def has_heredoc(cmd: str) -> bool:
    if any(p.search(cmd) for p in _BITSHIFT):
        return False
    return bool(_HEREDOC.search(cmd))


def needs_guard(cmd: str) -> bool:
    if not has_heredoc(cmd):
        return False                      # CLI already appends < /dev/null
    head = cmd.lstrip()
    return not head.startswith(GUARD)     # idempotent


def blank_literals(cmd: str) -> str:
    """Same-length copy of `cmd` with text the shell will not expand blanked:
    single quotes, $'...', backslash escapes, comments, quoted heredoc bodies.
    hooks/shell-code-only.sh drops double quotes and offsets, and both matter
    here: `"$SHA:tests/x"` corrupts too, and the fix is spliced in by position."""
    out = list(cmd)
    n = len(cmd)
    in_dq = in_backticks = False
    stack: list[bool] = []             # in_dq to restore at each closing paren
    pending: list[tuple[bool, str, bool]] = []
    i = 0

    def blank(start: int, end: int) -> None:
        out[start:end] = " " * (min(end, n) - start)

    while i < n:
        c = cmd[i]
        if c == "\\":
            blank(i, i + 2)
            i += 2
        elif cmd.startswith("$((", i) and (end := cmd.find("))", i)) >= 0:
            i = end + 2                # arithmetic: its `<<` is a shift, not a heredoc
        elif c == "`":                 # re-enters code even inside double quotes
            if in_backticks:
                in_dq = stack.pop() if stack else False
            else:
                stack.append(in_dq)
                in_dq = False
            in_backticks = not in_backticks
            i += 1
        elif in_dq:
            if c == '"':
                in_dq = False
            elif cmd.startswith("$(", i):
                stack.append(True)
                in_dq = False
                i += 1
            i += 1
        elif c == "'":
            end = cmd.find("'", i + 1)
            end = n if end < 0 else end + 1
            blank(i, end)
            i = end
        elif cmd.startswith("$'", i):
            end = i + 2
            while end < n and cmd[end] != "'":
                end += 2 if cmd[end] == "\\" else 1
            blank(i, end + 1)
            i = end + 1
        elif c == '"':
            in_dq = True
            i += 1
        elif cmd.startswith("((", i) and (end := cmd.find("))", i)) >= 0:
            i = end + 2                # same for an arithmetic command
        elif c == "(":
            stack.append(False)
            i += 1
        elif c == ")":
            in_dq = stack.pop() if stack else False
            i += 1
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;|&("):
            end = cmd.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif cmd.startswith("<<<", i):
            i += 3
        elif cmd.startswith("<<", i) and (m := _HEREDOC_OPEN.match(cmd, i)):
            literal = m.group(5) is None
            delim = m.group(2) or m.group(3) or m.group(4) or m.group(5)
            pending.append((m.group(1) == "-", delim, literal))
            i = m.end()
        elif c == "\n" and pending:
            i += 1
            for strip_tabs, delim, literal in pending:
                start = i
                while i < n:
                    end = cmd.find("\n", i)
                    end = n if end < 0 else end
                    line = cmd[i:end]
                    i = min(end + 1, n)
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
                if literal:
                    blank(start, i)
                else:                  # an unquoted body still honours `\$`
                    for esc in re.finditer(r"\\.", cmd[start:i]):
                        blank(start + esc.start(), start + esc.end())
            pending = []
        else:
            i += 1
    return "".join(out)


def tool_shell_is_zsh() -> bool:
    """The CLI's own pick: the first executable one of these naming bash or zsh,
    else zsh if it is installed."""
    for shell in (os.environ.get("CLAUDE_CODE_SHELL"), os.environ.get("SHELL")):
        if shell and ("zsh" in shell or "bash" in shell) and os.access(shell, os.X_OK):
            return "zsh" in os.path.basename(shell)
    return shutil.which("zsh") is not None


def _brace(m: re.Match) -> str:
    return f"${{{m['var']}}}{m[0][len(m['var']) + 1:]}"


def brace_modifiers(cmd: str) -> tuple[str, list[str]]:
    """(corrected command, the `$VAR:` fragments it changed)."""
    matches = list(_MODIFIER.finditer(blank_literals(cmd)))
    fixed = cmd
    for m in reversed(matches):
        fixed = f"{fixed[:m.start()]}{_brace(m)}{fixed[m.end():]}"
    return fixed, [m[0] for m in matches]


def deny_reason(cmd: str, fixed: str, fragments: list[str]) -> str:
    var = _MODIFIER.match(fragments[0])["var"]
    braced = ", ".join(sorted({_MODIFIER.sub(_brace, f) for f in fragments}))
    reason = (
        f"zsh reads the letter after `${var}:` in `{fragments[0]}` as a history modifier, "
        f"so the value and the path both get corrupted. Brace it: {braced}. "
        f"If you did mean a zsh modifier, put it inside the braces, as in `${{{var}:h}}`."
    )
    if len(fixed) <= 2000:
        reason += f"\n\nCorrected command:\n{fixed}"
    return reason


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command")
    if not isinstance(cmd, str):
        return 0

    if tool_shell_is_zsh():
        fixed, fragments = brace_modifiers(cmd)
        if fragments:
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": deny_reason(cmd, fixed, fragments),
                    }
                },
                sys.stdout,
            )
            return 0

    if not needs_guard(cmd):
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
