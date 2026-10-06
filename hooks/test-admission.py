#!/usr/bin/env python3
"""Machine-wide local test admission.

PreToolUse Bash gate that bounds how much pytest one Mac runs at once, for
EVERY fleet: Claude subagents, bulldozer/babysit workers, and the `claude -p`
workers Codex spawns (they load ~/.claude/settings.json hooks too -- 184
PreToolUse events in one Codex-spawned session, measured 2026-09-25).

2026-09-25: a Codex /orchestrate (5 `claude -p` workers) and a Claude /execute
(9 builders, "run the full suite with -n auto" in every brief) overlapped. 39+
full-suite runs at 14 xdist workers each (~4GB/run) took the 64GB Mac to
37.5/38.9GB swap; minute-long runs took 2h14m-2h31m and their red was thrash.
Codex throttled its own workers mid-run; that rule lived in one brief file and
never reached the other fleet. So the limit lives here, below every harness.

Two rules, DENY only:
  1. Worker cap: `-n auto|logical` or `-n N` with N > TEST_MAX_WORKERS (2).
  2. Full-suite admission: at most TEST_MAX_FULL (1) full-suite runs alive.
     "Full" = no positional path, or only a test root (tests/, .).

WHY DENY, NEVER REWRITE: PreToolUse `updatedInput` does not chain. With two
rewriting hooks on one command only one rewrite lands and the winner is
nondeterministic (measured: HOOK_A one run, HOOK_B the next), so a `-n 2`
rewrite would silently lose to bash-stdin-guard.py on any heredoc command. A
deny beat both rewrites, even under bypassPermissions.

Liveness comes from `ps`, so a crashed or finished run frees its slot with
nothing to reap. A claim file covers the gap between admission and the pytest
process appearing (cd, fetch, venv start); claims older than CLAIM_TTL are
ignored, and a session's own claims never block that same session.

Escape hatch: TEST_ADMISSION_BYPASS=1 inline or in the env (logged).
Ledger: JSONL at TEST_ADMISSION_LOG (default ~/.claude/test-admission.log).
The gate fails OPEN on its own errors -- a bug here must not brick Bash.
"""
import fcntl
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("TEST_ADMISSION_DIR", f"{HOME}/.claude/test-admission")
LEDGER = os.environ.get("TEST_ADMISSION_LOG", f"{HOME}/.claude/test-admission.log")
PS_FILE = os.environ.get("TEST_ADMISSION_PS_FILE")  # tests: stub `ps` output
CLAIM_TTL = 30


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


MAX_WORKERS = _int_env("TEST_MAX_WORKERS", 2)
MAX_FULL = _int_env("TEST_MAX_FULL", 1)

# pytest options that consume the NEXT token as their value.
VALUE_OPTS = {
    "-n", "--numprocesses", "-k", "-m", "-p", "-c", "-o", "-W", "-r",
    "--dist", "--rootdir", "--timeout", "--maxfail", "--tb", "--junitxml",
    "--junit-xml", "--basetemp", "--log-level", "--durations", "--ignore",
    "--ignore-glob", "--deselect", "--confcutdir", "--override-ini",
    "--maxprocesses", "--timeout-method", "--color", "--capture",
    "--cov", "--cov-report", "--cov-config", "--cov-fail-under",
}
# Options under which pytest collects or reports but runs no tests: exempt.
NO_RUN_OPTS = {"--collect-only", "--co", "--version", "-V", "-h", "--help",
               "--fixtures", "--markers"}
TEST_ROOTS = {"tests", "tests/", ".", "./", "./tests", "./tests/", "test", "test/"}
WRAPPERS = {"env", "nice", "nohup", "time", "exec", "command", "caffeinate"}
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&"}


def tokenize(line):
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = True
        return list(lex)
    except ValueError:  # unbalanced quote: degrade to a plain split
        return line.split()


