#!/usr/bin/env python3
"""Re-emit `✳ <aiTitle>` to VS Code terminal tabs so they survive window reloads.

Claude Code already generates a topic title (stored in the transcript as
`{"type":"ai-title","aiTitle":"..."}`) and writes `✳ <title>` to the terminal via an
OSC 0 escape sequence — but `cli.js` only does it when it detects a *new topic*
(`if (Z.isNewTopic && Z.title) QG1(Z.title)`). A VS Code reload wipes the tab title, and a
normal same-topic follow-up never re-emits it, so the tab stays blank.

Modes:
  (default / hook)  Read the Stop|UserPromptSubmit hook JSON on stdin, resolve THIS
                    session's transcript (exact `transcript_path`) + last aiTitle, find our
                    own terminal via the parent-process chain, write `✳ <title>` to it.
  --all             Retitle every open Claude tab at once. Enumerating tabs is easy (ps
                    gives pid+tty); the hard part is mapping each tty to its SESSION, since
                    tabs often share a cwd (e.g. all launched from ~) and neither the process env
                    nor the transcript records carry a tty/pid. The reliable key is TIME:
                    a claude process writes its first transcript record 1–2s after launch,
                    so we match each tab's process-start to the session whose first record
                    is closest (one-to-one, within tolerance).
  --transcript P    Force a specific transcript (testing).

WHY the parent-tty walk instead of /dev/tty: hooks (and tool subprocesses) run WITHOUT a
controlling terminal — `open('/dev/tty')` fails with ENXIO. But the hook is a descendant of
the `claude` TUI, which IS attached to a real `ttysNNN`; walking up ppid finds it.

CRITICAL: nothing is ever written to stdout — a UserPromptSubmit hook's stdout is injected
into Claude's context. Tab titles go to the tty device; the --all summary goes to stderr.
"""
import sys
import os
import glob
import json
import re
import subprocess
from datetime import datetime

PREFIX = "✳"
MAX_SCAN_BYTES = 5 * 1024 * 1024
MATCH_TOLERANCE_S = 20  # max gap between a tab's process-start and its session's first record


def _sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


# ---- transcript / title resolution -------------------------------------------------

def read_hook_input():
    if sys.stdin.isatty():
        return {}
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def project_dir_for_cwd(cwd):
    """Map a cwd to ~/.claude/projects/<sanitized>. Claude replaces '/' and '.' with '-'."""
    if not cwd:
        return None
    base = os.path.expanduser("~/.claude/projects")
    exact = os.path.join(base, re.sub(r"[/.]", "-", cwd))
    if os.path.isdir(exact):
        return exact
    tail = os.path.basename(cwd.rstrip("/")).replace(".", "-")
    cands = [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d) and tail in d]
    return max(cands, key=os.path.getmtime) if cands else None


def resolve_transcript(data):
    tp = data.get("transcript_path")
    if tp:
        tp = os.path.expanduser(tp)
        if os.path.exists(tp):
            return tp
    pd = project_dir_for_cwd(data.get("cwd"))
    for g in ([os.path.join(pd, "*.jsonl")] if pd else []) + [
        os.path.expanduser("~/.claude/projects/*/*.jsonl")
    ]:
        files = glob.glob(g)
        if files:
            return max(files, key=os.path.getmtime)
    return None


def last_ai_title(path):
    """Most recent aiTitle, found by scanning the transcript backward from EOF."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if not size:
        return None
    data = b""
    try:
        with open(path, "rb") as f:
            pos = size
            while pos > 0:
                read = min(65536, pos)
                pos -= read
                f.seek(pos)
                data = f.read(read) + data
                if b'"aiTitle"' in data:
                    for line in reversed(data.split(b"\n")):
                        if b'"aiTitle"' not in line:
                            continue
                        try:
                            o = json.loads(line.decode("utf-8", "replace"))
                        except Exception:
                            continue
                        if isinstance(o, dict) and o.get("aiTitle"):
                            return o["aiTitle"]
                if size - pos >= MAX_SCAN_BYTES:
                    break
    except Exception:
        return None
    return None


def first_timestamp(path):
    """Epoch of the earliest record in a transcript (≈ session launch time)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"timestamp"' not in line:
                    continue
                try:
                    t = json.loads(line).get("timestamp")
                except Exception:
                    continue
                if t:
                    try:
                        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        return None
    except Exception:
        return None
    return None


# ---- terminal targeting ------------------------------------------------------------

def own_tty():
    """Walk up the parent-process chain to the first ancestor on a real ttysNNN."""
    pid = os.getpid()
    for _ in range(16):
        out = _sh("ps -o ppid=,tty= -p %s" % pid).split()
        if len(out) < 2:
            break
        ppid, tty = out[0], out[1]
        if tty.startswith("ttys"):
            return "/dev/" + tty
        if ppid in ("0", "1", ""):
            break
        pid = ppid
    return None


