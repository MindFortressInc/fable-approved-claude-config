#!/usr/bin/env python3
"""Merge a batch's open PRs together, read-only, and run its tests.

Works in a detached scratch worktree off the live base tip, using the PR head
OIDs from `gh`. It pushes nothing, creates no ref, and removes only the worktree
it made. Conflicts and failures are reported, never resolved.

The scratch worktree goes under --scratch-dir, else $TRIAL_MERGE_SCRATCH_DIR,
else the directory that holds the clone (next to it, so it shares a disk and
is never in a temp dir the OS reaps).

Exit: 0 all merged, all tests passed. 1 a conflict or failing test.
2 could not run or could not clean up, so the answer is unknown.
3 all merged, but no test ran.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

GIT_TIMEOUT = 180
# Rerere could replay a recorded resolution, and repo hooks are not ours to fire.
SCRATCH_GIT = [
    "-c", "rerere.enabled=false", "-c", "core.hooksPath=/dev/null",
    "-c", "commit.gpgsign=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
    "-c", "user.name=trial-merge", "-c", "user.email=trial-merge@localhost",
]
ENV = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
OUTPUT_TAIL = 40
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class CannotRun(Exception):
    pass


def git(cwd, *args, check=True):
    try:
        p = subprocess.run(["git", *SCRATCH_GIT, "-C", cwd, *args], env=ENV, text=True, errors="replace",
                           capture_output=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise CannotRun(f"git {args[0]} exceeded {GIT_TIMEOUT}s")
    if check and p.returncode:
        raise CannotRun(f"git {args[0]} failed: {p.stderr.strip()}")
    return p


def read_prs(repo, numbers):
    prs = []
    for n in numbers:
        try:
            p = subprocess.run(
                ["gh", "pr", "view", str(n), "--json", "number,state,baseRefName,headRefName,headRefOid"],
                cwd=repo, env=ENV, text=True, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise CannotRun(f"gh pr view {n} exceeded 60s")
        if p.returncode:
            raise CannotRun(f"gh pr view {n} failed: {p.stderr.strip()}")
        prs.append(json.loads(p.stdout))
    return prs


def stack_order(prs):
    """Parents before children; otherwise the order given."""
    ordered, rest = [], list(prs)
    while rest:
        heads = {p["headRefName"] for p in rest}
        pr = next((p for p in rest if p["baseRefName"] not in heads), None)
        if pr is None:
            raise CannotRun("PR bases form a cycle")
        ordered.append(pr)
        rest.remove(pr)
    return ordered


def root_base(prs):
    heads = {p["headRefName"] for p in prs}
    roots = sorted({p["baseRefName"] for p in prs} - heads)
    if len(roots) != 1:
        raise CannotRun(f"batch must share one base outside itself, found: {', '.join(roots)}")
    return roots[0]


def changed_paths(tree, base, oid):
    return set(git(tree, "diff", "--name-only", f"{base}...{oid}").stdout.split("\n")) - {""}


def merge_all(tree, base_oid, prs):
    """Merge each PR in turn. A PR that cannot merge is aborted so the rest still get tried."""
    merged = []
    for pr in prs:
        oid = pr["headRefOid"]
        p = git(tree, "merge", "--no-ff", "--no-edit", "-m", f"trial #{pr['number']}", oid, check=False)
        if p.returncode == 0:
            pr["merge"] = "clean"
            merged.append(pr)
            continue
        pr["merge"] = "CONFLICT"
        paths = [x for x in git(tree, "diff", "--name-only", "--diff-filter=U").stdout.split("\n") if x]
        git(tree, "merge", "--abort", check=False)
        git(tree, "reset", "-q", "--hard", "HEAD")
        if not paths:
            pr["merge"] = "MERGE ERROR"
            pr["notes"].append(f"merge failed: {(p.stderr or p.stdout).strip()[:200]}")
            continue
        involved = [m for m in merged if changed_paths(tree, base_oid, m["headRefOid"]) & set(paths)]
        with_base = git(tree, "merge-tree", "--write-tree", base_oid, oid, check=False).returncode == 1
        names = [f"#{m['number']}" for m in involved] + (["base"] if with_base else [])
        pr["notes"].append(f"{', '.join(paths)} against {', '.join(names) or 'earlier PRs in the batch'}")
        for m in involved:
            m["notes"].append(f"#{pr['number']} conflicts with this on {', '.join(paths)}")
    return merged


class Running:
    """The test process group in flight, so a signal can take it down with us."""
    group = None


def run_test(tree, cmd, limit):
    proc = subprocess.Popen(cmd, shell=True, cwd=tree, env=ENV, text=True, errors="replace", start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    Running.group = proc.pid
    try:
        out, _ = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        kill_group()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:  # a daemon left the group and kept the pipe
            out = ""
        return {"cmd": cmd, "ok": False, "why": f"timed out after {limit}s", "out": out}
    finally:
        kill_group()
    return {"cmd": cmd, "ok": proc.returncode == 0, "why": f"exit {proc.returncode}", "out": out}


def kill_group():
    if Running.group is not None:
        try:
            os.killpg(Running.group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        Running.group = None


def remove_scratch(repo, parent, tree):
    """Remove exactly what this run made. Never prunes: other agents' entries are not ours."""
    for sig in SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    try:
        if os.path.exists(tree):
            git(repo, "worktree", "remove", "--force", tree)
        listed = git(repo, "worktree", "list", "--porcelain").stdout
    except CannotRun as e:
        return f"{tree}: {e}"
    if os.path.exists(tree) or f"worktree {tree}\n" in listed:
        return f"{tree}: worktree still present"
    shutil.rmtree(parent, ignore_errors=True)
    return None


