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
then any `cd` earlier in the chain (scoped to its `( )` / `$(...)` / backquote
subshell, pipeline element or backgrounded list; unknown once an and-or list
it may not have run in ends), then `env -C` / `sudo -D` (each relative to the
last), then the invocation's own `-C` / `--git-dir`; VAR=val prefixes and
`env -u` / `-i` shape the GIT_* vars the probe sees, and an export that may
not have run leaves its var uncertain. Quote-, comment- and heredoc-aware, so
"git stash" inside an echo/grep/commit message or a data heredoc is not a
stash (but a `$(...)` in an unquoted heredoc is). `bash -c` / `sh -c` bodies
(with their prefix assignments and `env -C`), a heredoc fed to a shell (in
that command's context), `eval` bodies (in the caller's scope, so their
cd/exports carry on) and `$(...)` / backquotes (also inside double quotes) are
scanned too; a body is judged both with the caller's $VARs expanded first and
with the body expanding them itself, since quote removal hides which happens.
`env` / shells / `eval` / wrappers (`time -p`, `command -p`, `sudo -u X`,
`exec -a X`) match by basename like git, their options consumed.
A mutating stash whose target cannot be resolved (an unknown $VAR, a `$(...)`,
a `cd` into a missing directory, a failed or timed-out repo or worktree probe,
shell bodies nested past the scan depth) is denied.
Known gaps: git run under `timeout` / `nice` / `xargs`, and a heredoc piped
into a shell (`cat <<EOF | bash`), are not seen; a cd inside a `{ ...; }`
group used as a pipeline element is not undone. A crash exits non-zero,
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
                "until"}
# Wrappers that run the next word as the command (matched by basename), with
# the short options that take a value; their other options are flags.
WRAPPERS = {"time": "", "nohup": "", "command": "", "builtin": "",
            "exec": "a", "sudo": "ugpCDrtUTR"}
SUDO_LONG_WITH_VALUE = {"--user", "--group", "--prompt", "--close-from",
                        "--chdir", "--role", "--type", "--other-user",
                        "--command-timeout", "--host"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                       "--super-prefix", "--config-env"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"<<(-?)[ \t]*((?:'[^']*'|\"[^\"]*\"|\\.|[^\s;&|<>()'\"\\])+)")
NOT_A_REPO = re.compile(r"not a git repository|cannot change to|must be run in a work tree")
SHELL_C = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
SUBST = "$(...)"                          # opaque stand-in for a $(...)
OPEN, CLOSE = "\0(", "\0)"                 # subshell scope markers from segments()
AND, OR, SEP = "\0&", "\0|", "\0;"         # && / || / (; newline) from segments()
PIPE, BG = "\0p", "\0b"                    # | or |& / a lone & from segments()
UNCERTAIN = "\0?"                         # env value: a step that may not have run set it
MAX_DEPTH = 3                             # nested shell/eval bodies scanned
GIT_TARGET = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")