def emit(title, tty_path):
    if not tty_path:
        return False
    # The title is model-generated text going inside an OSC sequence: strip
    # control chars (ESC, BEL, ...) so it can't terminate the sequence early
    # or inject further escapes into the terminal. That includes DEL and the C1
    # range (\x80-\x9f): some terminals honour ST (\x9c) / CSI (\x9b) directly.
    title = "".join(ch for ch in title if ch >= " " and not ("\x7f" <= ch <= "\x9f"))
    try:
        with open(tty_path, "w") as t:
            t.write("\x1b]0;{} {}\x07".format(PREFIX, title))
            t.flush()
        return True
    except Exception:
        return False


# ---- --all : retitle every open Claude tab via time-correlation ---------------------

def claude_tabs():
    """[(pid, ttysNNN, start_epoch)] for interactive claude TUIs, minus our own helpers."""
    tabs = []
    lstart_re = r"(\w{3} +\w{3} +\d+ +\d+:\d+:\d+ +\d{4})"
    for ln in _sh("ps -Ao pid=,tty=,lstart=,command=").splitlines():
        m = re.match(r"\s*(\d+)\s+(ttys\d+)\s+" + lstart_re + r"\s+(.*)", ln)
        if not m:
            continue
        pid, tty, lstart, cmd = m.groups()
        low = cmd.lower()
        if "claude" not in low or any(x in low for x in ("terminal-title.py", "mcp_server")):
            continue
        try:
            ep = datetime.strptime(re.sub(r"\s+", " ", lstart).strip(), "%a %b %d %H:%M:%S %Y").timestamp()
        except Exception:
            ep = None
        tabs.append((pid, tty, ep))
    return tabs


def cwds_for_pids(pids):
    """One batched lsof call → {pid: cwd}."""
    if not pids:
        return {}
    out = _sh("lsof -a -d cwd -p %s -Fpn 2>/dev/null" % ",".join(pids))
    res, cur = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            cur = line[1:]
        elif line.startswith("n") and cur:
            res[cur] = line[1:]
    return res


def run_all():
    tabs = claude_tabs()
    cwds = cwds_for_pids([p for p, _, _ in tabs])
    # Build a per-project-dir index of (transcript, first_ts) once.
    dir_index = {}

    def sessions_for(pd):
        if pd not in dir_index:
            dir_index[pd] = [[f, first_timestamp(f)] for f in glob.glob(os.path.join(pd, "*.jsonl"))]
        return dir_index[pd]

    used = set()
    done = []
    # Match earliest-launched tabs first so close-in-time neighbors resolve deterministically.
    for pid, tty, start in sorted(tabs, key=lambda x: (x[2] or 0)):
        pd = project_dir_for_cwd(cwds.get(pid))
        title = None
        if pd and start:
            best, best_d = None, None
            for entry in sessions_for(pd):
                f, fts = entry
                if not fts or f in used:
                    continue
                d = abs(fts - start)
                if best_d is None or d < best_d:
                    best, best_d = entry, d
            if best and best_d is not None and best_d <= MATCH_TOLERANCE_S:
                used.add(best[0])
                title = last_ai_title(best[0])
        fallback = not title
        if fallback:
            cwd = cwds.get(pid) or ""
            title = os.path.basename(cwd.rstrip("/")) or "Claude Code"
        ok = emit(title, "/dev/" + tty)
        done.append((tty, title, ok, fallback))
    return done


def run_hook(data):
    tp = resolve_transcript(data)
    title = last_ai_title(tp) if tp else None
    if not title:
        cwd = data.get("cwd") or os.getcwd()
        title = os.path.basename(cwd.rstrip("/")) or "Claude Code"
    tty = own_tty()
    if not emit(title, tty):
        emit(title, "/dev/tty")  # last-resort fallback


def main():
    args = sys.argv[1:]
    if "--all" in args:
        done = run_all()
        for tty, title, ok, fallback in sorted(done):
            if not ok:
                note = "   (write failed)"
            elif fallback:
                note = "   (no aiTitle yet — self-sets next turn)"
            else:
                note = ""
            sys.stderr.write("%s  %s %s%s\n" % (tty, PREFIX, title, note))
        emitted = sum(1 for _, _, ok, _ in done if ok)
        fell_back = sum(1 for _, _, ok, fb in done if ok and fb)
        summary = "%d tab(s) retitled" % emitted
        if fell_back:
            summary += " (%d cwd-fallback, no aiTitle yet)" % fell_back
        sys.stderr.write(summary + "\n")
        return
    data = read_hook_input()
    for i, a in enumerate(args):
        if a == "--transcript" and i + 1 < len(args):
            data["transcript_path"] = args[i + 1]
    run_hook(data)


if __name__ == "__main__":
    main()
