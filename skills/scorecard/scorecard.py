#!/usr/bin/env python3
"""scorecard.py — weekly automation quality report for the fleet.

Reads the durable automation ledger ($HOME/.claude/automation-ledger.jsonl,
appended by hooks/ledger-append.sh) and computes, over a --days window vs the
prior equal window:

  * bulldozer  — shipped / resolved / failed counts, plus merged-vs-still-open-
    vs-closed-unmerged for the shipped PRs (live `gh pr view` state per recorded
    <repo>#<pr>, retry-on-empty; skip with --no-gh).
  * babysit    — sweeps + bumps + fixes + red_ci (from its `sweep` events).
  * prlaunch   — gate integrity: cr_cli skips / outcome_eval N/A / PRLAUNCH_SKIP
    uses (from its `unit` events).
  * housekeeping — cleanup-queue depth now (cleanup-sweep.py --count) and the
    count of prlaunch-ok orphan ledgers/markers older than 7 days.

Output: a compact markdown table + a one-paragraph verdict that flags any metric
that regressed >20% window-over-window. Degrades gracefully — an absent/partial
ledger yields "no data yet — ledger empty" per section, never a traceback.

Design note: all ledger parsing + aggregation is PURE (no gh / no clock / no
filesystem beyond the ledger path you pass) so it can be unit-tested against a
fixture ledger. Live enrichment (gh, cleanup-sweep, orphan scan) lives in main()
and is skippable via --no-gh, so tests never touch the network.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
LEDGER = os.path.join(HOME, ".claude", "automation-ledger.jsonl")
PRLAUNCH_OK = os.path.join(HOME, ".claude", "prlaunch-ok")
CLEANUP_SWEEP = os.path.join(HOME, ".claude", "hooks", "cleanup-sweep.py")


# --------------------------------------------------------------------------
# Pure parsing + aggregation (unit-tested; no gh / clock / network)
# --------------------------------------------------------------------------
def parse_ledger(path):
    """Read a JSONL ledger into a list of dicts.

    Tolerates an absent file (→ []) and partial/corrupt lines (skipped), so a
    half-written record from a crashed run never breaks the report.
    """
    recs = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    recs.append(obj)
    except (FileNotFoundError, OSError):
        return []
    return recs


def parse_ts(rec):
    """Return the record's `ts` as an aware UTC datetime, or None if unusable."""
    ts = rec.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def classify(rec):
    """Map a record to its producing skill.

    babysit/prlaunch/wrapup records carry an explicit `skill`. Bulldozer appends
    the raw worker RESULT_JSON, which has no `skill` field of its own; its call
    site exports LEDGER_SKILL=bulldozer so ledger-append.sh stamps one, but rows
    written before that stamp existed have no `skill` at all and are still
    identified structurally from a `status` in the shipped/resolved/failed set.
    Keep BOTH paths: dropping the structural fallback would silently reclassify
    every historical bulldozer row as "unknown" and make window-over-window
    comparisons lie.
    """
    skill = rec.get("skill")
    if skill in ("babysit", "prlaunch", "wrapup", "bulldozer"):
        return skill
    if rec.get("status") in ("shipped", "resolved", "failed"):
        return "bulldozer"
    return "unknown"


