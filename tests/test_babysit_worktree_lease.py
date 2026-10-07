#!/usr/bin/env python3
"""Regression tests for skills/babysit/worktree_lease.py.

Reproduces the shape that motivated the module: a detached CR-CLI relaunch
reused a PR worktree that a fixer was mid-edit in. The git-safety dirty-check
at read time did not catch it because the worktree WAS clean at the exact
instant it looked -- a TOCTOU. A clean-at-check-time dirty test is
structurally insufficient, so these tests never consult git status at all.
They exercise the actual INTERLEAVING -- a lease held by one worker while a
selector runs concurrently/overlapping with it -- to prove the exclusion
holds purely from lease state, even in the exact scenario (clean tree,
worker still present) that fooled the old check.

test_launcher_declines_leased_pr_even_though_worktree_is_clean
    the core reproduction: lease held + tree "clean" -> still declined.
test_second_worker_cannot_acquire_while_held
    mutual exclusion: a worker cannot review/edit a worktree another holds.
test_heartbeat_survives_a_long_single_step_without_manual_refresh
    caller-refresh correction: refresh must not depend on caller
    step-boundary calls, or a live holder mid-step (e.g. a slow test run)
    goes stale.
test_dead_holder_is_reaped_and_pr_becomes_selectable
    a real subprocess is acquired-and-killed; the lease must be reaped
    (holder confirmed dead) without waiting out the TTL, and the PR must
    become selectable again in the SAME call that discovers it.
"""
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "babysit")
LEASE_PY = os.path.join(SKILL_DIR, "worktree_lease.py")

sys.path.insert(0, SKILL_DIR)
import worktree_lease  # noqa: E402
from worktree_lease import (  # noqa: E402
    WorktreeLease,
    is_leased,
    filter_leased,
    reap_stale,
)


class WorktreeLeaseInterleavingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="babysit-lease-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_launcher_declines_leased_pr_even_though_worktree_is_clean(self):
        key = "acme-api-3210"
        targets = [{"repo": "acme-api", "pr": "3210", "branch": "b", "base": "main"}]

        fixer = WorktreeLease(key, lease_dir=self.tmp, owner="fixer-worker-1",
                               heartbeat_interval=0.2, ttl=5)
        self.assertTrue(fixer.acquire())
        try:
            # worktree_is_clean = True  (stand-in for `git status --porcelain`=="")
            # deliberately never consulted below -- only the lease governs
            # selection. This is what makes the test an interleaving test
            # rather than a dirty-snapshot test: the exact state that fooled
            # the old git-safety check is reproduced here, and it must still
            # decline.
            candidates, skipped = filter_leased(targets, lease_dir=self.tmp)
            self.assertEqual(candidates, [], "leased PR was selected despite a live holder")
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0][1], "fixer-worker-1")
        finally:
            fixer.release()

        # released -> selectable again
        candidates, skipped = filter_leased(targets, lease_dir=self.tmp)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(skipped, [])

    def test_concurrent_acquire_only_one_winner(self):
        """acquire() must not be its own TOCTOU: N processes racing to take
        the SAME never-before-seen key must not all succeed. Uses real
        subprocesses (not threads/mocks) so the OS actually interleaves the
        file creates.

        Each racer's own pid is the lease's watch_pid (the default when a
        caller doesn't pass one), same as a real apply-worker that stays
        alive for its whole edit-test-push cycle -- so each racer sleeps
        briefly AFTER deciding the outcome, keeping its pid alive through
        the rest of the race instead of exiting the instant it wins/loses
        (a racer that exits immediately would make its own pid look dead to
        any later racer's staleness check, which is a realism bug in the
        harness, not a bug in acquire() itself)."""
        key = "acme-api-5555"
        n = 8
        code = (
            "import sys, time; sys.path.insert(0, %r)\n"
            "from worktree_lease import WorktreeLease\n"
            "import os\n"
            "l = WorktreeLease(%r, lease_dir=%r, owner='racer-%%d' %% os.getpid(), "
            "heartbeat_interval=0.2)\n"
            "won = l.acquire()\n"
            "print('WON' if won else 'LOST'); sys.stdout.flush()\n"
            "time.sleep(1.5)\n"
        ) % (SKILL_DIR, key, self.tmp)
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                   text=True) for _ in range(n)]
        outcomes = [p.communicate(timeout=10)[0].strip() for p in procs]
        self.assertEqual(outcomes.count("WON"), 1,
                          "exactly one racer must win: %r" % outcomes)
        self.assertEqual(outcomes.count("LOST"), n - 1)

    def test_second_worker_cannot_acquire_while_held(self):
        key = "acme-api-3210"
        w1 = WorktreeLease(key, lease_dir=self.tmp, owner="w1")
        w2 = WorktreeLease(key, lease_dir=self.tmp, owner="w2")
        self.assertTrue(w1.acquire())
        try:
            self.assertFalse(w2.acquire())
        finally:
            w1.release()
        self.assertTrue(w2.acquire())
        w2.release()

    def test_heartbeat_survives_a_long_single_step_without_manual_refresh(self):
        key = "slow-test-step"
        lease = WorktreeLease(key, lease_dir=self.tmp, owner="w1",
                               heartbeat_interval=0.2, ttl=1)
        self.assertTrue(lease.acquire())
        try:
            time.sleep(1.5)  # one long synchronous step; no refresh() call made
            leased, record = is_leased(key, lease_dir=self.tmp, ttl=1)
            self.assertTrue(leased, "lease went stale mid-step despite a live holder")
        finally:
            lease.release()

    def test_dead_holder_is_reaped_and_pr_becomes_selectable(self):
        key = "acme-api-9999"
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from worktree_lease import WorktreeLease\n"
            "l = WorktreeLease(%r, lease_dir=%r, owner='dead-worker', "
            "heartbeat_interval=0.2, ttl=300)\n"
            "assert l.acquire()\n"
            "import time; time.sleep(60)\n"
        ) % (SKILL_DIR, key, self.tmp)
        proc = subprocess.Popen([sys.executable, "-c", code])
        try:
            deadline = time.time() + 5
            leased = False
            while time.time() < deadline:
                leased, _ = is_leased(key, lease_dir=self.tmp, ttl=300)
                if leased:
                    break
                time.sleep(0.1)
            self.assertTrue(leased, "worker never acquired the lease")

            proc.kill()
            proc.wait(timeout=5)
            time.sleep(0.3)  # let the OS reap the zombie / pid table settle

            # TTL is 300s -- a purely-TTL reap would still report leased here.
            # Confirming holder death must reap it immediately.
            leased, _ = is_leased(key, lease_dir=self.tmp, ttl=300)
            self.assertFalse(leased, "dead holder's lease was not reaped")

            candidates, skipped = filter_leased(
                [{"repo": "acme-api", "pr": "9999"}], lease_dir=self.tmp, ttl=300)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(skipped, [])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_abandoned_lease_with_a_live_watched_pid_expires_at_max_age(self):
        # The watched pid is the agent, which outlives a cancelled worker, so
        # the daemon keeps heartbeating and neither the dead-holder nor the
        # TTL signal fires. MAX_AGE must still expire it.
        key = "abandoned-worker"
        lease = WorktreeLease(key, lease_dir=self.tmp, owner="abandoned",
                               heartbeat_interval=0.2, ttl=300, max_age=1)
        self.assertTrue(lease.acquire())  # watches this (live) test process
        try:
            leased, _ = is_leased(key, lease_dir=self.tmp, ttl=300, max_age=1)
            self.assertTrue(leased, "a fresh lease must be live within max_age")
            time.sleep(1.5)  # daemon heartbeats throughout; holder still alive
            leased, _ = is_leased(key, lease_dir=self.tmp, ttl=300, max_age=1)
            self.assertFalse(leased, "abandoned lease survived past max_age")
            taker = WorktreeLease(key, lease_dir=self.tmp, owner="next-worker",
                                   ttl=300, max_age=1)
            self.assertTrue(taker.acquire(), "next worker could not take the expired lease")
            taker.release()
        finally:
            lease.release()

    def test_max_age_zero_disables_the_ceiling(self):
        key = "no-ceiling"
        lease = WorktreeLease(key, lease_dir=self.tmp, owner="w1",
                               heartbeat_interval=0.2, ttl=300, max_age=0)
        self.assertTrue(lease.acquire())
        try:
            time.sleep(0.5)
            leased, _ = is_leased(key, lease_dir=self.tmp, ttl=300, max_age=0)
            self.assertTrue(leased)
        finally:
            lease.release()


