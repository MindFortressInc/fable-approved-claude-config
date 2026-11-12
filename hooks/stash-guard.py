#!/usr/bin/env python3
"""PreToolUse(Bash) hook: deny mutating `git stash` where refs/stash is shared.

`refs/stash` is ONE ref per repository, shared by every worktree of a clone.
With parallel agents each in their own worktree, a `git stash pop` takes
whatever is on top of that shared stack, which is routinely a sibling's WIP.
A `git stash` on a clean tree saves nothing, so the pop after it is ALWAYS
someone else's entry. We recorded 13+ cross-agent collisions this way; one
victim's work survived only as a dangling commit found with `git fsck`.

Denied: any stash subcommand except `list` / `show` (push, pop, apply, drop,
save, clear, store, create, branch, bare `git stash`, `git stash -u`, ...)
when its target repo is
  * a linked worktree (`--git-dir` != `--git-common-dir`), or
  * the main checkout while another worktree is attached (same shared stack).
A repo with no other worktrees, and a non-repo, are left alone.

The target is resolved per segment: the hook's cwd (or inherited GIT_DIR),
then any `cd` earlier in the chain (scoped to its `( )` / `$(...)` subshell),
then `env -C`, then the invocation's own `-C` / `--git-dir`. Quote-, comment-
and heredoc-aware, so "git stash" inside an echo/grep/commit message is not a
stash. `bash -c` / `sh -c` / `eval` bodies and `$(...)` (also inside double
quotes) are scanned too, and `env` / shells / `eval` match by basename like
git. A mutating stash whose target cannot be resolved (an unknown $VAR, a
`$(...)`, a `cd` into a missing directory, a failed worktree probe, shell
bodies nested past the scan depth) is denied.
Known gaps: git run under `timeout` / `nice` / `xargs` is not seen. A crash exits non-zero,
which Claude Code treats as a non-blocking error (fail open), like the other
Bash hooks here.
"""
import json
import os
import re
import shlex
import subprocess
import sys