def split_windows(records, now, days):
    """Partition records into (current, prior) equal windows by `ts`.

    current = (now-days, now]; prior = (now-2*days, now-days]. Records without a
    parseable ts belong to neither.
    """
    cur_start = now - timedelta(days=days)
    prev_start = now - timedelta(days=2 * days)
    cur, prev = [], []
    for r in records:
        ts = parse_ts(r)
        if ts is None:
            continue
        if cur_start < ts <= now:
            cur.append(r)
        elif prev_start < ts <= cur_start:
            prev.append(r)
    return cur, prev


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def aggregate(records):
    """Aggregate one window's records into a metrics dict. Pure."""
    agg = {
        "bulldozer": {"shipped": 0, "resolved": 0, "failed": 0, "shipped_prs": []},
        "babysit": {"sweeps": 0, "bumps": 0, "fixes": 0, "red_ci": 0},
        "prlaunch": {"units": 0, "cr_cli_skipped": 0, "outcome_na": 0, "prlaunch_skip": 0},
        "wrapup": {"events": 0, "cleanup_depths": []},
    }
    for r in records:
        kind = classify(r)
        if kind == "bulldozer":
            st = r.get("status")
            if st in ("shipped", "resolved", "failed"):
                agg["bulldozer"][st] += 1
            if st == "shipped":
                repo, pr = r.get("repo"), r.get("pr")
                if repo and pr is not None:
                    agg["bulldozer"]["shipped_prs"].append(
                        {"repo": repo, "pr": pr, "ticket": r.get("ticket")}
                    )
        elif kind == "babysit":
            if r.get("event") == "sweep":
                agg["babysit"]["sweeps"] += 1
                agg["babysit"]["bumps"] += _int(r.get("bumps"))
                agg["babysit"]["fixes"] += _int(r.get("fixes"))
                agg["babysit"]["red_ci"] += _int(r.get("red_ci"))
        elif kind == "prlaunch":
            if r.get("event") == "unit":
                gates = r.get("gates") or {}
                if not isinstance(gates, dict):
                    continue  # malformed unit record — skip, keep scoring the rest
                agg["prlaunch"]["units"] += 1
                cr = gates.get("cr_cli")
                if isinstance(cr, str) and cr not in ("clean", ""):
                    agg["prlaunch"]["cr_cli_skipped"] += 1
                oe = gates.get("outcome_eval")
                if isinstance(oe, str) and oe.startswith("na"):
                    agg["prlaunch"]["outcome_na"] += 1
                if gates.get("prlaunch_skip") is True:
                    agg["prlaunch"]["prlaunch_skip"] += 1
        elif kind == "wrapup":
            if r.get("event") == "cleanup_depth":
                agg["wrapup"]["events"] += 1
                agg["wrapup"]["cleanup_depths"].append(_int(r.get("count")))
    return agg


# 'up' = higher is better (regress when it drops >20%);
# 'down' = higher is worse (regress when it rises >20%).
REGRESSION_CHECKS = [
    ("bulldozer shipped", ("bulldozer", "shipped"), "up"),
    ("bulldozer failed", ("bulldozer", "failed"), "down"),
    ("babysit fixes", ("babysit", "fixes"), "up"),
    ("babysit red_ci", ("babysit", "red_ci"), "down"),
    ("prlaunch cr_cli skips", ("prlaunch", "cr_cli_skipped"), "down"),
    ("prlaunch PRLAUNCH_SKIP", ("prlaunch", "prlaunch_skip"), "down"),
]


def flag_regressions(cur_agg, prev_agg):
    """Return human strings for metrics that regressed >20% window-over-window.

    A metric with no prior baseline (prev == 0) is never flagged.
    """
    flags = []
    for name, (section, key), better in REGRESSION_CHECKS:
        cur, prev = cur_agg[section][key], prev_agg[section][key]
        if prev == 0:
            continue
        change = (cur - prev) / prev * 100.0
        if better == "up" and change < -20.0:
            flags.append("%s down %.0f%% (%d→%d)" % (name, abs(change), prev, cur))
        elif better == "down" and change > 20.0:
            flags.append("%s up %.0f%% (%d→%d)" % (name, change, prev, cur))
    return flags


def section_has_data(cur_agg, prev_agg, section, keys):
    """True if any of `keys` is nonzero in either window (else: no data yet)."""
    return any(cur_agg[section][k] or prev_agg[section][k] for k in keys)


