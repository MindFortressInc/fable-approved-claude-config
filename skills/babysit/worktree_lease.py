#!/usr/bin/env python3
"""worktree_lease.py -- exclusive per-PR worktree lease for babysit apply-workers.

## The bug this closes

A detached CR-CLI relaunch reused /tmp/<repo>-<pr>-cli while a fixer was
mid-edit in that SAME worktree. The git-safety dirty-check (`git status
--porcelain` at read time, right before reuse) did not catch it, because the
worktree WAS clean at the instant it looked -- a TOCTOU. No amount of
re-checking tree state fixes a TOCTOU; the fix is to stop deriving "is this
worktree in use" from tree state at all, and instead have the worker that is
USING it say so, explicitly, for as long as it is using it. That explicit
"I'm using this" record is the lease this module implements.

## Idiom mirrored, not invented

hooks/babysit-lock.sh already has the sweep-mutex shape this needs: a JSON
lock file recording owner/host/pid/started/heartbeat, with staleness decided
by TTL-since-heartbeat. This module mirrors that shape (see `is_stale`)
rather than inventing a second locking idiom -- just keyed per-worktree
instead of being one machine-wide lock, and (see the correction below) with
a heartbeat that doesn't depend on the caller remembering to refresh.

## The caller-refresh correction

babysit-lock.sh's heartbeat is refreshed by the CALLER at step boundaries
(the skill calls `refresh` after Steps 1 and 2). That shape goes stale
WHILE HEALTHY whenever a single step runs longer than the TTL
(e.g. a slow test run mid apply-worker cycle) -- the holder is alive and
working, but nothing calls refresh until the step ends, so a concurrent
reaper can steal a lease out from under a live worker.

This module does not reintroduce that shape. `acquire()` spawns a detached
heartbeat daemon (a separate OS process, not a thread tied to whatever the
holder's foreground code happens to be doing) that refreshes on a fixed
wall-clock interval regardless of what step the holder is executing. The
daemon watches the holder's pid and self-terminates the moment the holder is
gone -- it never "resurrects" a lease after its holder has died (see
_heartbeat_daemon_loop). The daemon dying with its watched process is what
lets a crash still surface as a stale/dead lease for the reap path below.

## Reap: three signals, mirroring the mutex's TTL plus speed and a ceiling

`is_stale()` treats a lease as stale if ANY of:
  1. the recorded holder's pid is confirmed dead (same host) -- immediate,
     no need to wait out a TTL for a worker that visibly crashed; or
  2. the heartbeat has aged past TTL -- the same backstop babysit-lock.sh
     uses, for cross-host holders or any case pid-liveness can't answer; or
  3. the lease itself is older than MAX_AGE since `started`. The watched
     pid is the worker's agent process (see _default_watch_pid), which can
     outlive the
     worker: a cancelled/crashed subagent leaves its parent agent alive, so
     the daemon keeps heartbeating and neither 1 nor 2 ever fires. MAX_AGE
     (BABYSIT_LEASE_MAX_AGE, default 2h -- far above any apply cycle) is
     the independent expiry that stops such a lease stranding forever.

Reaping a LEASE only ever removes this module's own JSON record. It never
touches a git worktree, and it never kills anything by name/pattern -- only
a pid this module itself wrote into the record it is now reaping.

## Cheap to inspect

A lease is one small JSON file, human-readable with `cat` or `jq`:
    /tmp/babysit-worktree-lease.<repo>-<pr>.json
`worktree_lease.py status` lists every live lease; `status <key>` shows one.

## API

    from worktree_lease import WorktreeLease, is_leased, filter_leased

    lease = WorktreeLease("acme-api-3210", worktree="/tmp/acme-api-3210-cli")
    if not lease.acquire():
        ...  # another worker holds it
    try:
        ...edit, test, push...
    finally:
        lease.release()   # a crash instead of a clean release still becomes
                           # reapable -- see 'Reap' above, not a second path

    leased, record = is_leased("acme-api-3210")   # read-only, reaps if stale
    candidates, skipped = filter_leased(targets)    # selection-time exclusion

CLI (for bash callers, e.g. a detached CR-CLI launcher script):
    worktree_lease.py acquire <key> [--owner O] [--worktree PATH] [--watch-pid PID]
                                                                    -> exit 0/3
    worktree_lease.py release <key> (--owner O | --force)           -> exit 0/2/3
    worktree_lease.py is-leased <key>                               -> exit 0/1
    worktree_lease.py status [<key>]
    worktree_lease.py reap
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

DEFAULT_LEASE_DIR = os.environ.get("BABYSIT_LEASE_DIR") or tempfile.gettempdir()
DEFAULT_TTL = int(os.environ.get("BABYSIT_LEASE_TTL", "600"))
DEFAULT_MAX_AGE = int(os.environ.get("BABYSIT_LEASE_MAX_AGE", "7200"))
DEFAULT_HEARTBEAT_INTERVAL = int(os.environ.get("BABYSIT_LEASE_HEARTBEAT_INTERVAL", "20"))

_LEASE_PREFIX = "babysit-worktree-lease."
_LEASE_SUFFIX = ".json"


def _now():
    return time.time()


def _lease_path(key, lease_dir=None):
    lease_dir = lease_dir or DEFAULT_LEASE_DIR
    safe = key.replace(os.sep, "_")
    return os.path.join(lease_dir, "%s%s%s" % (_LEASE_PREFIX, safe, _LEASE_SUFFIX))


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_atomic(path, record):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".lease-tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not owned by us -- treat as alive
    except OSError:
        return False


def is_stale(record, ttl=None, now=None, max_age=None):
    """Mirrors hooks/babysit-lock.sh's TTL-since-heartbeat staleness test,
    plus an immediate pid-liveness signal so a confirmed-dead holder does not
    have to wait out the TTL, plus a MAX_AGE ceiling so an abandoned lease
    whose watched pid stays alive still expires (see module docstring,
    'Reap'). max_age <= 0 disables the ceiling."""
    ttl = DEFAULT_TTL if ttl is None else ttl
    max_age = DEFAULT_MAX_AGE if max_age is None else max_age
    now = _now() if now is None else now
    started = record.get("started")
    if max_age > 0 and started and (now - started) >= max_age:
        return True
    if record.get("host") == socket.gethostname():
        pid = record.get("pid")
        if pid and not _pid_alive(pid):
            return True
    hb = record.get("heartbeat") or 0
    return (now - hb) >= ttl


# Process names that mark a coding-agent process: comma-separated
# BABYSIT_LEASE_AGENT_NAMES, default "claude" (Claude Code). Add the name of
# any other agent CLI you run babysit workers under. A node/bun process counts
# only when it is running one of them (a script with an agent name, or the
# @anthropic-ai/claude-code package). Read at call time, not import time, so a
# caller's env always applies.
_AGENT_RUNTIMES = ("node", "bun")


def _agent_names():
    raw = os.environ.get("BABYSIT_LEASE_AGENT_NAMES") or "claude"
    return tuple(n.strip() for n in raw.split(",") if n.strip())


def _ps(pid, field):
    try:
        return subprocess.run(["ps", "-o", field + "=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _is_agent(pid):
    # `comm` is the executable alone (the last and only column, so a path with
    # spaces survives); `args` is read only to see what a node/bun runs.
    name = os.path.splitext(os.path.basename(_ps(pid, "comm")))[0]
    agents = _agent_names()
    # Some agent CLIs ship as a platform-suffixed standalone binary
    # (<name>-aarch64-apple-darwin), often run without renaming.
    if name in agents or any(name.startswith(a + "-") for a in agents):
        return True
    if name not in _AGENT_RUNTIMES:
        return False
    toks = _ps(pid, "args").split()[1:]
    return any(os.path.splitext(os.path.basename(t))[0] in agents
               or "claude-code" in t for t in toks)


def _default_watch_pid(max_depth=12):
    """The CLI's default --watch-pid: the nearest ancestor that is a coding
    agent (see _agent_names), else the calling shell (os.getppid()).

    In an agent, every Bash tool call is its own short-lived
    shell, so the calling shell exits the moment `acquire` returns. The
    lease recorded that dead pid, the daemon stopped, and the very next
    is_leased()/status reaped the lease out from under a still-working
    worker. The agent process is the one that lives as long as the worker.
    Outside an agent (a human's terminal) no ancestor matches and the
    calling shell is kept, which is right there."""
    parent = os.getppid()
    pid = parent
    for _ in range(max_depth):
        if not pid or pid <= 1:
            break
        if _is_agent(pid):
            return pid
        try:
            pid = int(_ps(pid, "ppid"))
        except ValueError:
            break
    return parent


def _heartbeat_daemon_loop(key, owner, lease_dir, ttl, heartbeat_interval, watch_pid):
    path = _lease_path(key, lease_dir)
    while _pid_alive(watch_pid):
        record = _read(path)
        if not record or record.get("owner") != owner:
            return  # released, stolen, or reaped out from under us -- stop
        record["heartbeat"] = _now()
        _write_atomic(path, record)
        time.sleep(heartbeat_interval)
    # watch_pid confirmed dead: stop WITHOUT writing again. A stray write
    # here would "resurrect" a lease is_leased() may already have reaped.


def _spawn_heartbeat_daemon(key, owner, lease_dir, ttl, heartbeat_interval, watch_pid):
    args = [
        sys.executable, os.path.abspath(__file__), "_heartbeat-daemon", key,
        "--owner", owner, "--lease-dir", lease_dir or DEFAULT_LEASE_DIR,
        "--ttl", str(ttl if ttl is not None else DEFAULT_TTL),
        "--heartbeat-interval", str(heartbeat_interval if heartbeat_interval is not None
                                     else DEFAULT_HEARTBEAT_INTERVAL),
        "--watch-pid", str(watch_pid),
    ]
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    return proc.pid


class LeaseHeld(Exception):
    """Raised by the `with WorktreeLease(...)` form when acquisition fails."""

    def __init__(self, record):
        super().__init__("worktree lease held by %s" % (record or {}).get("owner"))
        self.record = record


class WorktreeLease:
    """Exclusive lease on one PR's worktree for an apply-worker's
    edit-test-push cycle. See module docstring for the idiom and the
    caller-refresh heartbeat correction this implements."""

    def __init__(self, key, lease_dir=None, ttl=None, heartbeat_interval=None,
                 owner=None, worktree=None, max_age=None):
        self.key = key
        self.lease_dir = lease_dir or DEFAULT_LEASE_DIR
        self.ttl = DEFAULT_TTL if ttl is None else ttl
        self.max_age = max_age
        self.heartbeat_interval = (DEFAULT_HEARTBEAT_INTERVAL if heartbeat_interval is None
                                    else heartbeat_interval)
        self.owner = owner or ("%s-%d" % (socket.gethostname(), os.getpid()))
        self.worktree = worktree
        self.path = _lease_path(key, self.lease_dir)
        self._daemon_pid = None
        self._held = False

    def acquire(self, watch_pid=None):
        """Take the lease. Returns True if acquired (including reaping a
        stale one out from under a dead holder), False if genuinely held by
        a live different owner.

        The "no other worker holds it" guarantee only means something if
        acquire() itself can't be won by two racing processes -- a plain
        read-then-write (check `existing`, then write) has exactly the same
        TOCTOU shape this whole module exists to remove, just moved one
        level down. So the actual file create goes through
        `_create_exclusive` (atomic, content-complete -- see its docstring
        for why a plain O_CREAT|O_EXCL isn't enough): the fast,
        no-contention path can only ever be won by one of N simultaneous
        first-time acquirers. The stale-reap path (unlink a dead holder's
        file, then recreate) still funnels through the same
        exclusive-create, so only one of several racing reapers ends up
        actually holding it -- the other(s) get False."""
        watch_pid = watch_pid or os.getpid()
        os.makedirs(self.lease_dir, exist_ok=True)
        record = {
            "owner": self.owner, "host": socket.gethostname(), "pid": watch_pid,
            "key": self.key, "worktree": self.worktree,
            "started": _now(), "heartbeat": _now(),
        }

        if self._create_exclusive(record):
            return self._finish_acquire(watch_pid)

        existing = _read(self.path)
        if existing and existing.get("owner") == self.owner:
            # re-entrant acquire by the same owner -- not a race target,
            # a plain overwrite is fine.
            _write_atomic(self.path, record)
            return self._finish_acquire(watch_pid)
        if existing and not is_stale(existing, self.ttl, max_age=self.max_age):
            return False  # genuinely held live by someone else
        # stale (dead holder, expired TTL, or unreadable) -- reap and retry
        # the exclusive create ONCE. If a concurrent acquirer wins that
        # retry, we correctly lose too rather than both claiming success.
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        if self._create_exclusive(record):
            return self._finish_acquire(watch_pid)
        return False

    def _create_exclusive(self, record):
        """Atomically create self.path with FULL content already in place --
        never a plain os.open(O_CREAT|O_EXCL) on the target. That creates an
        EMPTY file the instant open() returns, before the content is
        written; a concurrent racer's _read() can observe that empty file
        between our create and our write, fail to parse it, treat it as
        corrupt-and-reapable, unlink it, and win a lease of its own -- the
        exact TOCTOU this whole fix exists to close, just narrowed to a
        microsecond window instead of a multi-second one (caught by
        test_concurrent_acquire_only_one_winner: 2/8 racers won before this
        fix).

        Standard fix: write the full content to a scratch file first, then
        os.link() it into place. link() is exclusive (EEXIST if the target
        already exists) AND atomic AND the destination is only ever visible
        with its content already complete, because it's a hard link to an
        inode that was fully written before the link was made."""
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".lease-tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(record, f)
            try:
                os.link(tmp, self.path)
                return True
            except FileExistsError:
                return False
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _finish_acquire(self, watch_pid):
        self._held = True
        self._daemon_pid = _spawn_heartbeat_daemon(
            self.key, self.owner, self.lease_dir, self.ttl,
            self.heartbeat_interval, watch_pid,
        )
        return True

    def refresh(self):
        """Manual refresh. Not required for correctness (the daemon already
        refreshes continuously) -- exposed for callers/tests that want an
        immediate bump."""
        record = _read(self.path)
        if not record or record.get("owner") != self.owner:
            return False
        record["heartbeat"] = _now()
        _write_atomic(self.path, record)
        return True

    def release(self):
        if self._daemon_pid:
            try:
                os.kill(self._daemon_pid, 15)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            self._daemon_pid = None
        record = _read(self.path)
        if record and record.get("owner") == self.owner:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
        self._held = False

    def __enter__(self):
        if not self.acquire():
            raise LeaseHeld(_read(self.path))
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def is_leased(key, lease_dir=None, ttl=None, max_age=None):
    """Read-only selection-time check. Reaps a stale record as a side
    effect, so 'a lease whose holder died is reaped and the PR becomes
    selectable again' takes effect in THIS call, not the next sweep."""
    path = _lease_path(key, lease_dir)
    record = _read(path)
    if not record:
        return False, None
    if is_stale(record, ttl, max_age=max_age):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return False, None
    return True, record


def filter_leased(targets, key_fn=None, lease_dir=None, ttl=None, max_age=None):
    """Selection-time exclusion for any launcher choosing among PR
    worktrees. This is the ONLY place the exclusion belongs --
    consulting lease state when a candidate is CHOSEN, never re-deriving
    "in use" from the worktree's current cleanliness at read/reuse time
    (that dirty-check-at-read-time TOCTOU is what corrupted the CR-CLI
    relaunch described in the module docstring).

    key_fn maps a target dict -> lease key; default '{repo}-{pr}' matches
    the CR-CLI launcher's `id="${repo}-${pr}"` convention.

    Returns (candidates, skipped) where skipped is [(target, holder_owner), ...].
    """
    key_fn = key_fn or (lambda t: "%s-%s" % (t["repo"], t["pr"]))
    candidates, skipped = [], []
    for t in targets:
        leased, record = is_leased(key_fn(t), lease_dir, ttl, max_age)
        if leased:
            skipped.append((t, (record or {}).get("owner")))
        else:
            candidates.append(t)
    return candidates, skipped


def list_leases(lease_dir=None, ttl=None, max_age=None):
    lease_dir = lease_dir or DEFAULT_LEASE_DIR
    out = []
    try:
        names = os.listdir(lease_dir)
    except FileNotFoundError:
        return out
    for n in names:
        if not (n.startswith(_LEASE_PREFIX) and n.endswith(_LEASE_SUFFIX)):
            continue
        record = _read(os.path.join(lease_dir, n))
        if not record:
            continue
        record = dict(record)
        record["_stale"] = is_stale(record, ttl, max_age=max_age)
        out.append(record)
    return out


def reap_stale(lease_dir=None, ttl=None, max_age=None):
    """Bulk sweep: remove every stale lease file. Returns the reaped records."""
    lease_dir = lease_dir or DEFAULT_LEASE_DIR
    reaped = []
    for record in list_leases(lease_dir, ttl, max_age):
        if record.get("_stale") and record.get("key"):
            path = _lease_path(record["key"], lease_dir)
            try:
                os.unlink(path)
                reaped.append(record)
            except FileNotFoundError:
                pass
    return reaped


# ---------------------------------------------------------------- CLI ----
def main(argv=None):
    # --lease-dir/--ttl are given AFTER the subcommand by every caller here
    # (e.g. `acquire <key> --lease-dir ...`), so they must be declared on
    # each subparser, not just the top-level parser -- argparse only lets a
    # top-level optional appear BEFORE the subcommand token. A `parents=`
    # common-options parser keeps that from being duplicated five times.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lease-dir", default=None)
    common.add_argument("--ttl", type=int, default=None)
    common.add_argument("--max-age", type=int, default=None,
                        help="seconds after `started` a lease expires even while "
                             "heartbeating (default BABYSIT_LEASE_MAX_AGE / 7200; "
                             "<=0 disables)")

    p = argparse.ArgumentParser(prog="worktree_lease.py", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("acquire", parents=[common],
                        help="take the lease; exit 0 acquired, 3 held-live")
    a.add_argument("key")
    a.add_argument("--owner", default=None)
    a.add_argument("--worktree", default=None)
    a.add_argument("--watch-pid", type=int, default=None,
                    help="pid whose death ends the heartbeat daemon and "
                         "makes the lease reapable (default: the nearest "
                         "agent ancestor (BABYSIT_LEASE_AGENT_NAMES, "
                         "default claude), else the calling "
                         "shell). Never pass $$ from an agent's Bash tool "
                         "call: that shell exits when the call returns")

    r = sub.add_parser("release", parents=[common],
                        help="drop the lease; exit 0 released/absent, "
                             "2 no --owner given, 3 --owner is not the holder")
    r.add_argument("key")
    r.add_argument("--owner", default=None,
                    help="the exact owner string `acquire` recorded. Required "
                         "unless --force: it is NOT defaulted to this process, "
                         "which could never be the holder")
    r.add_argument("--force", action="store_true",
                    help="drop the lease whoever holds it -- for clearing a "
                         "lease stranded by a holder that died without releasing")

    il = sub.add_parser("is-leased", parents=[common],
                         help="exit 0 leased(live), 1 not leased")
    il.add_argument("key")

    st = sub.add_parser("status", parents=[common])
    st.add_argument("key", nargs="?")

    sub.add_parser("reap", parents=[common])

    hd = sub.add_parser("_heartbeat-daemon", parents=[common], help=argparse.SUPPRESS)
    hd.add_argument("key")
    hd.add_argument("--owner", required=True)
    hd.add_argument("--watch-pid", type=int, required=True)
    hd.add_argument("--heartbeat-interval", type=float, default=DEFAULT_HEARTBEAT_INTERVAL)

    args = p.parse_args(argv)

    if args.cmd == "_heartbeat-daemon":
        _heartbeat_daemon_loop(args.key, args.owner, args.lease_dir, args.ttl,
                                args.heartbeat_interval, args.watch_pid)
        return 0

    if args.cmd == "acquire":
        owner = args.owner or ("%s-%d" % (socket.gethostname(), os.getpid()))
        lease = WorktreeLease(args.key, lease_dir=args.lease_dir, ttl=args.ttl,
                               owner=owner, worktree=args.worktree, max_age=args.max_age)
        watch_pid = args.watch_pid or _default_watch_pid()
        ok = lease.acquire(watch_pid=watch_pid)
        if not ok:
            existing = _read(lease.path) or {}
            age = int(_now() - (existing.get("heartbeat") or 0))
            print("LEASED key=%s holder=%s age=%ds" % (args.key, existing.get("owner"), age))
            return 3
        print("ACQUIRED key=%s owner=%s watch_pid=%d daemon_pid=%s"
              % (args.key, owner, watch_pid, lease._daemon_pid))
        return 0

    if args.cmd == "release":
        path = _lease_path(args.key, args.lease_dir)
        record = _read(path)
        if not record:
            print("UNLEASED key=%s" % args.key)
            return 0
        # Do NOT default `owner` to this process's own identity. A CLI
        # `release` is always a separate, short-lived process from the
        # `acquire` that wrote the record (see the comment below), so
        # `host-<our pid>` could never equal the recorded
        # `host-<acquirer pid>`: the guard was unsatisfiable and every
        # ownerless release a no-op that still exited 0. Callers saw
        # success while the lease survived -- how one PR's lease outlived
        # its holder by 12h and was declined by 6 consecutive sweeps.
        if not args.force:
            if args.owner is None:
                print("NEEDS-OWNER key=%s owner=%s -- pass the --owner string "
                      "`acquire` recorded, or --force (left intact)"
                      % (args.key, record.get("owner")))
                return 2
            if record.get("owner") != args.owner:
                print("NOT-OWNER key=%s owner=%s (left intact)"
                      % (args.key, record.get("owner")))
                return 3
        # No daemon pid to signal here (a CLI `release` call is a separate,
        # short-lived process from whatever called `acquire`) -- deleting the
        # record is enough: the daemon checks record.owner before every write
        # and self-terminates on the next check once it finds this gone.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        print("RELEASED key=%s owner=%s" % (args.key, record.get("owner")))
        return 0

    if args.cmd == "is-leased":
        leased, record = is_leased(args.key, args.lease_dir, args.ttl, args.max_age)
        if leased:
            print("LEASED key=%s holder=%s" % (args.key, record.get("owner")))
            return 0
        print("UNLEASED key=%s" % args.key)
        return 1

    if args.cmd == "status":
        if args.key:
            leased, record = is_leased(args.key, args.lease_dir, args.ttl, args.max_age)
            print(json.dumps(record) if leased else "UNLEASED")
            return 0
        leases = [r for r in list_leases(args.lease_dir, args.ttl, args.max_age) if not r["_stale"]]
        if not leases:
            print("UNLEASED (no active leases)")
            return 0
        for r in leases:
            age = int(_now() - (r.get("heartbeat") or 0))
            print("%s owner=%s pid=%s worktree=%s heartbeat_age=%ds"
                  % (r.get("key"), r.get("owner"), r.get("pid"), r.get("worktree") or "-", age))
        return 0

    if args.cmd == "reap":
        reaped = reap_stale(args.lease_dir, args.ttl, args.max_age)
        for r in reaped:
            print("REAPED key=%s owner=%s" % (r.get("key"), r.get("owner")))
        if not reaped:
            print("nothing to reap")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