def segments(command):
    """Yield token lists, one per simple command."""
    for line in command.replace("\\\n", " ").splitlines():
        seg = []
        for tok in tokenize(line):
            if tok in SEPARATORS:
                if seg:
                    yield seg
                seg = []
            else:
                seg.append(tok)
        if seg:
            yield seg


def _is_python(tok):
    return os.path.basename(tok).lower().startswith("python")


def _is_pytest(tok):
    return os.path.basename(tok) in ("pytest", "py.test")


def pytest_args(seg, skip_wrappers=True):
    """Args after `pytest` if seg runs pytest (in command position), else None."""
    i = 0
    while skip_wrappers and i < len(seg):
        tok = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok) or tok in WRAPPERS:
            i += 1
        elif tok == "timeout":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 2 if seg[i] in ("-s", "-k", "--signal", "--kill-after") else 1
            i += 1  # the duration
        elif tok in ("uv", "poetry", "pipenv") and i + 1 < len(seg) and seg[i + 1] == "run":
            i += 2
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
        else:
            break
    if i >= len(seg):
        return None
    head = seg[i]
    if _is_pytest(head):
        return seg[i + 1:]
    if _is_python(head):
        rest, k = seg[i + 1:], 0
        while k < len(rest):
            tok = rest[k]
            if tok == "-m":
                return rest[k + 2:] if k + 1 < len(rest) and rest[k + 1] == "pytest" else None
            if not tok.startswith("-"):  # script path: ps shows console-script pytest as `python .../pytest`
                return rest[k + 1:] if _is_pytest(tok) else None
            k += 2 if tok in ("-X", "-W") else 1  # interpreter options taking a value
    return None


def worker_count(args):
    """The -n value as a string, or None when xdist isn't requested."""
    val = None
    for k, tok in enumerate(args):
        if tok in ("-n", "--numprocesses") and k + 1 < len(args):
            val = args[k + 1]
        elif tok.startswith("--numprocesses="):
            val = tok.split("=", 1)[1]
        elif re.fullmatch(r"-n\S+", tok):
            val = tok[2:].lstrip("=")
    return val


def over_cap(val):
    if val is None:
        return False
    if val.isdigit():
        return int(val) > MAX_WORKERS
    return True  # auto / logical / anything we can't bound


def is_full_suite(args):
    positional, skip = [], False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok[:1] in (">", "<") or re.match(r"^\d?>", tok):
            break  # redirection: the rest isn't pytest's
        if tok.startswith("-"):
            skip = tok in VALUE_OPTS
            continue
        positional.append(tok)
    return all(p in TEST_ROOTS for p in positional)


def corrected(seg):
    out = " ".join(shlex.quote(t) for t in seg)
    out = re.sub(r"(?<!\S)(--numprocesses[= ]\S+|-n ?\S+)", f"-n {MAX_WORKERS}", out)
    if "--dist" not in out:
        out += " --dist loadfile"
    return out


def ps_rows():
    if PS_FILE:
        text = open(PS_FILE).read()
    else:
        text = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,command="],
                              capture_output=True, text=True, timeout=5).stdout
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            yield int(parts[0]), int(parts[1]), parts[2], parts[3]


def live_full_suites():
    """Full-suite pytest controllers: interpreter processes only (shells,
    `timeout`, and xdist workers excluded), a child of a match deduped."""
    hits = {}
    for pid, ppid, etime, cmd in ps_rows():
        toks = tokenize(cmd)
        if not toks or not (_is_python(toks[0]) or _is_pytest(toks[0])):
            continue
        args = pytest_args(toks, skip_wrappers=False)
        if args is not None and not NO_RUN_OPTS.intersection(args) and is_full_suite(args):
            hits[pid] = (ppid, etime, cmd)
    return [(pid, e, c) for pid, (ppid, e, c) in hits.items() if ppid not in hits]