# --------------------------------------------------------------------------
# Live enrichment (gh / cleanup-sweep / orphan scan) — skipped by --no-gh
# --------------------------------------------------------------------------
def default_gh_runner(repo, pr, owner=None):
    """Return the PR state (OPEN/MERGED/CLOSED) via `gh pr view`, retry-on-empty.

    A bare repo name (no `owner/`) resolves against `owner`; with no owner it
    returns None (counted as unknown) rather than guessing one.

    An empty response is a transient throttle, not a closed PR (mirrors babysit's
    convention), so retry >=3 before giving up. Returns None on failure.
    """
    if "/" in str(repo):
        full = str(repo)
    elif owner:
        full = "%s/%s" % (owner, repo)
    else:
        return None
    for attempt in range(3):
        try:
            out = subprocess.run(
                ["gh", "pr", "view", str(pr), "-R", full, "--json", "state", "-q", ".state"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        state = out.stdout.strip()
        if state:
            return state
        if attempt < 2:
            time.sleep(1.0)
    return None


def enrich_pr_states(shipped_prs, runner):
    """Bucket shipped PRs into merged / open / closed_unmerged / unknown."""
    counts = {"merged": 0, "open": 0, "closed_unmerged": 0, "unknown": 0}
    for ref in shipped_prs:
        state = runner(ref["repo"], ref["pr"])
        if state == "MERGED":
            counts["merged"] += 1
        elif state == "OPEN":
            counts["open"] += 1
        elif state == "CLOSED":
            counts["closed_unmerged"] += 1
        else:
            counts["unknown"] += 1
    return counts


def cleanup_depth():
    """Current cleanup-queue depth via cleanup-sweep.py --count (None on error)."""
    try:
        out = subprocess.run(
            ["python3", CLEANUP_SWEEP, "--count"],
            capture_output=True, text=True, timeout=30,
        )
        return int(out.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def orphan_markers(now, older_than_days=7):
    """Count prlaunch-ok ledgers/markers whose mtime is older than N days."""
    if not os.path.isdir(PRLAUNCH_OK):
        return 0
    cutoff = now.timestamp() - older_than_days * 86400
    n = 0
    for name in os.listdir(PRLAUNCH_OK):
        p = os.path.join(PRLAUNCH_OK, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                n += 1
        except OSError:
            continue
    return n


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _delta(prev, cur, better):
    if prev == 0 and cur == 0:
        return "—"
    if prev == 0:
        return "new"
    change = (cur - prev) / prev * 100.0
    warn = ""
    if (better == "up" and change < -20) or (better == "down" and change > 20):
        warn = " ⚠"
    sign = "+" if change >= 0 else ""
    return "%s%.0f%%%s" % (sign, change, warn)


def render(cur_agg, prev_agg, pr_states, cleanup, orphans, days, flags, now):
    """Render the full markdown report."""
    out = []
    out.append("## Automation scorecard — last %dd vs prior %dd" % (days, days))
    out.append("_generated %s_" % now.strftime("%Y-%m-%d %H:%MZ"))
    out.append("")
    out.append("| Area | Metric | This | Prior | Δ |")
    out.append("|---|---|--:|--:|--:|")

    def row(area, metric, section, key, better):
        cur, prev = cur_agg[section][key], prev_agg[section][key]
        out.append("| %s | %s | %d | %d | %s |" % (area, metric, cur, prev, _delta(prev, cur, better)))

    def note(area, text):
        out.append("| %s | %s |  |  |  |" % (area, text))

    # Bulldozer -----------------------------------------------------------
    if section_has_data(cur_agg, prev_agg, "bulldozer", ("shipped", "resolved", "failed")):
        row("Bulldozer", "shipped", "bulldozer", "shipped", "up")
        row("Bulldozer", "resolved (no PR)", "bulldozer", "resolved", "flat")
        row("Bulldozer", "failed", "bulldozer", "failed", "down")
        if pr_states is None:
            note("Bulldozer", "shipped-PR outcomes: _skipped (--no-gh)_")
        else:
            note("Bulldozer", "shipped-PR outcomes: merged %d · still-open %d · closed-unmerged %d · unknown %d"
                 % (pr_states["merged"], pr_states["open"], pr_states["closed_unmerged"], pr_states["unknown"]))
    else:
        note("Bulldozer", "_no data yet — ledger empty_")

    # Babysit -------------------------------------------------------------
    if section_has_data(cur_agg, prev_agg, "babysit", ("sweeps", "bumps", "fixes", "red_ci")):
        row("Babysit", "sweeps", "babysit", "sweeps", "flat")
        row("Babysit", "bumps", "babysit", "bumps", "flat")
        row("Babysit", "fixes applied", "babysit", "fixes", "up")
        row("Babysit", "red CI seen", "babysit", "red_ci", "down")
    else:
        note("Babysit", "_no data yet — ledger empty_")

    # PRlaunch ------------------------------------------------------------
    if section_has_data(cur_agg, prev_agg, "prlaunch", ("units", "cr_cli_skipped", "outcome_na", "prlaunch_skip")):
        row("PRlaunch", "units shipped", "prlaunch", "units", "up")
        row("PRlaunch", "cr_cli skipped", "prlaunch", "cr_cli_skipped", "down")
        row("PRlaunch", "outcome_eval N/A", "prlaunch", "outcome_na", "flat")
        row("PRlaunch", "PRLAUNCH_SKIP used", "prlaunch", "prlaunch_skip", "down")
    else:
        note("PRlaunch", "_no data yet — ledger empty_")

    # Housekeeping --------------------------------------------------------
    note("Housekeeping", "cleanup queue depth now: %s" % ("n/a" if cleanup is None else cleanup))
    note("Housekeeping", "prlaunch-ok orphans >7d: %d" % orphans)

    # Verdict -------------------------------------------------------------
    any_data = (
        section_has_data(cur_agg, prev_agg, "bulldozer", ("shipped", "resolved", "failed"))
        or section_has_data(cur_agg, prev_agg, "babysit", ("sweeps", "bumps", "fixes", "red_ci"))
        or section_has_data(cur_agg, prev_agg, "prlaunch", ("units", "cr_cli_skipped", "outcome_na", "prlaunch_skip"))
    )
    out.append("")
    if not any_data:
        out.append("**Verdict:** no data yet — the automation ledger is empty for both windows. "
                   "The fleet either hasn't run or predates the ledger; nothing to grade.")
    elif flags:
        out.append("**Verdict:** ⚠ %d metric(s) regressed >20%% window-over-window: %s. "
                   "Investigate before the next batch — if the driver model changed this window, "
                   "that is the prime suspect."
                   % (len(flags), "; ".join(flags)))
    else:
        out.append("**Verdict:** fleet healthy — no tracked metric regressed >20% window-over-window. "
                   "Shipping, sweeping, and gate integrity are holding steady.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Automation fleet scorecard.")
    ap.add_argument("--days", type=int, default=7, help="window size in days (default 7)")
    ap.add_argument("--no-gh", action="store_true", help="skip live gh PR-state enrichment")
    ap.add_argument("--gh-owner", default=os.environ.get("SCORECARD_GH_OWNER"),
                    help="GitHub owner for ledger rows that record a bare repo name "
                         "(default $SCORECARD_GH_OWNER; unset → those PRs count as unknown)")
    ap.add_argument("--ledger", default=LEDGER, help="ledger path (default ~/.claude/automation-ledger.jsonl)")
    ap.add_argument("--now", default=None, help="override 'now' as ISO8601 UTC (testing)")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    if args.now:
        override = parse_ts({"ts": args.now})
        if override is not None:
            now = override

    records = parse_ledger(args.ledger)
    cur, prev = split_windows(records, now, args.days)
    cur_agg, prev_agg = aggregate(cur), aggregate(prev)

    pr_states = None
    if not args.no_gh:
        pr_states = enrich_pr_states(
            cur_agg["bulldozer"]["shipped_prs"],
            lambda repo, pr: default_gh_runner(repo, pr, args.gh_owner),
        )

    cleanup = cleanup_depth()
    orphans = orphan_markers(now)
    flags = flag_regressions(cur_agg, prev_agg)

    print(render(cur_agg, prev_agg, pr_states, cleanup, orphans, args.days, flags, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
