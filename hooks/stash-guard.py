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

The target is resolved per segment: the hook's cwd, then any `cd` earlier in
the chain, then the invocation's own `-C` / `--git-dir`. Quote-, comment- and
heredoc-aware, so "git stash" inside an echo/grep/commit message is not a
stash. `bash -c` / `sh -c` / `eval` bodies and `$(...)` are scanned too. A
mutating stash whose target cannot be resolved (an unknown $VAR) is denied.
Known gaps: `$(git stash ...)` nested inside a double-quoted string, and git
run under `timeout` / `nice` / `xargs`, are not seen. A crash exits non-zero,
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
VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

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
    pass


def segments(s):
    """Top-level command segments: quote-aware, comments and heredoc bodies
    dropped, split on unquoted ; & | ( ) ` and newlines."""
    segs, buf, pending = [], [], []
    i, n, q = 0, len(s), None
    while i < n:
        c = s[i]
        if q:
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
        if c in ";&|()`\n":
            segs.append("".join(buf))
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
    return VAR.sub(sub, word)


def resolve(path, cwd, env):
    path = expand(path, env)
    if os.path.isabs(path):
        return os.path.normpath(path)
    if cwd is None:
        raise Unresolved("the working directory is unknown at this point")
    return os.path.normpath(os.path.join(cwd, path))


def git(args, git_env):
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
    listing = git(base + ["worktree", "list", "--porcelain"], git_env) or ""
    live = [b for b in listing.split("\n\n")
            if b.startswith("worktree ") and "\nprunable" not in b]
    if len(live) > 1:
        return (f"{top} is the main checkout with {len(live) - 1} other "
                f"worktree(s) attached")
    return None


def strip_prefix(toks, env):
    """Drop shell keywords, wrappers and VAR=val assignments before the
    command word. Returns (remaining tokens, scoped assignments)."""
    scoped = {}
    while toks:
        t = toks[0]
        if t in PREFIX_WORDS:
            toks = toks[1:]
        elif t == "env":
            toks = toks[1:]
            while toks and toks[0].startswith("-"):
                toks = toks[2:] if toks[0] in ("-u", "--unset", "-C", "--chdir") else toks[1:]
        elif ASSIGN.match(t):
            k, v = t.split("=", 1)
            scoped[k] = v
            toks = toks[1:]
        else:
            break
    return toks, scoped


def judge(cmd, cwd, env, depth=0):
    """Return a deny reason for the first mutating shared-stack stash in
    `cmd`, or None. Tracks cd and plain assignments across segments."""
    for seg in segments(cmd):
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        toks, scoped = strip_prefix(toks, env)
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
            continue
        if depth < 3 and (head in SHELLS or head == "eval"):
            body = None
            if head == "eval":
                body = " ".join(toks[1:])
            elif "-c" in toks[1:]:
                k = toks.index("-c")
                body = toks[k + 1] if k + 1 < len(toks) else None
            if body:
                reason = judge(body, cwd, dict(env), depth + 1)
                if reason:
                    return reason
            continue
        if os.path.basename(head) != "git":
            continue
        reason = judge_git(toks[1:], cwd, {**env, **scoped})
        if reason:
            return reason
    return None


def judge_git(args, cwd, env):
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
        if k in env and k not in os.environ:
            git_env[k] = env[k]
    if git_dir is None and "GIT_DIR" in git_env:
        git_dir = git_env.pop("GIT_DIR")
    reason = shared_reason(target, git_dir, git_env)
    if not reason:
        return None
    return f"[stash-guard] Blocked {label}: {reason}. " + ALTERNATIVES


def main():
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