class StaleReapRaceTests(unittest.TestCase):
    """Two reapers can read the SAME stale record. The first unlinks it and
    takes a fresh lease; the second, still acting on its stale read, used to
    unlink by path -- deleting the first's LIVE lease -- and take its own, so
    both returned True. The interleaving is made deterministic by having
    `_read` hand the second reaper the stale record while the file on disk
    already holds the winner's fresh one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="babysit-lease-race-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.key = "acme-api-7070"
        self.path = os.path.join(self.tmp, "babysit-worktree-lease.%s.json" % self.key)
        # Ancient started/heartbeat on another host: stale by TTL and max-age.
        self.stale = {"owner": "dead-worker", "host": "elsewhere", "pid": 1,
                      "key": self.key, "started": 1.0, "heartbeat": 1.0}
        # No daemon is wanted here: a winning acquire would otherwise spawn one.
        patcher = mock.patch.object(worktree_lease, "_spawn_heartbeat_daemon",
                                    return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_live(self, owner="winner"):
        now = time.time()
        rec = {"owner": owner, "host": socket.gethostname(), "pid": os.getpid(),
               "key": self.key, "started": now, "heartbeat": now}
        with open(self.path, "w") as f:
            json.dump(rec, f)
        return rec

    def _stale_first_read(self):
        """Return the stale record on the first _read, the real file after."""
        real = worktree_lease._read
        calls = []

        def fake(path):
            calls.append(path)
            return dict(self.stale) if len(calls) == 1 else real(path)
        return mock.patch.object(worktree_lease, "_read", side_effect=fake)

    def _owner_on_disk(self):
        with open(self.path) as f:
            return json.load(f)["owner"]

    def test_second_reaper_acting_on_a_stale_read_cannot_delete_a_fresh_lease(self):
        self._write_live("winner")
        loser = WorktreeLease(self.key, lease_dir=self.tmp, owner="loser", ttl=300)
        with self._stale_first_read():
            won = loser.acquire()
        self.assertFalse(won, "both reapers won: the stale read deleted a live lease")
        self.assertEqual(self._owner_on_disk(), "winner")
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".")], [],
                         "the reap must leave no private tmp file behind")

    def test_is_leased_on_a_stale_read_does_not_delete_a_fresh_lease(self):
        self._write_live("winner")
        with self._stale_first_read():
            leased, record = is_leased(self.key, lease_dir=self.tmp, ttl=300)
        self.assertTrue(leased, "a fresh lease was reported free on a stale read")
        self.assertEqual(record["owner"], "winner")
        self.assertEqual(self._owner_on_disk(), "winner")

    def test_reap_stale_on_a_stale_read_does_not_delete_a_fresh_lease(self):
        self._write_live("winner")
        with self._stale_first_read():
            reaped = reap_stale(lease_dir=self.tmp, ttl=300)
        self.assertEqual(reaped, [])
        self.assertEqual(self._owner_on_disk(), "winner")

    def test_a_genuinely_stale_lease_is_still_reaped_and_taken(self):
        with open(self.path, "w") as f:
            json.dump(self.stale, f)
        taker = WorktreeLease(self.key, lease_dir=self.tmp, owner="taker", ttl=300)
        self.assertTrue(taker.acquire())
        self.assertEqual(self._owner_on_disk(), "taker")


class HeartbeatDaemonReleaseTests(unittest.TestCase):
    """A CLI `release` that lands between the heartbeat daemon's _read and its
    _write_atomic used to be undone: os.replace recreated the file and the
    lease was held until MAX_AGE. `release` must stop the daemon (verified by
    its command line) BEFORE unlinking."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="babysit-lease-daemon-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.watch = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self._stop, self.watch)

    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    def _cli(self, *argv, env=None):
        return subprocess.run([sys.executable, LEASE_PY] + list(argv),
                              capture_output=True, text=True, env=env, timeout=30)

    def test_cli_release_stops_the_daemon_and_the_lease_stays_gone(self):
        key = "acme-api-8080"
        path = os.path.join(self.tmp, "babysit-worktree-lease.%s.json" % key)
        env = dict(os.environ, BABYSIT_LEASE_HEARTBEAT_INTERVAL="1")
        p = self._cli("acquire", key, "--lease-dir", self.tmp, "--owner", "w8080",
                      "--watch-pid", str(self.watch.pid), env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        daemon = int(p.stdout.split("daemon_pid=")[1].split()[0])
        self.addCleanup(_kill_if_heartbeat_daemon, daemon, key)
        time.sleep(1.5)  # at least one heartbeat write has landed

        p = self._cli("release", key, "--lease-dir", self.tmp, "--owner", "w8080")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(worktree_lease._pid_alive(daemon),
                         "heartbeat daemon still running after release")
        time.sleep(1.5)  # > one heartbeat interval
        self.assertFalse(os.path.exists(path), "lease resurrected after release")

    def test_release_never_signals_a_pid_that_is_not_a_heartbeat_daemon(self):
        key = "acme-api-8081"
        path = os.path.join(self.tmp, "babysit-worktree-lease.%s.json" % key)
        bystander = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self._stop, bystander)
        now = time.time()
        with open(path, "w") as f:
            json.dump({"owner": "w8081", "host": socket.gethostname(),
                       "pid": self.watch.pid, "key": key, "started": now,
                       "heartbeat": now, "daemon_pid": bystander.pid}, f)
        p = self._cli("release", key, "--lease-dir", self.tmp, "--force")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(bystander.poll(), "release killed a non-daemon pid")


