#!/usr/bin/env python3
"""Share complete probe-E listings, never ticket-specific verdicts.

Called only by collision-check.sh --_pr. Exit 4 carries incomplete rows:
the shell may use a positive match, but must not infer CLEAR from them.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time


TTL = 60


def fetch(script, slug):
    return subprocess.run([script, "--_pr-list", slug], capture_output=True, text=True,
                          timeout=105)


def read_cache(path):
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        age = time.time() - data["fetched_at"]
        if 0 <= age < TTL and isinstance(data["rows"], str):
            return data["rows"]
    except (OSError, ValueError, KeyError, TypeError):
        return None  # Unreadable/corrupt cache is a miss, never an empty list.
    return None


def cache_key(script, slug):
    # gh selects env tokens before its active keychain account. Do not put the
    # token on argv, in the cache, or in diagnostics; only a digest is retained.
    auth = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                          capture_output=True, timeout=5)
    if auth.returncode or not auth.stdout.strip():
        return None
    digest = hashlib.sha256(b"collision-pr-v1\0" + auth.stdout.strip())
    for value in (slug.lower(), os.environ.get("GH_HOST", "github.com"),
                  os.environ.get("COLLISION_CHECK_RETIRED_REMOTES", ""),
                  os.environ.get("COLLISION_CHECK_OWN_ORGS", "")):
        digest.update(b"\0" + value.encode())
    digest.update(Path(script).read_bytes())
    return digest.hexdigest()


def cached_fetch(script, slug):
    directory = os.environ.get("COLLISION_CHECK_PR_CACHE_DIR",
                               str(Path(tempfile.gettempdir()) / f"collision-pr-cache-{os.getuid()}"))
    if not directory:
        return fetch(script, slug)
    try:
        root = Path(directory)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return fetch(script, slug)  # Never trust a public or symlinked cache.
        key = cache_key(script, slug)
        if key is None:
            return fetch(script, slug)
        lock_fd = os.open(root / (key + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except (OSError, subprocess.TimeoutExpired):
        return fetch(script, slug)  # Cache unavailable: still query GitHub honestly.

    with os.fdopen(lock_fd, "w") as lock:
        deadline = time.monotonic() + 110
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return subprocess.CompletedProcess([], 1, "", "PR cache refresh wait timed out\n")
                time.sleep(0.1)
        # Check under the lock: a concurrent builder may just have refreshed it.
        path = root / (key + ".json")
        rows = read_cache(path)
        if rows is not None:
            return subprocess.CompletedProcess([], 0, rows, "")
        started = time.time()
        result = fetch(script, slug)
        if result.returncode == 0:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as stream:
                    temporary = stream.name
                    json.dump({"fetched_at": started, "rows": result.stdout}, stream)
                os.replace(temporary, path)
            except OSError:
                # A cache write failure does not invalidate the live response.
                pass
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
        return result


def main():
    try:
        result = cached_fetch(sys.argv[1], sys.argv[2])
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"PR listing unavailable: {type(error).__name__}", file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode if result.returncode >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
