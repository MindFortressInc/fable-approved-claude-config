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
or `if` / loop / `case` branch it may not have run in ends; `$PWD` / `$OLDPWD`
follow it), then `env -C` / `sudo -D` (each relative to the last), then the
invocation's own `-C` (all followed through symlinks the way chdir does) and
`--git-dir` / `--work-tree` (left for git to resolve after every `-C`);
VAR=val prefixes, `unset` and `env -u` / `-i` shape the GIT_* vars the probe
sees, a plain assignment reaches git or a child shell only once exported, and
an export that may not have run leaves its var uncertain. Quote-, comment-,
redirection- and heredoc-aware, so "git stash" inside an echo/grep/commit
message or a data heredoc is not a stash (but a `$(...)` in an unquoted
heredoc is), and `2>/dev/null git stash` is. `bash -c` / `sh -c` bodies
(with their prefix assignments and `env -C`), a heredoc or here string fed to
a shell (in that command's context), `eval` bodies (in the caller's scope, so
their cd/exports carry on), `$(...)` / backquotes (also inside double quotes)
and git aliases (`-c alias.x=...` or configured; `!` ones as shell bodies)
are scanned too; a body is judged both with the caller's $VARs expanded first
and with the body expanding them itself, since quote removal hides which
happens. `$VAR` command and subcommand words are expanded before matching.
`env` / shells / `eval` / wrappers (`time -p`, `command -p`, `sudo -u X`,
`exec -a X`, `timeout`, `nice`, `stdbuf`, `ionice`, `setsid`, `xargs`) match
by basename like git, their options consumed.
A mutating stash whose target cannot be resolved (an unknown $VAR, a `$(...)`,
a `cd` into a missing directory, a failed or timed-out repo, worktree or alias
probe, an unresolvable command or subcommand word, shell bodies or aliases
nested past the scan depth) is denied.
Known gaps: a heredoc piped into a shell (`cat <<EOF | bash`) is not seen; a
cd inside a `{ ...; }` group used as a pipeline element is not undone, and a
loop body is scanned once, in the state its first pass sees. A crash exits
non-zero, which Claude Code treats as a non-blocking error (fail open), like the other
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
OPENERS = {"if", "while", "until", "for", "select", "case"}
CLOSERS = {"fi", "done", "esac"}
BLOCK_WORDS = OPENERS | CLOSERS | {"then", "do", "else", "elif", "{", "}", "!"}
SUDO_LONG_WITH_VALUE = {"--user", "--group", "--prompt", "--close-from",
                        "--chdir", "--role", "--type", "--other-user",
                        "--command-timeout", "--host"}
# Wrappers that run a later word as the command (matched by basename):
# (short options taking a value, long options taking a value, positional
# words before the command). Their other options are flags.
WRAPPERS = {"time": ("", (), 0), "nohup": ("", (), 0),
            "command": ("", (), 0), "builtin": ("", (), 0),
            "setsid": ("", (), 0), "exec": ("a", (), 0),
            "sudo": ("ugpCDrtUTR", SUDO_LONG_WITH_VALUE, 0),
            "timeout": ("sk", ("--signal", "--kill-after"), 1),
            "nice": ("n", ("--adjustment",), 0),
            "stdbuf": ("ioe", ("--input", "--output", "--error"), 0),
            "ionice": ("cnpt", ("--class", "--classdata", "--pid", "--pgid",
                                "--uid"), 0),
            "xargs": ("EIdLnPsa", ("--arg-file", "--delimiter", "--max-lines",
                                   "--max-args", "--max-procs", "--max-chars",
                                   "--process-slot-var"), 0)}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                       "--super-prefix", "--config-env"}