def _kill_if_heartbeat_daemon(pid, key):
    """Test cleanup: stop a daemon this test spawned, only if it still is one."""
    argv = worktree_lease._ps(pid, "command").split()
    if "_heartbeat-daemon" in argv and key in argv:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


class CliReleaseOwnerDerivationTests(unittest.TestCase):
    """The CLI `release` path's owner default used to be unsatisfiable.

    `release` used to default `owner` to `host-<its own pid>`. But a CLI
    `release` is ALWAYS a separate, short-lived process from the `acquire`
    that wrote the record (the handler's own comment says so), so that
    default could never equal the recorded `host-<acquirer pid>`. Every
    `release <key>` without `--owner` was therefore a guaranteed no-op --
    and it still returned 0, so the caller saw success while the lease
    survived. That is how one PR's lease outlived its holder by 12h and was
    declined by 6 consecutive sweeps.

    These tests drive the real CLI entry point, because the defect lives in
    the argv handler rather than in WorktreeLease itself.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="babysit-lease-cli-")
        self._daemons = []

    def tearDown(self):
        # acquire() spawns a detached heartbeat daemon; don't leak it.
        for pid in self._daemons:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cli(self, *argv):
        proc = subprocess.run(
            [sys.executable, LEASE_PY] + list(argv),
            capture_output=True, text=True,
        )
        return proc.returncode, proc.stdout.strip()

    def _acquire(self, key, owner=None):
        argv = ["acquire", key, "--lease-dir", self.tmp]
        if owner:
            argv += ["--owner", owner]
        rc, out = self._cli(*argv)
        self.assertEqual(rc, 0, out)
        for tok in out.split():
            if tok.startswith("daemon_pid="):
                self._daemons.append(int(tok.split("=", 1)[1]))
        return out

    def _lease_file(self, key):
        return os.path.join(self.tmp, "babysit-worktree-lease.%s.json" % key)

    def test_release_without_owner_does_not_silently_report_success(self):
        """The stranded-lease shape: acquire normally, then release normally."""
        key = "acme-web-881"
        self._acquire(key)

        rc, out = self._cli("release", key, "--lease-dir", self.tmp)

        # The lease is still there -- so this must NOT look like success.
        self.assertTrue(os.path.exists(self._lease_file(key)),
                        "precondition: ownerless release leaves the lease")
        self.assertNotEqual(rc, 0,
                            "ownerless release left the lease intact but "
                            "exited 0 -- the silent no-op that strands leases "
                            "(got: %r)" % out)

    def test_release_with_the_recorded_owner_actually_releases(self):
        key = "acme-42"
        self._acquire(key, owner="fixer-worker-1")

        rc, out = self._cli("release", key, "--lease-dir", self.tmp,
                            "--owner", "fixer-worker-1")

        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self._lease_file(key)), out)

    def test_release_with_a_different_owner_fails_loudly_and_keeps_the_lease(self):
        key = "acme-43"
        self._acquire(key, owner="fixer-worker-1")

        rc, out = self._cli("release", key, "--lease-dir", self.tmp,
                            "--owner", "some-other-worker")

        self.assertNotEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self._lease_file(key)),
                        "a non-owner must not be able to drop someone's lease")

    def test_release_force_drops_a_stranded_lease_regardless_of_owner(self):
        """The operator escape hatch for clearing a stranded lease by hand."""
        key = "acme-44"
        self._acquire(key, owner="dead-holder")

        rc, out = self._cli("release", key, "--lease-dir", self.tmp, "--force")

        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self._lease_file(key)), out)

    def test_release_of_an_absent_lease_is_still_a_clean_success(self):
        rc, out = self._cli("release", "never-leased", "--lease-dir", self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertIn("UNLEASED", out)


class CliDefaultWatchPidTests(unittest.TestCase):
    """The CLI's default watch pid used to be os.getppid() -- the shell that
    ran `acquire`. In a coding agent every Bash tool call is its own shell
    that exits when the call returns, so the lease recorded a dead pid and
    the next is_leased()/status reaped it while the worker was still working
    in the worktree (the worker found its own lease already gone before it
    released it).

    Drives that exact shape with real processes: a long-lived fake agent (an
    executable named `claude`, or -- via BABYSIT_LEASE_AGENT_NAMES -- another
    agent CLI, including its platform-suffixed standalone binary name) runs a
    transient shell, the shell runs `acquire` and exits, the agent keeps
    running. The lease must still be held, and must become reapable once the
    agent itself dies."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="babysit-lease-watch-")
        self.agent = None
        self._daemons = []

    def tearDown(self):
        if self.agent and self.agent.poll() is None:
            self.agent.kill()
            self.agent.wait(timeout=5)
        # The heartbeat daemon would exit on its own within one interval of
        # the fake agent dying; kill it now so none outlive the test run.
        for pid in self._daemons:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lease_taken_from_a_transient_agent_shell_outlives_that_shell(self):
        cases = (
            ("claude", None),
            ("Coding Agents/claude", None),
            ("acme-agent", "claude,acme-agent"),
            ("acme-agent-aarch64-apple-darwin", "claude,acme-agent"),
        )
        for n, (agent, names) in enumerate(cases):
            with self.subTest(agent=agent, agent_names=names):
                self._check_agent_shape(agent, names, n)

    def _check_agent_shape(self, agent, names, n):
        agent_bin = os.path.join(self.tmp, agent)
        os.makedirs(os.path.dirname(agent_bin), exist_ok=True)
        os.symlink("/bin/sh", agent_bin)
        key = "acme-web-931-%d" % n
        marker = os.path.join(self.tmp, "acquired-%d" % n)
        # The inner `; rc=$?; exit $rc` stops sh from exec'ing python in
        # place of the transient shell, which would hide the bug.
        acquire = " ".join(shlex.quote(a) for a in (
            sys.executable, LEASE_PY, "acquire", key, "--owner", "w931",
            "--lease-dir", self.tmp))
        tool_call = "%s > %s; rc=$?; exit $rc" % (acquire, shlex.quote(marker))
        env = dict(os.environ)
        env.pop("BABYSIT_LEASE_AGENT_NAMES", None)
        if names:
            env["BABYSIT_LEASE_AGENT_NAMES"] = names
        # tool_call goes in as $1, so no path is ever re-split by the outer shell.
        self.agent = subprocess.Popen(
            [agent_bin, "-c", 'sh -c "$1"; sleep 60', "agent", tool_call], env=env)
        try:
            self._assert_held_then_reapable(key, marker)
        finally:
            if self.agent.poll() is None:
                self.agent.kill()
                self.agent.wait(timeout=5)

    def _assert_held_then_reapable(self, key, marker):
        deadline = time.time() + 10
        while time.time() < deadline and "ACQUIRED" not in (
                open(marker).read() if os.path.exists(marker) else ""):
            time.sleep(0.1)
        out = open(marker).read()
        self._daemons += [int(t.split("=", 1)[1]) for t in out.split()
                          if t.startswith("daemon_pid=")]
        self.assertIn("ACQUIRED", out)
        time.sleep(0.3)  # the transient tool-call shell is gone by now

        leased, record = is_leased(key, lease_dir=self.tmp, ttl=300)
        self.assertTrue(leased, "lease reaped while its agent is still alive: %s" % out)
        self.assertEqual(record["pid"], self.agent.pid, out)

        self.agent.kill()
        self.agent.wait(timeout=5)
        time.sleep(0.3)
        leased, _ = is_leased(key, lease_dir=self.tmp, ttl=300)
        self.assertFalse(leased, "lease not reapable after its agent died")


if __name__ == "__main__":
    unittest.main()
