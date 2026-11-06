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
  * /execute   — PRlaunch units whose `driver` is execute, plus the split across
    every driver. Rows written before the driver field existed count as untagged.
  * escapes    — deep-review → CodeRabbit CLI escape rate for branches first
    deep-reviewed in each window, with CR coverage stated (hooks/cr-parity.py).
  * housekeeping — cleanup-queue depth now (cleanup-sweep.py --count), the
    count of prlaunch-ok orphan ledgers/markers older than 7 days, and the count
    of open `Ops:` Linear tickets with no update in 14 days (skip with --no-gh).

Output: a compact markdown table + a one-paragraph verdict that flags any metric
that regressed >20% window-over-window. Degrades gracefully — an absent/partial
ledger yields "no data yet — ledger empty" per section, never a traceback.

Design note: all ledger parsing + aggregation is PURE (no gh / no clock / no
filesystem beyond the ledger path you pass) so it can be unit-tested against a
fixture ledger. Live enrichment (gh, cleanup-sweep, orphan scan) lives in main()
and is skippable via --no-gh, so tests never touch the network.
"""
import argparse
import importlib.util
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
CR_PARITY = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "hooks", "cr-parity.py")
LINEAR_CLIENT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "done-without-evidence",
                             "done_without_evidence.py")
DRIVERS = ("execute", "orchestrate", "bulldozer", "manual")
STALE_OPS_DAYS = 14
STALE_OPS_MAX_PAGES = 20
STALE_OPS_QUERY = """
query($cutoff: DateTimeOrDuration!, $after: String) {
  issues(first: 250, after: $after, filter: {
    title: {startsWith: "Ops:"},
    state: {type: {nin: ["completed", "canceled", "duplicate"]}},
    updatedAt: {lt: $cutoff}
  }) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
"""


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
        "drivers": dict.fromkeys(DRIVERS + ("untagged",), 0),
        "wrapup": {"events": 0, "cleanup_depths": []},
        # Filled by escape_windows(): escapes bucket by a branch's FIRST
        # deep-review, which needs the whole ledger, not one window's records.
        "escapes": empty_escapes(),
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
                driver = r.get("driver")
                agg["drivers"][driver if driver in DRIVERS else "untagged"] += 1
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


ESCAPE_KEYS = ("deep_reviewed", "covered", "coverage_pct", "escape_branch_pct", "major_per_100",
               "other")


def empty_escapes(error=None):
    """Zeroed escape metrics; `error` set means cr-parity failed, not an empty ledger."""
    return dict({k: 0 for k in ESCAPE_KEYS}, classes={}, error=error)


def escape_windows(records, now, days):
    """(current, prior) deep-review escape metrics, as ints for flag_regressions.

    Branches are bucketed by their first deep-review. Each window is computed
    from the ledger as it stood at that window's end, so both windows get the
    same observation lag for CR to run; otherwise the prior window would see a
    full extra window of late CR runs and the current one would always look
    less covered. Rates are over CR-covered branches; a deep-reviewed branch CR
    never ran on is unknown (cr-parity.py's coverage discipline), hence
    coverage is a metric.
    """
    try:
        spec = importlib.util.spec_from_file_location("cr_parity", CR_PARITY)
        cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cp)
        stamped = [(parse_ts(r), r) for r in records if isinstance(r, dict)]
        windows = []
        for s, e in ((now - timedelta(days=days), now),
                     (now - timedelta(days=2 * days), now - timedelta(days=days))):
            seen = [r for ts, r in stamped if ts is not None and ts <= e]
            windows.append(cp.window(cp.branches(seen), s, e))
    except Exception as e:  # a missing/broken cr-parity.py or malformed row must not sink the whole scorecard
        print("scorecard: escapes section skipped (%s): %s" % (CR_PARITY, e), file=sys.stderr)
        err = " ".join(("%s: %s" % (type(e).__name__, e)).split()).replace("|", "/")
        return empty_escapes(err), empty_escapes(err)

    def pct(x):
        return 0 if x is None else int(round(x * 100))

    out = []
    for w in windows:
        classes = w["classes"]
        out.append({
            "deep_reviewed": w["deep_reviewed"],
            "covered": w["covered"],
            "coverage_pct": pct(w["coverage_pct"]),
            "escape_branch_pct": pct(w["with_escape_pct"]),
            "major_per_100": pct(w["major_per_branch"]),
            "other": classes.pop("other", 0),
            "classes": dict(classes),
        })
    return out[0], out[1]


# 'up' = higher is better (regress when it drops >20%);
# 'down' = higher is worse (regress when it rises >20%).
REGRESSION_CHECKS = [
    ("bulldozer shipped", ("bulldozer", "shipped"), "up"),
    ("bulldozer failed", ("bulldozer", "failed"), "down"),
    ("babysit fixes", ("babysit", "fixes"), "up"),
    ("babysit red_ci", ("babysit", "red_ci"), "down"),
    ("prlaunch cr_cli skips", ("prlaunch", "cr_cli_skipped"), "down"),
    ("prlaunch PRLAUNCH_SKIP", ("prlaunch", "prlaunch_skip"), "down"),
    ("CR coverage of deep-reviewed branches %", ("escapes", "coverage_pct"), "up"),
    ("deep-review branches w/ escape %", ("escapes", "escape_branch_pct"), "down"),
    ("deep-review major+ escapes per 100 branches", ("escapes", "major_per_100"), "down"),
]


def flag_regressions(cur_agg, prev_agg):
    """Return human strings for metrics that regressed >20% window-over-window.

    A metric with no prior baseline (prev == 0, or the section absent) is never
    flagged.
    """
    flags = []
    for name, (section, key), better in REGRESSION_CHECKS:
        cur = cur_agg.get(section, {}).get(key, 0)
        prev = prev_agg.get(section, {}).get(key, 0)
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


def stale_ops_count(now, gql=None):
    """Open `Ops:` tickets with no update in STALE_OPS_DAYS, or None if unknown.

    A failed query is None, never 0: a Linear outage must not read as a clean
    board. done_without_evidence.gql() die()s on any Linear error, hence SystemExit.
    """
    cutoff = (now - timedelta(days=STALE_OPS_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if gql is None:
            spec = importlib.util.spec_from_file_location("done_without_evidence", LINEAR_CLIENT)
            client = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(client)
            gql = client.gql
        total, after = 0, None
        for _ in range(STALE_OPS_MAX_PAGES):
            issues = gql(STALE_OPS_QUERY, {"cutoff": cutoff, "after": after})["issues"]
            total += len(issues["nodes"])
            if not issues["pageInfo"]["hasNextPage"]:
                return total
            after = issues["pageInfo"]["endCursor"]
        print("scorecard: stale Ops count gave up after %d pages" % STALE_OPS_MAX_PAGES, file=sys.stderr)
    except (Exception, SystemExit) as e:
        print("scorecard: stale Ops count unavailable: %r" % (e,), file=sys.stderr)
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


def render(cur_agg, prev_agg, pr_states, cleanup, orphans, days, flags, now, stale_ops="_skipped (--no-gh)_"):
    """Render the full markdown report. `stale_ops` None means the count is unknown."""
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

    # /execute ------------------------------------------------------------
    if section_has_data(cur_agg, prev_agg, "drivers", DRIVERS + ("untagged",)):
        row("/execute", "units shipped (driver=execute)", "drivers", "execute", "flat")
        d = cur_agg["drivers"]
        note("/execute", "PRlaunch units by driver: %s. Untagged rows predate the driver field or named none, "
             "so the /execute count is a floor while untagged is above 0"
             % " · ".join("%s %d" % (k, d[k]) for k in DRIVERS + ("untagged",)))
    else:
        note("/execute", "_no data yet — ledger empty_")

    # Deep-review escapes -------------------------------------------------
    if cur_agg["escapes"].get("error"):
        note("Deep-review escapes", "_skipped — cr-parity.py failed: %s_" % cur_agg["escapes"]["error"])
    elif section_has_data(cur_agg, prev_agg, "escapes", ("deep_reviewed",)):
        e = cur_agg["escapes"]
        row("Deep-review escapes", "CR coverage of deep-reviewed branches %", "escapes", "coverage_pct", "up")
        row("Deep-review escapes", "branches w/ escape %", "escapes", "escape_branch_pct", "down")
        row("Deep-review escapes", "major+ escapes per 100 branches", "escapes", "major_per_100", "down")
        note("Deep-review escapes", "%d of %d deep-reviewed branches CR-covered; %d unknown (no CR run, excluded)"
             % (e["covered"], e["deep_reviewed"], e["deep_reviewed"] - e["covered"]))
        top = sorted(e["classes"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        note("Deep-review escapes", "major+ miss classes: %s · other %d — full table: `hooks/cr-parity.py`"
             % (", ".join("%s %d" % kv for kv in top) or "none", e["other"]))
    else:
        note("Deep-review escapes", "_no data yet — ledger empty_")

    # Housekeeping --------------------------------------------------------
    note("Housekeeping", "cleanup queue depth now: %s" % ("n/a" if cleanup is None else cleanup))
    note("Housekeeping", "prlaunch-ok orphans >7d: %d" % orphans)
    note("Housekeeping", "stale open Ops tickets (no update in %dd): %s"
         % (STALE_OPS_DAYS, "n/a" if stale_ops is None else stale_ops))

    # Verdict -------------------------------------------------------------
    any_data = (
        section_has_data(cur_agg, prev_agg, "bulldozer", ("shipped", "resolved", "failed"))
        or section_has_data(cur_agg, prev_agg, "babysit", ("sweeps", "bumps", "fixes", "red_ci"))
        or section_has_data(cur_agg, prev_agg, "prlaunch", ("units", "cr_cli_skipped", "outcome_na", "prlaunch_skip"))
        or section_has_data(cur_agg, prev_agg, "escapes", ("deep_reviewed",))
    )
    out.append("")
    if not any_data:
        out.append("**Verdict:** no data yet — the automation ledger is empty for both windows. "
                   "The fleet either hasn't run or predates the ledger; nothing to grade.")
    elif flags:
        out.append("**Verdict:** ⚠ %d metric(s) regressed >20%% window-over-window: %s. "
                   "Investigate before the next batch."
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
    ap.add_argument("--no-gh", action="store_true",
                    help="skip live gh PR-state enrichment and the Linear stale-Ops count")
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
    cur_agg["escapes"], prev_agg["escapes"] = escape_windows(records, now, args.days)

    pr_states = None
    if not args.no_gh:
        pr_states = enrich_pr_states(
            cur_agg["bulldozer"]["shipped_prs"],
            lambda repo, pr: default_gh_runner(repo, pr, args.gh_owner),
        )

    cleanup = cleanup_depth()
    orphans = orphan_markers(now)
    flags = flag_regressions(cur_agg, prev_agg)

    kwargs = {} if args.no_gh else {"stale_ops": stale_ops_count(now)}
    print(render(cur_agg, prev_agg, pr_states, cleanup, orphans, args.days, flags, now, **kwargs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