# Long-standing git builtins: git ignores an alias that shadows one, so these
# never need an alias lookup.
GIT_BUILTINS = set("""add am annotate apply archive bisect blame branch bundle
    cat-file checkout cherry cherry-pick clean clone commit config
    count-objects describe diff fetch for-each-ref format-patch fsck gc grep
    hash-object help init log ls-files ls-remote ls-tree merge merge-base mv
    name-rev notes prune pull push range-diff rebase reflog remote reset
    restore revert rev-list rev-parse rm shortlog show show-branch show-ref
    sparse-checkout status switch symbolic-ref tag update-ref var version
    worktree""".split())
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIR_OP = re.compile(r"&>>?|<<<|<<-|<<|<>|<&|<|>>|>&|>\||>")
FD_WORD = re.compile(r"\d+|\{[A-Za-z_][A-Za-z0-9_]*\}")
HEREDOC = re.compile(r"<<(-?)[ \t]*((?:'[^']*'|\"[^\"]*\"|\\.|[^\s;&|<>()'\"\\])+)")
NOT_A_REPO = re.compile(r"not a git repository|cannot change to|must be run in a work tree")
SHELL_C = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
SUBST = "$(...)"                          # opaque stand-in for a $(...)
OPEN, CLOSE = "\0(", "\0)"                 # subshell scope markers from segments()
AND, OR, SEP = "\0&", "\0|", "\0;"         # && / || / (; newline) from segments()
PIPE, BG = "\0p", "\0b"                    # | or |& / a lone & from segments()
UNCERTAIN = "\0?"                         # env value: a step that may not have run set it
LOCAL = "\0l"                             # env key prefix: that var is set but not exported
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


