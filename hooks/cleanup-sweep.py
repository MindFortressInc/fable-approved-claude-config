#!/usr/bin/env python3
"""Cleanup-queue helper for check-careful.sh's deferred deletes.

Reads ~/.claude/cleanup-needed.log (JSON-lines: {ts,cwd,cmd,reason}) — written
by the careful hook when an unrecognized delete is deferred during an
unattended loop. Shared by the cleanup sweep:
  - /babysit-prs (unattended)        -> `--count` / default report (surface only)
  - /cleanup, /wrapup, /PRlaunch     -> `--run` to action, `--remove` to decline.

  cleanup-sweep.py            human-readable summary (default)
  cleanup-sweep.py --count    just the number of pending entries
  cleanup-sweep.py --json     one entry per line with an 'i' index (for resolve)
  cleanup-sweep.py --run N    DELETE entry N's parsed targets, then drop entry N
  cleanup-sweep.py --run-all  --run every entry (descending, so indices hold)
  cleanup-sweep.py --remove N drop entry index N WITHOUT deleting (declined)
  cleanup-sweep.py --append   queue one JSON entry read from stdin (the hook)

Every path that writes the log does so under one shared lock (`queue_lock()`),
including check-careful.sh's append — see that function for why.

Why `--run` instead of re-running the queued command:
  The careful hook (check-careful.sh) defers ANY unrecognized `rm -r` — so
  replaying the stored `cmd` just re-defers it. Worse, a queued `cmd` is the
  *original* command that happened to contain the rm (e.g. `git worktree add`
  after an `rm -rf`, or a `git clone` into a scratch dir) — re-running it would
  RECREATE the scratch, not clear it. So `--run` parses the command for its
  actual delete targets (cd- and VAR=-aware, glob-expanded, relative to the
  entry's cwd), reusing careful-rm.py's parser, and deletes ONLY those, via
  shutil/os — no side effects, no re-defer.
"""
import sys
import os
import re
import glob
import shutil
import shlex
import json
import fcntl
import importlib.util
from contextlib import contextmanager

LOG = os.path.expanduser("~/.claude/cleanup-needed.log")
LOCK = LOG + ".lock"

# Credential-shaped strings must never rest in the queue (we hit this: a GitHub
# PAT sat in cleanup-needed.log in plaintext). check-careful.sh redacts on
# write; this mirror scrubs entries written before that fix (or by an older
# hook) the first time any sweep touches the log. Keep in sync with
# redact_secrets() in check-careful.sh.
_SECRET_PATTERNS = [
    # {20,} mirrors the gh*_/sk-/AKIA patterns below: without a minimum,
    # an ordinary path like `github_pat_backup` was redacted as a secret.
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "***REDACTED***"),
    (re.compile(r"(^|[^A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}"), r"\1***REDACTED***"),
    (re.compile(r"(^|[^A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}"), r"\1***REDACTED***"),
    # AKIA = permanent AWS access-key ID; ASIA = AWS STS temporary access-key
    # ID — same 4-letter-prefix + 16-alnum shape, must be redacted the same.
    (re.compile(r"(^|[^A-Za-z0-9_])(?:AKIA|ASIA)[0-9A-Z]{16}"), r"\1***REDACTED***"),
    # IGNORECASE: an all-caps `AUTHORIZATION: BEARER <secret>` slipped past
    # the first-letter-only classes.
    (re.compile(r"(^|[^A-Za-z0-9_])lin_api_[A-Za-z0-9]{20,}"), r"\1***REDACTED***"),
    (re.compile(r"(^|[^A-Za-z0-9_])xox[abposr]-[A-Za-z0-9-]{10,}"), r"\1***REDACTED***"),
    (re.compile(r"(authorization:?\s*(?:bearer|token|basic)\s+)[^\s\"']+", re.IGNORECASE), r"\1***REDACTED***"),
    # A raw `Authorization: <key>` (Linear personal keys are sent bare). {8,}
    # keeps a scheme word already handled above (Bearer/Token/Basic) unmatched.
    (re.compile(r"(authorization:\s*)[^\s\"'*]{8,}", re.IGNORECASE), r"\1***REDACTED***"),
]


def _redact(text):
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text

# Reuse the rm parser (segments / rm_targets) so target extraction matches the
# exact logic the careful hook used to defer the delete in the first place.
_CR = None