def trial(args, unit_tests, prs):
    open_prs = stack_order([p for p in prs if p["state"] == "OPEN"])
    for pr in prs:
        pr["notes"] = []
    if not open_prs:
        return report(prs, None, None, [], [], False), 0
    base = root_base(open_prs)
    tip = git(args.repo, "ls-remote", "origin", f"refs/heads/{base}").stdout.split()
    if not tip:
        raise CannotRun(f"base branch {base} not found on origin")
    base_oid = tip[0]
    git(args.repo, "fetch", "--no-tags", "--no-write-fetch-head", "origin", base_oid,
        *[p["headRefOid"] for p in open_prs])

    parent = os.path.realpath(tempfile.mkdtemp(prefix=f"{os.path.basename(args.repo).lstrip('.')}.trial-merge-",
                                               dir=args.scratch_dir))
    tree = os.path.join(parent, "tree")
    leftover = None
    try:
        git(args.repo, "worktree", "add", "--detach", "-q", tree, base_oid)
        merged = merge_all(tree, base_oid, open_prs)
        for pr in merged:
            pr["tests"] = [run_test(tree, c, args.test_timeout) for c in unit_tests.get(pr["number"], [])]
        batch_tests = [run_test(tree, c, args.test_timeout) for c in args.test] if merged else []
    finally:
        leftover = remove_scratch(args.repo, parent, tree)
        if leftover:
            print(f"LEFTOVER, remove by hand: {leftover}", file=sys.stderr)
    all_tests = [t for p in merged for t in p["tests"]] + batch_tests
    failed = any(p["merge"] != "clean" for p in open_prs) or any(not t["ok"] for t in all_tests)
    code = 2 if leftover else 1 if failed else 0 if all_tests else 3
    return report(open_prs + [p for p in prs if p["state"] != "OPEN"], base, base_oid, batch_tests, all_tests, failed), code


def report(prs, base, base_oid, batch_tests, all_tests, failed):
    lines = []
    if base:
        lines.append(f"trial-merge: base {base}@{base_oid[:7]}, merge order "
                     + " ".join(f"#{p['number']}" for p in prs if p["state"] == "OPEN"))
    lines += ["| PR | head | merge | targeted tests | conflict / notes |", "|---|---|---|---|---|"]
    for p in prs:
        head = p["headRefOid"][:7]
        if p["state"] != "OPEN":
            lines.append(f"| #{p['number']} | {head} | skipped: {p['state']} | not run | |")
            continue
        tests = p.get("tests")
        cell = "not run" if p["merge"] != "clean" else "none given" if not tests else \
            "pass" if all(t["ok"] for t in tests) else "FAIL"
        lines.append(f"| #{p['number']} | {head} | {p['merge']} | {cell} | {'; '.join(p['notes'])} |")
    for t in batch_tests:
        lines.append(f"batch test {'pass' if t['ok'] else 'FAIL'}: {t['cmd']}")
    for t in all_tests:
        if not t["ok"]:
            tail = "\n".join(t["out"].splitlines()[-OUTPUT_TAIL:])
            lines.append(f"--- FAILED ({t['why']}): {t['cmd']}\n{tail}")
    if not base:
        lines.append("VERDICT: NOTHING OPEN")
    elif failed:
        held = " ".join(f"#{p['number']}" for p in prs if p.get("merge") == "clean") or "no PR"
        lines.append(f"VERDICT: FAILED (the tested tree held {held})")
    elif not all_tests:
        lines.append("VERDICT: merged clean, NO TESTS RUN")
    else:
        lines.append("VERDICT: CLEAN")
    return "\n".join(lines)


def interrupted(*_):
    raise CannotRun("interrupted by signal")


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prs", nargs="+", type=int, metavar="PR")
    ap.add_argument("--repo", default=".", help="a clone of the repo; only its object store and worktree list change")
    ap.add_argument("--scratch-dir", default=os.environ.get("TRIAL_MERGE_SCRATCH_DIR") or None,
                    help="parent of the scratch worktree (default $TRIAL_MERGE_SCRATCH_DIR, else the clone's parent)")
    ap.add_argument("--unit-test", action="append", default=[], metavar="PR=CMD", help="a unit's targeted test")
    ap.add_argument("--test", action="append", default=[], metavar="CMD", help="a batch-wide test, such as the ratchets")
    ap.add_argument("--test-timeout", type=int, default=900, help="seconds per test command")
    return ap.parse_args()


def main():
    args = parse_args()
    args.repo = os.path.abspath(args.repo)
    args.scratch_dir = os.path.abspath(os.path.expanduser(args.scratch_dir or os.path.dirname(args.repo)))
    unit_tests = {}
    try:
        for spec in args.unit_test:
            n, sep, cmd = spec.partition("=")
            if not (sep and n.isdigit() and int(n) in args.prs):
                raise CannotRun(f"--unit-test {spec!r} must be PR=CMD for a PR in this batch")
            unit_tests.setdefault(int(n), []).append(cmd)
        if not os.path.isdir(args.scratch_dir):
            raise CannotRun(f"scratch dir {args.scratch_dir} does not exist")
        for sig in SIGNALS:
            signal.signal(sig, interrupted)
        output, code = trial(args, unit_tests, read_prs(args.repo, args.prs))
    except Exception as e:  # exit 1 means "a real conflict or failure", so a crash must not land there
        print(f"trial-merge: could not run: {e if isinstance(e, CannotRun) else repr(e)}", file=sys.stderr)
        return 2
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