READ_ONLY = {"list", "show"}
SHELLS = {"bash", "sh", "zsh", "dash"}
PREFIX_WORDS = {"{", "}", "!", "then", "do", "else", "elif", "if", "while",
                "until", "time", "nohup", "command", "builtin", "exec", "sudo"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                       "--super-prefix", "--config-env"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
SHELL_C = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
SUBST = "$(...)"                          # opaque stand-in for a $(...)
OPEN, CLOSE = "\0(", "\0)"                 # subshell scope markers from segments()
MAX_DEPTH = 3                             # nested shell/eval bodies scanned

ALTERNATIVES = (
    "refs/stash is ONE stack shared by every worktree of this repo, so a pop "
    "takes whatever is on top (routinely a parallel agent's WIP), and a stash "
    "on a clean tree saves nothing, so the pop after it is always someone "
    "else's entry. Safe alternatives: (1) a scratch commit on your "
    "own branch: `git add -A && git commit -m wip`, later "
    "`git reset --soft HEAD~1`; "
    "(2) run the base in its own checkout: `git worktree add --detach "
    "<scratchpad>/<unit>/base <base-sha>`; (3) read a base file: `git show "
    "<sha>:<path> > <scratchpad>/<unit>/<file>`; (4) copy files to your scratchpad "
    "and back. `git stash list` and `git stash show` stay allowed. A human who "
    "really needs a stash can run it in their own terminal."
)


class Unresolved(Exception):
    """The repo a stash targets cannot be determined statically."""


def close_paren(s, i):
    """Index of the `)` closing the `$(` whose body starts at `i` (quote- and
    nesting-aware), or len(s) when it is never closed."""
    depth, q, n = 1, None, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and q != "'":
            i += 2
            continue
        if q:
            if c == q:
                q = None
        elif c in ("'", '"'):
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def segments(s):
    """Top-level command segments: quote-aware, comments and heredoc bodies
    dropped, split on unquoted ; & | ( ) ` and newlines. `( )` and `$(...)`
    bodies are bracketed by OPEN / CLOSE so judge() can scope their cd."""
    segs, buf, pending = [], [], []
    i, n, q = 0, len(s), None
    while i < n:
        c = s[i]
        if q:
            if c == "$" and q == '"' and s.startswith("$(", i):
                j = close_paren(s, i + 2)
                segs += [OPEN, *segments(s[i + 2:j]), CLOSE]
                buf.append(SUBST)
                i = j + 1
                continue
            buf.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                buf.append(s[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if s[i + 1] == "\n":          # line continuation joins tokens
                i += 2
                continue
            buf += [c, s[i + 1]]
            i += 2
            continue
        if c in ("'", '"'):
            q = c
            buf.append(c)
            i += 1
            continue
        if c == "#" and (not buf or buf[-1] in " \t"):
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c == "<" and s.startswith("<<", i) and not s.startswith("<<<", i):
            m = HEREDOC.match(s, i)
            if m:
                pending.append((m.group(1) == "-", m.group(2) or m.group(3) or m.group(4)))
                buf.append(m.group(0))
                i = m.end()
                continue
        if c == "$" and s.startswith("$(", i):
            j = close_paren(s, i + 2)     # body runs first; outer sees a token
            segs += [OPEN, *segments(s[i + 2:j]), CLOSE]
            buf.append(SUBST)
            i = j + 1
            continue
        if c in ";&|()`\n":
            segs.append("".join(buf))
            if c in "()":
                segs.append(OPEN if c == "(" else CLOSE)
            buf = []
            i += 1
            if c == "\n" and pending:     # skip heredoc bodies
                for strip_tabs, delim in pending:
                    while i < n:
                        end = s.find("\n", i)
                        line = s[i:] if end < 0 else s[i:end]
                        i = n if end < 0 else end + 1
                        if (line.lstrip("\t") if strip_tabs else line) == delim:
                            break
                pending = []
            continue
        buf.append(c)
        i += 1
    segs.append("".join(buf))
    return segs


def expand(word, env):
    """Expand ~ and $VAR in a path word; raise Unresolved for unknown vars."""
    if word == "~" or word.startswith("~/"):
        word = env.get("HOME", os.path.expanduser("~")) + word[1:]

    def sub(m):
        name = m.group(1) or m.group(2)
        if name not in env:
            raise Unresolved(f"${name} is not set in this command")
        return env[name]

    if "`" in word or "$(" in word:
        raise Unresolved(f"{word!r} is a command substitution")
    if "$" in VAR.sub("", word):
        raise Unresolved(f"{word!r} has an expansion that is not tracked")
    return VAR.sub(sub, word)


def resolve(path, cwd, env):
    """Absolute, normalised form of a path word relative to `cwd`."""
    path = expand(path, env)
    if os.path.isabs(path):
        return os.path.normpath(path)
    if cwd is None:
        raise Unresolved("the working directory is unknown at this point")
    return os.path.normpath(os.path.join(cwd, path))


def git(args, git_env):
    """stdout of `git <args>`, or None on any failure."""
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True,
                           timeout=5, env=git_env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def shared_reason(target, git_dir, git_env):
    """Why a stash at `target` would touch a shared stack, else None."""
    base = ["-C", target] + ([f"--git-dir={git_dir}"] if git_dir else [])
    out = git(base + ["rev-parse", "--path-format=absolute",
                      "--git-dir", "--git-common-dir", "--show-toplevel"], git_env)
    if not out:
        return None                       # not a repo: the stash fails on its own
    lines = out.splitlines()
    own, common = lines[0], lines[1]
    top = lines[2] if len(lines) > 2 else target
    if own != common:
        return f"{top} is a linked worktree"
    listing = git(base + ["worktree", "list", "--porcelain"], git_env)
    if listing is None:
        return (f"could not list the worktrees of {top} (git worktree list "
                f"failed), and an unresolvable mutating stash is denied by default")
    live = [b for b in listing.split("\n\n")
            if b.startswith("worktree ") and "\nprunable" not in b]
    if len(live) > 1:
        return (f"{top} is the main checkout with {len(live) - 1} other "
                f"worktree(s) attached")
    return None


def strip_prefix(toks, env):
    """Drop shell keywords, wrappers and VAR=val assignments before the
    command word. Returns (remaining tokens, scoped assignments, the last
    `env -C` / `--chdir` directory word or None)."""
    scoped, chdir = {}, None
    while toks:
        t = toks[0]
        if t in PREFIX_WORDS:
            toks = toks[1:]
        elif os.path.basename(t) == "env":
            toks = toks[1:]
            while toks and toks[0].startswith("-"):
                o = toks[0]
                if o in ("-C", "--chdir"):
                    chdir = toks[1] if len(toks) > 1 else None
                elif o.startswith("--chdir="):
                    chdir = o.split("=", 1)[1]
                elif o.startswith("-C"):
                    chdir = o[2:]
                toks = toks[2:] if o in ("-u", "--unset", "-C", "--chdir") else toks[1:]
        elif ASSIGN.match(t):
            k, v = t.split("=", 1)
            scoped[k] = v
            toks = toks[1:]
        else:
            break
    return toks, scoped, chdir


def judge(cmd, cwd, env, depth=0):
    """Return a deny reason for the first mutating shared-stack stash in
    `cmd`, or None. Tracks cd and plain assignments across segments; a
    subshell's are undone when it closes."""
    scopes = []
    for seg in segments(cmd):
        if seg == OPEN:
            scopes.append((cwd, dict(env)))
            continue
        if seg == CLOSE:
            if scopes:
                cwd, env = scopes.pop()
            continue
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        toks, scoped, chdir = strip_prefix(toks, env)
        if not toks:
            for k, v in scoped.items():   # bare `A=b` sets shell state
                try:
                    env[k] = expand(v, env)
                except Unresolved:
                    env.pop(k, None)
            continue
        head = toks[0]
        if head == "export":
            for t in toks[1:]:
                if ASSIGN.match(t):
                    k, v = t.split("=", 1)
                    try:
                        env[k] = expand(v, env)
                    except Unresolved:
                        env.pop(k, None)
            continue
        if head in ("cd", "pushd", "popd"):
            args = [a for a in toks[1:] if a == "-" or not a.startswith("-")]
            try:
                if head == "popd" or args[:1] == ["-"]:
                    raise Unresolved("cd -/popd target is not tracked")
                cwd = resolve(args[0], cwd, env) if args else env.get("HOME")
            except Unresolved:
                cwd = None
            if cwd is not None and not os.path.isdir(cwd):
                cwd = None                # the cd fails; where the shell stays is untracked
            continue
        run_cwd = cwd
        if chdir is not None:
            try:
                run_cwd = resolve(chdir, cwd, env)
            except Unresolved:
                run_cwd = None
        name = os.path.basename(head)
        if name in SHELLS or name == "eval":
            if depth >= MAX_DEPTH:
                if "stash" in " ".join(toks[1:]):
                    return ("[stash-guard] Blocked a shell body that may run "
                            "`git stash`: it is nested too deeply to inspect, "
                            "and an uninspected mutating stash is denied by "
                            "default. " + ALTERNATIVES)
                continue
            body = None
            if name == "eval":
                body = " ".join(toks[1:])
            else:                         # -c, or a cluster like -lc / -ec
                k = next((k for k, t in enumerate(toks[1:], 1)
                          if SHELL_C.fullmatch(t)), None)
                body = toks[k + 1] if k is not None and k + 1 < len(toks) else None
            if body:
                reason = judge(body, run_cwd, dict(env), depth + 1)
                if reason:
                    return reason
            continue
        if name != "git":
            continue
        reason = judge_git(toks[1:], run_cwd, env, scoped)
        if reason:
            return reason
    return None


def judge_git(args, cwd, env, scoped=None):
    """Deny reason for one git invocation's argv (after `git`), or None.
    `env` expands the argv (the shell expands words before a command-scoped
    VAR=val takes effect); `scoped` only reaches git's own environment."""
    target, git_dir, i = cwd, None, 0
    pending_unresolved = None
    while i < len(args) and args[i].startswith("-"):
        opt = args[i]
        name, _, inline = opt.partition("=")
        value = None
        if name in GIT_OPTS_WITH_VALUE:
            if inline:
                value = inline
                i += 1
            else:
                value = args[i + 1] if i + 1 < len(args) else ""
                i += 2
        else:
            i += 1
        try:
            if name == "-C" and value:
                target = resolve(value, target, env)
            elif name == "--git-dir" and value:
                git_dir = resolve(value, target, env)
        except Unresolved as e:
            pending_unresolved = str(e)
    if i >= len(args) or args[i] != "stash":
        return None
    sub = args[i + 1] if i + 1 < len(args) else "push"
    if sub in READ_ONLY:
        return None
    label = f"`git stash{'' if i + 1 >= len(args) else ' ' + sub}`"
    if pending_unresolved or target is None:
        why = pending_unresolved or "the working directory is unknown"
        return (f"[stash-guard] Blocked {label}: could not resolve which repo "
                f"it targets ({why}), and an unresolvable mutating stash is "
                f"denied by default. " + ALTERNATIVES)
    git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        if k in (scoped or {}):
            try:
                git_env[k] = expand(scoped[k], env)
            except Unresolved as e:
                return (f"[stash-guard] Blocked {label}: could not resolve which "
                        f"repo it targets ({e}), and an unresolvable mutating "
                        f"stash is denied by default. " + ALTERNATIVES)
        elif k in env:
            git_env[k] = env[k]
    if git_dir is None and "GIT_DIR" in git_env:
        git_dir = git_env.pop("GIT_DIR")
    reason = shared_reason(target, git_dir, git_env)
    if not reason:
        return None
    return f"[stash-guard] Blocked {label}: {reason}. " + ALTERNATIVES


def main():
    """Read a PreToolUse payload on stdin; print a deny decision if needed."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str) or "stash" not in cmd:
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    reason = judge(cmd, cwd, dict(os.environ))
    if reason:
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "permissionDecision": "deny",
                                          "permissionDecisionReason": reason}},
                  sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