ALTERNATIVES = (
    "refs/stash is ONE stack shared by every worktree of this repo, so a pop "
    "takes whatever is on top (routinely a parallel agent's WIP), and a stash "
    "on a clean tree saves nothing, so the pop after it is always someone "
    "else's entry. Safe alternatives: (1) a scratch commit on your "
    "own branch: `git add <only the paths you may change>`, check "
    "`git diff --cached --stat`, `git commit -m wip`, later "
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
    """Index of the `)` closing the `$(` whose body starts at `i` (quote-,
    comment- and nesting-aware), or len(s) when it is never closed."""
    depth, q, n, start = 1, None, len(s), i
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
        elif c == "#" and (i == start or s[i - 1] in " \t\n;&|()"):
            nl = s.find("\n", i)         # a comment's `)` closes nothing
            if nl < 0:
                return n
            i = nl
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def close_backtick(s, i):
    """Index of the backtick closing the one whose body starts at `i`, or
    len(s) when it is never closed."""
    n = len(s)
    while i < n:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i
        i += 1
    return n


def substitution(s, i):
    """(body, end) of the `$(...)` or backquote substitution at `i`."""
    if s[i] == "`":
        j = close_backtick(s, i + 1)
        return re.sub(r"\\([$`\\])", r"\1", s[i + 1:j]), j + 1
    j = close_paren(s, i + 2)
    return s[i + 2:j], j + 1


def expansions(text):
    """Bodies of the `$(...)` / backquote substitutions in `text`. The shell
    runs these in an unquoted heredoc even when its data is not a script."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`" or text.startswith("$(", i):
            body, i = substitution(text, i)
            out.append(body)
            continue
        i += 1
    return out


class Seg(str):
    """A command segment; `docs` lists its heredocs as (body, quoted)."""
    docs = ()


def heredoc_word(word):
    """The delimiter a heredoc word names, after quote removal."""
    try:
        return "".join(shlex.split(word, posix=True))
    except ValueError:
        return re.sub(r"[\\'\"]", "", word)


def segments(s):
    """Top-level command segments: quote-aware, comments dropped, split on
    unquoted ; & | ( ) and newlines. `( )`, `$(...)` and backquote bodies are
    bracketed by OPEN / CLOSE so judge() can scope their cd; `&&` / `||` emit
    AND / OR, `|` / `|&` PIPE, a lone `&` BG and `;` / newline SEP, so scan()
    can tell when a cd might not have run or ran in a subshell. Each segment
    is a Seg whose `docs` collects its heredoc bodies, so scan() judges them
    in the context of the command that owns them."""
    segs, buf, pending, docs = [], [], [], []
    i, n, q = 0, len(s), None
    while i < n:
        c = s[i]
        if q:
            if q == '"' and (c == "`" or s.startswith("$(", i)):
                body, i = substitution(s, i)
                segs += [OPEN, *segments(body), CLOSE]
                buf.append(SUBST)
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
                pending.append((m.group(1) == "-", heredoc_word(m.group(2)),
                                any(ch in m.group(2) for ch in "'\"\\"), docs))
                buf.append(m.group(0))
                i = m.end()
                continue
        if c == "`" or s.startswith("$(", i):
            body, i = substitution(s, i)  # body runs first; outer sees a token
            segs += [OPEN, *segments(body), CLOSE]
            buf.append(SUBST)
            continue
        if (c == "&" and (s.startswith("&>", i) or (buf and buf[-1] in "<>"))) \
                or (c == "|" and buf and buf[-1] == ">"):
            buf.append(c)                 # a redirection, not a separator
            i += 1
            continue
        if c in ";&|()\n":
            seg = Seg("".join(buf))
            seg.docs = docs
            segs.append(seg)
            buf, docs = [], []
            if c in "()":
                segs.append(OPEN if c == "(" else CLOSE)
            elif c in "&|" and s.startswith(c * 2, i):
                segs.append(AND if c == "&" else OR)
                i += 1
            elif c == "|":
                segs.append(PIPE)
                if s.startswith("|&", i):
                    i += 1                # |& is a pipe too
            else:
                segs.append(BG if c == "&" else SEP)
            i += 1
            if c == "\n" and pending:     # heredoc bodies follow this line
                for strip_tabs, delim, quoted, owner in pending:
                    body = []
                    while i < n:
                        end = s.find("\n", i)
                        line = s[i:] if end < 0 else s[i:end]
                        i = n if end < 0 else end + 1
                        if (line.lstrip("\t") if strip_tabs else line) == delim:
                            break
                        body.append(line)
                    owner.append(("\n".join(body), quoted))
                pending = []
            continue
        buf.append(c)
        i += 1
    seg = Seg("".join(buf))
    seg.docs = docs
    segs.append(seg)
    return segs


def expand(word, env):
    """Expand ~ and $VAR in a path word; raise Unresolved for unknown vars."""
    if word == "~" or word.startswith("~/"):
        home = env.get("HOME", os.path.expanduser("~"))
        if home == UNCERTAIN:
            raise Unresolved("$HOME may or may not have been changed")
        word = home + word[1:]

    def sub(m):
        name = m.group(1) or m.group(2)
        if name not in env:
            raise Unresolved(f"${name} is not set in this command")
        if env[name] == UNCERTAIN:
            raise Unresolved(f"${name} is set by a step that may not have run")
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
    """The finished `git <args>`, or None when git could not run or timed out."""
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=5, env={**git_env, "LC_ALL": "C",
                                              "LANGUAGE": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None


def shared_reason(target, git_dir, git_env):
    """Why a stash at `target` would touch a shared stack, else None."""
    base = ["-C", target] + ([f"--git-dir={git_dir}"] if git_dir else [])
    r = git(base + ["rev-parse", "--path-format=absolute",
                    "--git-dir", "--git-common-dir", "--show-toplevel"], git_env)
    if r is not None and r.returncode and NOT_A_REPO.search(r.stderr):
        return None                       # not a repo: the stash fails on its own
    lines = r.stdout.splitlines() if r is not None and not r.returncode else []
    if len(lines) < 2:
        return (f"could not probe {target} (git rev-parse failed or timed "
                f"out), and an unresolvable mutating stash is denied by default")
    own, common = lines[0], lines[1]
    top = lines[2] if len(lines) > 2 else target
    if own != common:
        return f"{top} is a linked worktree"
    listing = git(base + ["worktree", "list", "--porcelain"], git_env)
    if listing is None or listing.returncode:
        return (f"could not list the worktrees of {top} (git worktree list "
                f"failed), and an unresolvable mutating stash is denied by default")
    live = [b for b in listing.stdout.split("\n\n")
            if b.startswith("worktree ") and "\nprunable" not in b]
    if len(live) > 1:
        return (f"{top} is the main checkout with {len(live) - 1} other "
                f"worktree(s) attached")
    return None


def strip_prefix(toks, env):
    """Drop shell keywords, wrappers and VAR=val assignments before the
    command word. Returns (remaining tokens, scoped assignments, the
    `env -C` / `--chdir` directory words in order). A scoped value of None
    means `env -u` / `env -i` removed that variable."""
    scoped, chdirs = {}, []

    def env_opt(opt, val):
        if opt == "u":
            scoped[val] = None
        elif opt == "C":
            chdirs.append(val)
        else:                             # -i: nothing inherited survives
            scoped.clear()
            scoped.update(dict.fromkeys((*env, *GIT_TARGET)))

    while toks:
        t = toks[0]
        if t in PREFIX_WORDS:
            toks = toks[1:]
        elif os.path.basename(t) in WRAPPERS:
            wrapper, toks = os.path.basename(t), toks[1:]
            takes = WRAPPERS[wrapper]
            while toks and toks[0].startswith("-") and toks[0] != "-":
                o, toks = toks[0], toks[1:]
                if o == "--":
                    break
                if o.startswith("--"):
                    name, eq, val = o.partition("=")
                    if wrapper == "sudo" and name in SUDO_LONG_WITH_VALUE:
                        if not eq:
                            val, toks = (toks[0], toks[1:]) if toks else ("", toks)
                        if name == "--chdir":
                            chdirs.append(val)
                    continue
                for k in range(1, len(o)):        # a short cluster like -Eu X
                    if o[k] in takes:
                        val = o[k + 1:]
                        if not val:
                            val, toks = (toks[0], toks[1:]) if toks else ("", toks)
                        if wrapper == "sudo" and o[k] == "D":
                            chdirs.append(val)
                        break
        elif os.path.basename(t) == "env":
            toks = toks[1:]
            while toks and toks[0].startswith("-"):
                o, toks = toks[0], toks[1:]
                if o == "--":
                    break
                if o in ("-", "--ignore-environment"):
                    env_opt("i", None)
                elif o.startswith("--"):
                    name, eq, val = o.partition("=")
                    if name in ("--unset", "--chdir"):
                        if not eq:
                            val, toks = (toks[0], toks[1:]) if toks else ("", toks)
                        env_opt("u" if name == "--unset" else "C", val)
                    continue
                for k in range(1, len(o)):        # a short cluster like -iu X
                    if o[k] == "i":
                        env_opt("i", None)
                    elif o[k] in "uC":
                        val = o[k + 1:]
                        if not val:
                            val, toks = (toks[0], toks[1:]) if toks else ("", toks)
                        env_opt(o[k], val)
                        break
        elif ASSIGN.match(t):
            k, v = t.split("=", 1)
            scoped[k] = v
            toks = toks[1:]
        else:
            break
    return toks, scoped, chdirs


def with_scoped(env, scoped):
    """`env` as the command sees it once its VAR=val prefixes and env(1)
    unsets apply. Raises Unresolved when a GIT_* target var cannot expand."""
    out = dict(env)
    for k, v in scoped.items():
        if v is None:
            out.pop(k, None)
            continue
        try:
            out[k] = expand(v, env)
        except Unresolved:
            if k in GIT_TARGET:
                raise
            out[k] = UNCERTAIN
    return out


def substitute(body, env):
    """`body` with its tracked $VAR / ${VAR} expansions applied, the way the
    caller's shell expands a double-quoted eval / -c body before running it."""
    def sub(m):
        v = env.get(m.group(1) or m.group(2), UNCERTAIN)
        return m.group(0) if v == UNCERTAIN else v
    return VAR.sub(sub, body)


def variants(body, env):
    """The scripts `body` may be once the shell runs it. Quote removal hides
    whether the caller expanded its $VARs first (double quotes) or the body
    expands them itself, after its own assignments (single quotes), so both
    readings are judged."""
    pre = substitute(body, env)
    return [pre] if pre == body else [pre, body]


def merge(cwd, env, cwd2, env2):
    """State after a step that may or may not have taken effect: a cwd the
    two disagree on is unknown (None), an env var they disagree on UNCERTAIN."""
    out = dict(env)
    for k in set(env) | set(env2):
        if env.get(k) != env2.get(k):
            out[k] = UNCERTAIN
    return (cwd if cwd == cwd2 else None), out


def may_stash(text):
    """Whether `text` could spell `stash` once quotes are removed."""
    return "stash" in re.sub(r"[\"'\\$\n]", "", text)


def judge(cmd, cwd, env, depth=0):
    """Return a deny reason for the first mutating shared-stack stash in
    `cmd`, or None."""
    return scan(cmd, cwd, env, depth)[0]


def scan(cmd, cwd, env, depth):
    """(deny reason or None, cwd, env) after `cmd`. Tracks cd and plain
    assignments across segments; a subshell's are undone when it closes.
    A step in an and-or list may not have run, so once the list ends (or an
    `||` follows it) a cwd it changed is unknown and an env var it changed
    UNCERTAIN. Pipeline elements before the last and a backgrounded list run
    in subshells; the last element does in bash but not zsh, so its changes
    are uncertain too."""
    scopes = []
    lst = (cwd, dict(env))                # state where this and-or list began
    elem = lst                            # ... and this pipeline element
    cond = piped = False
    for seg in segments(cmd) + [SEP]:
        if seg == OPEN:
            scopes.append((cwd, dict(env), lst, elem, cond, piped))
            lst = elem = (cwd, dict(env))
            cond = piped = False
            continue
        if seg == CLOSE:
            if scopes:
                cwd, env, lst, elem, cond, piped = scopes.pop()
            continue
        if seg == PIPE:                   # the left side ran in a subshell
            cwd, env, piped = elem[0], dict(elem[1]), True
            continue
        if seg in (AND, OR, SEP, BG):
            if piped:
                cwd, env = merge(cwd, env, *elem)
                piped = False
            if seg == BG:                 # the whole list ran in the background
                cwd, env = lst[0], dict(lst[1])
            elif seg == OR or (seg == SEP and cond):
                cwd, env = merge(cwd, env, *lst)
            if seg in (SEP, BG):
                lst, cond = (cwd, dict(env)), False
            else:
                cond = True
            elem = (cwd, dict(env))
            continue
        for doc, quoted in seg.docs:      # an unquoted heredoc runs its $(...)
            for body in [] if quoted else expansions(doc):
                reason = judge(body, cwd, dict(env), depth)
                if reason:
                    return reason, cwd, env
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        toks, scoped, chdirs = strip_prefix(toks, env)
        if not toks:
            for k, v in scoped.items():   # bare `A=b` sets shell state
                if v is None:
                    continue
                try:
                    env[k] = expand(v, env)
                except Unresolved:
                    env[k] = UNCERTAIN
            continue
        head = toks[0]
        if head == "export":
            for t in toks[1:]:
                if ASSIGN.match(t):
                    k, v = t.split("=", 1)
                    try:
                        env[k] = expand(v, env)
                    except Unresolved:
                        env[k] = UNCERTAIN
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
        try:
            for d in chdirs:              # each env -C is relative to the last
                run_cwd = resolve(d, run_cwd, env)
        except Unresolved:
            run_cwd = None
        name = os.path.basename(head)
        if name in SHELLS or name == "eval":
            if name == "eval":
                raw = " ".join(toks[1:])
            else:                         # -c, or a cluster like -lc / -ec
                k = next((k for k, t in enumerate(toks[1:], 1)
                          if SHELL_C.fullmatch(t)), None)
                raw = toks[k + 1] if k is not None and k + 1 < len(toks) else None
            if raw:
                scripts = variants(raw, env)  # `eval "$CMD"` runs what $CMD holds
            elif name == "eval":
                continue
            else:                         # `bash <<EOF`: the heredoc is the script
                scripts = [s for doc, quoted in seg.docs
                           for s in ([doc] if quoted else variants(doc, env))]
            if not scripts:
                continue
            if depth >= MAX_DEPTH:
                if any(may_stash(s) for s in scripts):
                    return ("[stash-guard] Blocked a shell body that may run "
                            "`git stash`: it is nested too deeply to inspect, "
                            "and an uninspected mutating stash is denied by "
                            "default. " + ALTERNATIVES), cwd, env
                continue
            try:
                body_env = with_scoped(env, scoped)
            except Unresolved as e:
                if not any(may_stash(s) for s in scripts):
                    continue
                return ("[stash-guard] Blocked a shell body that may run "
                        f"`git stash`: could not resolve which repo it targets "
                        f"({e}), and an unresolvable mutating stash is denied "
                        "by default. " + ALTERNATIVES), cwd, env
            if name != "eval":            # a child shell: its state is its own
                for s in scripts:
                    reason = judge(s, run_cwd, dict(body_env), depth + 1)
                    if reason:
                        return reason, cwd, env
                continue
            outs = []
            for s in scripts:
                reason, c, e = scan(s, cwd, dict(body_env), depth + 1)
                if reason:
                    return reason, cwd, env
                outs.append((c, e))
            cwd, after = outs[0]
            for c, e in outs[1:]:         # readings that disagree leave it uncertain
                cwd, after = merge(cwd, after, c, e)
            for k in scoped:              # `A=b eval ...` sets A only for the eval
                if k in env:
                    after[k] = env[k]
                else:
                    after.pop(k, None)
            env = after                   # eval runs here: its cd/exports stay
            continue
        if name != "git":
            continue
        reason = judge_git(toks[1:], run_cwd, env, scoped)
        if reason:
            return reason, cwd, env
    if scopes:                            # an unclosed ( ends with the command
        cwd, env = scopes[0][:2]
    return None, cwd, env


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
    try:
        seen = with_scoped(env, scoped or {})
    except Unresolved as e:
        return (f"[stash-guard] Blocked {label}: could not resolve which "
                f"repo it targets ({e}), and an unresolvable mutating "
                f"stash is denied by default. " + ALTERNATIVES)
    for k in GIT_TARGET:
        if seen.get(k) == UNCERTAIN:
            return (f"[stash-guard] Blocked {label}: could not resolve which "
                    f"repo it targets (${k} is set by a step that may not "
                    f"have run), and an unresolvable mutating stash is denied "
                    f"by default. " + ALTERNATIVES)
        if k in seen:
            git_env[k] = seen[k]
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
    if not isinstance(cmd, str):
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