def _careful_rm():
    global _CR
    if _CR is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "careful-rm.py")
        spec = importlib.util.spec_from_file_location("careful_rm", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CR = mod
    return _CR


@contextmanager
def queue_lock():
    """Exclusive lock over the cleanup queue — held by EVERY writer of LOG.

    The queue is a read-then-full-rewrite surface (`save()` truncates), and
    ~/.claude is shared by many parallel Claude Code workers: check-careful.sh
    queues a deferred delete on every unrecognized `rm -r`, in any session,
    while /cleanup, /wrapup, /PRlaunch and /babysit-prs all run sweeps. Without
    a shared lock, an append landing between a sweep's `load()` and its
    `save()` is silently erased — the entry was never in the list written back.
    So the hook appends through `--append` (which takes this lock) and every
    sweep rewrite happens under it, giving appends and rewrites one total order.

    Deliberately NOT held across the deletions in `--run`/`--run-all`:
    `shutil.rmtree` of a big worktree takes seconds to tens of seconds, and
    check-careful.sh is a PreToolUse hook that must never block that long. The
    sweeps instead re-read the queue under the lock and drop only the entries
    they resolved (`drop_entries`), so a concurrent append survives regardless
    of how long the deleting took.
    """
    d = os.path.dirname(LOCK)
    if d:
        os.makedirs(d, exist_ok=True)
    fh = open(LOCK, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fh.close()  # closing the fd releases the flock


def load():
    """Load queue entries, scrubbing any credential-shaped strings.

    Entries written before check-careful.sh redacted on write may hold a raw
    token; if any redaction fired, the log is rewritten in place immediately so
    the plaintext credential stops resting on disk.
    """
    out, dirty = [], False
    # cmd/cwd are path-bearing: extract_targets() resolves delete targets
    # against them. If redaction rewrites either, the entry no longer
    # describes real filesystem paths — auto-running it (--run/--run-all)
    # could silently no-op (redacted path doesn't exist -> looks "cleared"
    # without deleting anything) or resolve into an unrelated path. `reason`
    # is display-only and never feeds path resolution, so it doesn't count.
    PATH_FIELDS = ("cmd", "cwd")
    try:
        with open(LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    e = {"cmd": line, "cwd": "", "reason": "", "ts": 0}
                if not isinstance(e, dict):
                    # `null`, a list or a bare number is as malformed as bad
                    # JSON here — e.get() below would raise on all of them.
                    e = {"cmd": line, "cwd": "", "reason": "", "ts": 0}
                for k in ("cmd", "reason", "cwd"):
                    v = e.get(k, "")
                    if isinstance(v, str):
                        r = _redact(v)
                        if r != v:
                            e[k] = r
                            dirty = True
                            if k in PATH_FIELDS:
                                e["_redacted_path_field"] = True
                out.append(e)
    except FileNotFoundError:
        pass
    if dirty:
        save(out)
    return out


def save(entries):
    if not entries:
        try:
            os.remove(LOG)
        except FileNotFoundError:
            pass
        return
    with open(LOG, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def append(entry):
    """Queue one deferred delete (check-careful.sh's `--append`), under the lock."""
    with queue_lock():
        with open(LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")


def drop_entries(dropped):
    """Remove `dropped` entries from the queue without clobbering concurrent appends.

    Re-reads the queue under the lock rather than writing back the caller's
    (now possibly stale) snapshot, and removes one occurrence per dropped entry
    by value — indices from the snapshot no longer hold once another process
    has appended.
    """
    if not dropped:
        return
    with queue_lock():
        current = load()
        for e in dropped:
            for i, c in enumerate(current):
                if c == e:
                    current.pop(i)
                    break
        save(current)


_VAR = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)')


def _expand(s, local_vars):
    """Expand $VAR / ${VAR} from local assignments, then the real environment.
    Unknown variables are left intact (so resolve() can detect + skip them)."""
    def repl(m):
        name = m.group(1) or m.group(2)
        if name in local_vars:
            return local_vars[name]
        return os.environ.get(name, m.group(0))
    return _VAR.sub(repl, s)


def _resolve(t, cwd, local_vars):
    t = os.path.expanduser(_expand(t, local_vars))
    if not os.path.isabs(t):
        t = os.path.join(cwd, t)
    return os.path.normpath(t)


_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def extract_targets(cmd, base_cwd):
    """Parse a queued command into its real delete targets.

    Tracks `cd` (so a target relative to a mid-command cd resolves correctly)
    and simple NAME=value assignments (so `WT=/tmp/x; rm -rf "$WT"` resolves).
    Returns resolved absolute path patterns (globs preserved for delete()).
    """
    cr = _careful_rm()
    cwd = os.path.expanduser(base_cwd) if base_cwd else os.path.expanduser("~")
    local_vars, targets = {}, []
    for seg in cr.segments(cmd):
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        if not toks:
            continue
        k = 0
        while k < len(toks) and _ASSIGN.match(toks[k]):
            name, val = toks[k].split('=', 1)
            local_vars[name] = os.path.expanduser(_expand(val, local_vars))
            k += 1
        rest = toks[k:]
        if not rest:
            continue
        if rest[0] == 'cd' and len(rest) > 1:
            cwd = _resolve(rest[1], cwd, local_vars)
        elif rest[0] == 'rm':
            for t in cr.rm_targets(rest[1:]):
                targets.append(_resolve(t, cwd, local_vars))
    return targets


# Paths we refuse to delete even when approved — a parser slip must never nuke
# the home dir or the filesystem root.
def _is_catastrophic(ap):
    home = os.path.expanduser("~")
    return ap in ("", "/", home, os.path.dirname(home))


def delete_targets(targets):
    cr = _careful_rm()
    deleted, missing, errors, skipped = [], [], [], []
    for p in targets:
        if "$" in p:
            skipped.append((p, "unresolved shell variable"))
            continue
        ap = os.path.abspath(p)
        if _is_catastrophic(ap):
            skipped.append((ap, "refused: catastrophic path"))
            continue
        hits = glob.glob(ap)
        if not hits:
            missing.append(ap)
            continue
        for h in hits:
            _, label = cr.classify(h)
            try:
                if os.path.isdir(h) and not os.path.islink(h):
                    shutil.rmtree(h)
                else:
                    os.remove(h)
                deleted.append((h, label))
            except OSError as e:
                errors.append((h, str(e)))
    return deleted, missing, errors, skipped


def run_entry(entries, n):
    """Delete entry n's parsed targets; drop the entry iff fully resolved.
    Returns True if the entry was dropped."""
    e = entries[n]
    print(f"\n[{n}] in {e.get('cwd') or '?'}")
    if e.get("_redacted_path_field"):
        print("    ⚠ cmd/cwd was credential-redacted — path no longer matches reality; "
              "left in queue for manual review, not auto-run")
        return False
    targets = extract_targets(e.get("cmd", ""), e.get("cwd", ""))
    if not targets:
        print("    no delete targets parsed (nothing to do) — left in queue for manual review")
        return False
    deleted, missing, errors, skipped = delete_targets(targets)
    for h, label in deleted:
        print(f"    ✓ deleted: {h} — {label}")
    for ap in missing:
        print(f"    · already gone: {ap}")
    for h, msg in errors:
        print(f"    ✗ error: {h} — {msg}")
    for p, why in skipped:
        print(f"    ⚠ skipped: {p} — {why}")
    if errors or skipped:
        print(f"    → kept entry [{n}] (unresolved targets above)")
        return False
    entries.pop(n)
    print(f"    → cleared entry [{n}]")
    return True


def main():
    args = sys.argv[1:]

    if args and args[0] == "--append":
        # check-careful.sh pipes one JSON entry in. Appending through here (not
        # `>> $CLEANUP_LOG`) is what puts hook appends and sweep rewrites under
        # the SAME lock.
        raw = sys.stdin.read().strip()
        if not raw:
            return
        try:
            entry = json.loads(raw)
        except ValueError:
            entry = {"cmd": raw, "cwd": "", "reason": "", "ts": 0}
        append(entry)
        return

    if args and args[0] == "--remove":
        try:
            n = int(args[1])
        except (IndexError, ValueError):
            print("usage: cleanup-sweep.py --remove N", file=sys.stderr)
            sys.exit(2)
        # Whole read-modify-write under one lock: it does no slow work.
        with queue_lock():
            entries = load()
            if 0 <= n < len(entries):
                entries.pop(n)
                save(entries)
        return

    with queue_lock():
        entries = load()  # consistent snapshot (save() truncates in place)

    if args and args[0] == "--count":
        print(len(entries))
        return
    if args and args[0] == "--json":
        for i, e in enumerate(entries):
            e = dict(e)
            e["i"] = i
            print(json.dumps(e))
        return
    if args and args[0] == "--run":
        try:
            n = int(args[1])
        except (IndexError, ValueError):
            print("usage: cleanup-sweep.py --run N", file=sys.stderr)
            sys.exit(2)
        if not (0 <= n < len(entries)):
            print(f"no entry [{n}] (queue has {len(entries)})", file=sys.stderr)
            sys.exit(2)
        e = entries[n]
        if run_entry(entries, n):
            drop_entries([e])
        return
    if args and args[0] == "--run-all":
        if not entries:
            print("🧹 No cleanups pending.")
            return
        dropped = []
        for n in range(len(entries) - 1, -1, -1):  # descending: indices stay valid
            e = entries[n]
            if run_entry(entries, n):
                dropped.append(e)
        drop_entries(dropped)
        print(f"\n=== {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} remaining ===")
        return

    # default: human-readable report
    if not entries:
        print("🧹 No cleanups pending.")
        return
    print(f"🧹 {len(entries)} cleanup(s) pending (deferred during unattended runs):")
    for i, e in enumerate(entries):
        print(f"\n[{i}] in {e.get('cwd') or '?'}")
        print(f"    $ {e.get('cmd', '')}")
        for ln in (e.get("reason", "") or "").splitlines():
            print(f"    {ln}")
        if e.get("_redacted_path_field"):
            print("    ⚠ cmd/cwd was credential-redacted — review manually before --run")


if __name__ == "__main__":
    main()