def proc_cwd(pid):
    if PS_FILE:
        return "?"
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=3).stdout
        return next((ln[1:] for ln in out.splitlines() if ln.startswith("n")), "?")
    except Exception:
        return "?"


def fresh_claims(session_id):
    now, out = time.time(), []
    for name in os.listdir(STATE_DIR):
        if not name.startswith("claim-"):
            continue
        path = os.path.join(STATE_DIR, name)
        try:
            c = json.load(open(path))
        except Exception:
            continue
        if now - c.get("ts", 0) > CLAIM_TTL:
            try:
                os.remove(path)
            except OSError:
                pass
        elif c.get("session_id") != session_id:
            out.append(c)
    return out


def admit_full(session_id, cwd, command):
    """Return None if admitted (claim written), else a deny reason."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, ".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        live = live_full_suites()
        claims = fresh_claims(session_id)
        if max(len(live), len(claims)) >= MAX_FULL:
            if live:
                pid, etime, cmd = live[0]
                who = f"pid {pid}, running {etime}, cwd {proc_cwd(pid)}: {cmd[:160]}"
            else:
                c = claims[0]
                who = f"just admitted for session {c.get('session_id')} in {c.get('cwd')}"
            return (f"a full pytest suite is already running on this machine ({who}). "
                    f"Limit is {MAX_FULL} at a time. Run your targeted test "
                    f"files instead -- the full suite is CI's job -- or wait for it to finish.")
        name = f"claim-{time.time():.6f}-{os.getpid()}.json"
        with open(os.path.join(STATE_DIR, name), "w") as f:
            json.dump({"ts": time.time(), "session_id": session_id, "cwd": cwd,
                       "command": _redact(command)[:300]}, f)
    return None


def _redactor():
    """cleanup-sweep.py's credential scrubber, so a token typed inline in a
    pytest command never rests in the ledger or a claim file. Identity when the
    helper is absent (a partial vendor of hooks/)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cleanup-sweep.py")
    try:
        spec = importlib.util.spec_from_file_location("_cleanup_sweep_redact", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._redact
    except Exception:
        return lambda text: text


_REDACT = []


def _redact(text):
    if not _REDACT:  # lazy: only the deny/bypass/claim paths pay for the import
        _REDACT.append(_redactor())
    return _REDACT[0](text)


def log(decision, reason, payload, command):
    try:
        with open(LEDGER, "a") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "decision": decision,
                "reason": _redact(reason), "session_id": payload.get("session_id"),
                "cwd": payload.get("cwd"), "command": _redact(command)[:300]}) + "\n")
    except Exception:
        pass


def decide(payload):
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or "py" not in command:
        return None
    runs = [(seg, a) for seg in segments(command)
            if (a := pytest_args(seg)) is not None and not NO_RUN_OPTS.intersection(a)]
    if not runs:
        return None
    if "TEST_ADMISSION_BYPASS=1" in command or os.environ.get("TEST_ADMISSION_BYPASS") == "1":
        log("bypass", "TEST_ADMISSION_BYPASS=1", payload, command)
        return None
    reasons = []
    for seg, args in runs:
        if over_cap(worker_count(args)):
            reasons.append(
                f"`-n {worker_count(args)}` exceeds the machine-wide cap of {MAX_WORKERS} xdist "
                f"workers (parallel agent fleets on one machine exhausted swap). Re-run as: "
                f"{corrected(seg)}")
    if not reasons and any(is_full_suite(a) for _, a in runs):
        r = admit_full(payload.get("session_id"), payload.get("cwd"), command)
        if r:
            reasons.append(r)
    if not reasons:
        return None
    log("deny", " | ".join(reasons), payload, command)
    return " ".join(reasons)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        reason = decide(payload)
    except Exception as e:  # fail open, but leave a trace
        log("error", repr(e), payload if isinstance(payload, dict) else {}, "")
        return 0
    if reason:
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "permissionDecision": "deny",
                                          "permissionDecisionReason": reason}}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
