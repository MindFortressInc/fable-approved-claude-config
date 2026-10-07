#!/usr/bin/env python3
"""ci_rerun_mode.py -- pick the `gh run rerun` mode for a ci_triage transient re-run.

`gh run rerun <id> --failed` re-runs only the failed/cancelled jobs and REUSES
every successful job's outputs, including the CI router's (`pick runner
backend`, inline or as `<caller> / pick runner backend` when the router is a
reusable workflow). A shard the router first sent to GitHub-hosted therefore
stays on GitHub-hosted on every `--failed` re-run. Measured: a PR stayed on
`ubuntu-latest` for 4-5 `--failed` re-runs; one full `gh run rerun <id>`
re-routed every shard to the self-hosted pool and went green.

Rule -- FULL re-run (no `--failed`) only when ALL of:
  1. some failed/cancelled job in the inspected attempt ran on GitHub-hosted,
  2. the run HAS a router job (a repo without one runs on hosted by design;
     a full re-run there just repeats green jobs),
  3. the PR does not carry the fast-lane label (default `ci:fast`; read LIVE
     -- the router reads it live too, so a full re-run under it lands on
     hosted again anyway).
Anything else, including any read error, keeps `--failed` -- the fail-safe
direction. The once-only bound lives in commands/babysit-prs.md, not here.

Configuration (env, read at import):
  BABYSIT_CI_ROUTER_JOB      router job name (default "pick runner backend")
  BABYSIT_CI_ROUTER_CALLERS  comma list of reusable-workflow caller job names
                             whose `<caller> / ...` jobs count as the router
                             (default "route")
  BABYSIT_CI_FAST_LABEL      PR label that pins the router to hosted
                             (default "ci:fast")

Usage:
  ci_rerun_mode.py --repo your-org/acme-api --run <id> --pr <n> [--attempt <n>]
Prints one JSON object: {"mode": "full"|"failed", "cmd": "...", "reason": "...",
"attempt": n, "hosted_failed": [...]}. Exit 0 whenever a mode was chosen.
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROUTER_JOB = os.environ.get("BABYSIT_CI_ROUTER_JOB") or "pick runner backend"
ROUTER_CALLERS = tuple(c.strip() for c in
                       (os.environ.get("BABYSIT_CI_ROUTER_CALLERS") or "route").split(",")
                       if c.strip())
FAST_LABEL = os.environ.get("BABYSIT_CI_FAST_LABEL") or "ci:fast"
RERUN_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure"}
HOSTED_GROUP = "GitHub Actions"
HOSTED_LABEL = re.compile(r"^(ubuntu|windows|macos)-(latest|\d[\w.-]*)$", re.I)


def is_router(job):
    parts = [p.strip() for p in (job.get("name") or "").split(" / ")]
    return parts[-1] == ROUTER_JOB or (len(parts) > 1 and parts[0] in ROUTER_CALLERS)


def is_hosted(job):
    # The runner group is what actually ran the job; a self-hosted runner may
    # carry a custom `ubuntu-latest` label. Labels decide only when no runner
    # was ever assigned (group is null, e.g. a job that never started).
    group = job.get("runner_group_name")
    if group:
        return group == HOSTED_GROUP
    labels = [str(lbl) for lbl in job.get("labels") or []]
    if any(lbl.lower() == "self-hosted" for lbl in labels):
        return False
    return any(HOSTED_LABEL.match(lbl) for lbl in labels)


def decide(jobs, pr_labels):
    """Return (mode, reason, hosted_failed_job_names). Pure."""
    failed = [j for j in jobs if (j.get("conclusion") or "") in RERUN_CONCLUSIONS]
    if not failed:
        return "failed", "no failed or cancelled job in this attempt", []
    hosted = [j.get("name") or "?" for j in failed if is_hosted(j)]
    if not hosted:
        return "failed", "every failed job ran self-hosted", []
    if not any(is_router(j) for j in jobs):
        return "failed", "no router job: hosted is this repo's normal runner", hosted
    if pr_labels is None:
        return "failed", f"PR labels unreadable: cannot rule out {FAST_LABEL}", hosted
    if FAST_LABEL in pr_labels:
        return "failed", f"PR carries {FAST_LABEL}: the router would pick hosted again", hosted
    return "full", "failed job(s) ran on GitHub-hosted and a router exists: a full re-run re-routes them", hosted


def _gh(*args):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout


def fetch_jobs(repo, run, attempt):
    out = _gh("api", "--paginate", f"repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100",
              "--jq", ".jobs[] | {name, conclusion, labels, runner_group_name}")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def fetch_pr_labels(repo, pr):
    try:
        return json.loads(_gh("pr", "view", str(pr), "-R", repo, "--json", "labels", "-q", "[.labels[].name]"))
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--run", required=True, type=int)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--attempt", type=int, help="default: the run's latest attempt")
    a = ap.parse_args(argv)

    attempt, hosted = a.attempt, []
    try:
        if attempt is None:
            attempt = int(_gh("api", f"repos/{a.repo}/actions/runs/{a.run}", "--jq", ".run_attempt").strip())
        jobs = fetch_jobs(a.repo, a.run, attempt)
    # OSError: `gh` missing or not executable raises FileNotFoundError /
    # PermissionError from subprocess, not a RuntimeError -- it is a read
    # error like any other and must still print the fail-safe JSON.
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
        mode, reason = "failed", f"jobs unreadable, keeping --failed: {e}"
    else:
        mode, reason, hosted = decide(jobs, fetch_pr_labels(a.repo, a.pr))

    cmd = f"gh run rerun {a.run} -R {a.repo}" + (" --failed" if mode == "failed" else "")
    print(json.dumps({"mode": mode, "cmd": cmd, "reason": reason, "attempt": attempt, "hosted_failed": hosted}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