def words(seg):
    """(words, here strings) of a segment after quote removal. Redirections
    and their operands are dropped wherever they sit (`2>/dev/null git stash`
    still runs git), each `<<<` operand is kept as a here string, and a
    heredoc operator's delimiter word is dropped (its body is in seg.docs)."""
    out, here, raw, pending = [], [], [], None
    i, n, q = 0, len(seg), None

    def flush():
        nonlocal raw, pending
        if not raw:
            return
        w = "".join(shlex.split("".join(raw), posix=True))
        raw = []
        if pending is None:
            out.append(w)
        elif pending == "<<<":
            here.append(w)
        pending = None

    while i < n:
        c = seg[i]
        if q:
            raw.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                raw.append(seg[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            raw += [c, seg[i + 1]]
            i += 2
            continue
        if c in ("'", '"'):
            q = c
            raw.append(c)
        elif c in " \t\n":
            flush()
        elif c in "<>" or seg.startswith("&>", i):
            if FD_WORD.fullmatch("".join(raw)):
                raw = []                  # `2>` / `{fd}>`: the fd is not a word
            flush()
            pending = REDIR_OP.match(seg, i).group(0)
            i += len(pending)
            continue
        else:
            raw.append(c)
        i += 1
    if q:
        raise ValueError("No closing quotation")
    flush()
    return out, here


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


def expand_word(word, env):
    """The words a command or subcommand word becomes once its tracked $VARs
    expand (split on whitespace, the way an unquoted expansion is; an empty
    one vanishes). Raises Unresolved when it cannot be expanded."""
    if "$" not in word and "`" not in word:
        return [word]
    return expand(word, env).split()


def resolve(path, cwd, env, physical=False):
    """Absolute form of a path word relative to `cwd`. `cd` is logical (`..`
    drops the last component, as bash's default `cd -L` does); a chdir(2)
    (git -C, env -C, sudo -D, cd -P) is `physical`: symlinks are followed
    before `..`, so `link/..` lands where the link points."""
    path = expand(path, env)
    if not os.path.isabs(path):
        if cwd is None:
            raise Unresolved("the working directory is unknown at this point")
        path = os.path.join(cwd, path)
    return os.path.realpath(path) if physical else os.path.normpath(path)


def git(args, git_env):
    """The finished `git <args>`, or None when git could not run or timed out."""
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=5, env={**git_env, "LC_ALL": "C",
                                              "LANGUAGE": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None


def shared_reason(target, git_dir, work_tree, git_env):
    """Why a stash at `target` would touch a shared stack, else None."""
    base = ["-C", target] + ([f"--git-dir={git_dir}"] if git_dir else []) + \
        ([f"--work-tree={work_tree}"] if work_tree else [])
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
            takes, long_takes, positional = WRAPPERS[wrapper]
            while toks and toks[0].startswith("-") and toks[0] != "-":
                o, toks = toks[0], toks[1:]
                if o == "--":
                    break
                if o.startswith("--"):
                    name, eq, val = o.partition("=")
                    if name in long_takes:
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
            toks = toks[positional:]      # `timeout 10 git ...`: the duration
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


def exported_env(env, scoped):
    """The environment a child process (git, a child shell) inherits: what
    with_scoped() gives, minus the vars that were set but never exported.
    A command-scoped VAR=val always reaches the child; a var whose export may
    not have run is UNCERTAIN."""
    out = with_scoped(env, scoped)
    for k in [k for k in out if k.startswith(LOCAL)]:
        name, flag = k[len(LOCAL):], out.pop(k)
        if name in scoped or name not in out:
            continue
        if flag == UNCERTAIN:
            out[name] = UNCERTAIN
        else:
            out.pop(name)
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
    scopes, blocks = [], []               # blocks: state where each if/loop/case began
    lst = (cwd, dict(env))                # state where this and-or list began
    elem = lst                            # ... and this pipeline element
    cond = piped = False
    for seg in segments(cmd) + [SEP]:
        if seg == OPEN:
            scopes.append((cwd, dict(env), lst, elem, cond, piped, blocks))
            lst = elem = (cwd, dict(env))
            cond = piped = False
            blocks = []
            continue
        if seg == CLOSE:
            if scopes:
                cwd, env, lst, elem, cond, piped, blocks = scopes.pop()
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
            toks, herestrs = words(seg)
        except ValueError:
            toks, herestrs = seg.split(), []
        while toks and toks[0] in BLOCK_WORDS:
            w, toks = toks[0], toks[1:]
            if w in OPENERS:              # a branch or loop body may be skipped
                blocks.append((cwd, dict(env)))
            elif w in ("else", "elif") and blocks:
                cwd, env = merge(cwd, env, *blocks[-1])
            elif w in CLOSERS and blocks:
                cwd, env = merge(cwd, env, *blocks.pop())
        toks, scoped, chdirs = strip_prefix(toks, env)
        opaque = None
        if toks and ("$" in toks[0] or "`" in toks[0]):
            try:                          # `"$B" stash pop` runs what $B holds
                toks = expand_word(toks[0], env) + toks[1:]
                toks, more, dirs = strip_prefix(toks, env)
                scoped.update(more)
                chdirs += dirs
            except Unresolved as e:
                opaque = str(e)
        if not toks:
            for k, v in scoped.items():   # bare `A=b` sets shell state
                if v is None:
                    continue
                if k not in env:          # a new var stays unexported
                    env[LOCAL + k] = "1"
                try:
                    env[k] = expand(v, env)
                except Unresolved:
                    env[k] = UNCERTAIN
            continue
        head = toks[0]
        if head in ("export", "unset"):
            opts = "".join(t[1:] for t in toks[1:] if t.startswith("-"))
            for t in toks[1:]:
                k, eq, v = t.partition("=")
                if t.startswith("-") or not ASSIGN.match(k + "="):
                    continue
                if head == "unset":
                    if "f" not in opts:
                        env.pop(k, None)
                        env.pop(LOCAL + k, None)
                    continue
                if eq:
                    try:
                        env[k] = expand(v, env)
                    except Unresolved:
                        env[k] = UNCERTAIN
                if "n" in opts:           # export -n: set, but no longer exported
                    env[LOCAL + k] = "1"
                else:
                    env.pop(LOCAL + k, None)
            continue
        if head in ("cd", "pushd", "popd"):
            args = [a for a in toks[1:] if a == "-" or not a.startswith("-")]
            physical = any(a.startswith("-") and "P" in a for a in toks[1:]
                           if a not in args)
            old = cwd
            try:
                if head == "popd" or args[:1] == ["-"]:
                    raise Unresolved("cd -/popd target is not tracked")
                cwd = (resolve(args[0], cwd, env, physical) if args
                       else env.get("HOME"))
            except Unresolved:
                cwd = None
            if cwd is not None and not os.path.isdir(cwd):
                cwd = None                # the cd fails; where the shell stays is untracked
            if cwd is None:
                env["PWD"] = env["OLDPWD"] = UNCERTAIN
            else:                         # bash keeps $PWD / $OLDPWD in step
                env["PWD"], env["OLDPWD"] = cwd, (UNCERTAIN if old is None
                                                  else old)
            continue
        run_cwd = cwd
        try:
            for d in chdirs:              # each env -C is relative to the last
                run_cwd = resolve(d, run_cwd, env, physical=True)
        except Unresolved:
            run_cwd = None
        if opaque:                        # judge it as if the word expands to git
            if judge_git(toks[1:], run_cwd, env, scoped, depth):
                return ("[stash-guard] Blocked a command whose name could not "
                        f"be resolved ({opaque}): run as git, it is a mutating "
                        "stash on a shared stack, and an unresolvable mutating "
                        "stash is denied by default. " + ALTERNATIVES), cwd, env
            continue
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
            else:                         # `bash <<EOF` / `bash <<< ...`: stdin is the script
                scripts = [s for doc, quoted in seg.docs
                           for s in ([doc] if quoted else variants(doc, env))]
                scripts += [s for h in herestrs for s in variants(h, env)]
            if not scripts:
                continue
            if depth >= MAX_DEPTH:
                if any(may_stash(s) for s in scripts):
                    return ("[stash-guard] Blocked a shell body that may run "
                            "`git stash`: it is nested too deeply to inspect, "
                            "and an uninspected mutating stash is denied by "
                            "default. " + ALTERNATIVES), cwd, env
                continue
            try:                          # eval shares this shell; a child gets the exports
                body_env = (with_scoped if name == "eval" else exported_env)(env, scoped)
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
        reason = judge_git(toks[1:], run_cwd, env, scoped, depth)
        if reason:
            return reason, cwd, env
    if scopes:                            # an unclosed ( ends with the command
        cwd, env = scopes[0][:2]
    return None, cwd, env


def blocked(label, why):
    """The deny reason for a mutating stash whose target is unresolvable."""
    return (f"[stash-guard] Blocked {label}: could not resolve which repo it "
            f"targets ({why}), and an unresolvable mutating stash is denied "
            f"by default. " + ALTERNATIVES)


def judge_git(args, cwd, env, scoped=None, depth=0):
    """Deny reason for one git invocation's argv (after `git`), or None.
    `env` expands the argv (the shell expands words before a command-scoped
    VAR=val takes effect); `scoped` only reaches git's own environment.
    `--git-dir` / `--work-tree` stay as written: git resolves a relative one
    after every `-C`, and so does the probe, which gets the same options."""
    scoped = scoped or {}
    target, git_dir, work_tree, configs, i = cwd, None, None, [], 0
    pending_unresolved = config_unresolved = None
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
                target = resolve(value, target, env, physical=True)
            elif name == "--git-dir" and value:
                git_dir = expand(value, env)
            elif name == "--work-tree" and value:
                work_tree = expand(value, env)
            elif name == "-c" and value:
                try:
                    configs.append(expand(value, env))
                except Unresolved as e:
                    config_unresolved = str(e)
        except Unresolved as e:
            pending_unresolved = str(e)
    if i >= len(args):
        return None
    opaque = None
    try:                                  # `git "$S" pop` runs what $S holds
        rest = expand_word(args[i], env) + args[i + 1:]
    except Unresolved as e:
        rest, opaque = ["stash"] + args[i + 1:], str(e)
    if not rest:
        return None
    try:
        seen = exported_env(env, scoped)
    except Unresolved as e:
        seen = e
    git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if not isinstance(seen, Unresolved):
        git_env.update({k: v for k, v in seen.items()
                        if k.startswith("GIT_CONFIG") and v != UNCERTAIN})
    if rest[0] != "stash":
        return judge_alias(rest, target, git_dir, work_tree, configs,
                           config_unresolved, seen, git_env, depth)
    sub = rest[1] if len(rest) > 1 else "push"
    try:
        sub = (expand_word(sub, env) or ["push"])[0]
    except Unresolved as e:
        opaque = opaque or str(e)
    if sub in READ_ONLY and not opaque:
        return None
    label = f"`git stash{'' if len(rest) < 2 else ' ' + rest[1]}`"
    if opaque:
        label = f"`git {' '.join(args[i:i + 2])}`"
    if pending_unresolved or target is None:
        return blocked(label, pending_unresolved or "the working directory is unknown")
    if isinstance(seen, Unresolved):
        return blocked(label, seen)
    for k in GIT_TARGET:
        if seen.get(k) == UNCERTAIN:
            return blocked(label, f"${k} is set by a step that may not have run")
        if k in seen:
            git_env[k] = seen[k]
    if git_dir is None and "GIT_DIR" in git_env:
        git_dir = git_env.pop("GIT_DIR")
    reason = shared_reason(target, git_dir, work_tree, git_env)
    if not reason:
        return None
    if opaque:                            # a stash only if the word expands to one
        reason += (f", and a word of it could not be resolved ({opaque}), "
                   f"so it may be a mutating stash")
    return f"[stash-guard] Blocked {label}: {reason}. " + ALTERNATIVES


def judge_alias(rest, target, git_dir, work_tree, configs, config_unresolved,
                seen, git_env, depth):
    """Deny reason when `git <rest>` runs an alias (`-c alias.x=...` on the
    command line, or configured) that can reach a mutating stash, else None.
    git ignores an alias that shadows a builtin, so those are not looked up.
    A `!` alias is a shell body run at the repo's top level."""
    name = rest[0]
    if name in GIT_BUILTINS or name.startswith("-"):
        return None
    label = f"`git {name}`"
    if config_unresolved:
        return blocked(label, f"a `-c` value that may define its alias "
                              f"cannot be expanded: {config_unresolved}")
    base = (["-C", target] if target else []) + \
        ([f"--git-dir={git_dir}"] if git_dir else []) + \
        ([f"--work-tree={work_tree}"] if work_tree else [])
    for c in configs:
        base += ["-c", c]
    env = git_env if isinstance(seen, Unresolved) else {
        **git_env, **{k: seen[k] for k in GIT_TARGET
                      if k in seen and seen[k] != UNCERTAIN}}
    r = git(base + ["config", "--get", f"alias.{name}"], env)
    if r is None or r.returncode not in (0, 1):
        return blocked(label, "its alias lookup (git config) failed or timed out")
    value = r.stdout.strip() if r.returncode == 0 else ""
    if not value:
        return None
    if depth >= MAX_DEPTH:
        if may_stash(value):
            return ("[stash-guard] Blocked " + label + ": its alias chain is "
                    "nested too deeply to inspect, and an uninspected mutating "
                    "stash is denied by default. " + ALTERNATIVES)
        return None
    # The alias acts in the repo the invocation names, so its own git sees
    # that repo: carry the options over as the env git sets for an alias.
    child = {} if isinstance(seen, Unresolved) else dict(seen)
    for k, v in (("GIT_DIR", git_dir), ("GIT_WORK_TREE", work_tree)):
        if v:
            child[k] = v if os.path.isabs(v) or not target else os.path.join(target, v)
    extra = " ".join(shlex.quote(a) for a in rest[1:])
    if value.startswith("!"):             # git hands its `-c` options on in the env
        quoted = " ".join("'" + c.replace("'", "'\\''") + "'" for c in configs)
        if quoted:
            prior = child.get("GIT_CONFIG_PARAMETERS")
            child["GIT_CONFIG_PARAMETERS"] = f"{prior} {quoted}" if prior else quoted
        return judge(f"{value[1:]} {extra}", target, child, depth + 1)
    try:
        words_ = shlex.split(value)
    except ValueError:
        words_ = value.split()
    opts = [w for c in configs for w in ("-c", c)]
    return judge_git(opts + words_ + rest[1:], target, child, {}, depth + 1)


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
    reason = judge(cmd, cwd, {**os.environ, "PWD": cwd})
    if reason:
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "permissionDecision": "deny",
                                          "permissionDecisionReason": reason}},
                  sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
