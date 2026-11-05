#!/usr/bin/env python3
"""The parity scoreboard: per-class deep-review miss rate against CodeRabbit.

PRlaunch runs deep-review first and requires it clean before the CodeRabbit CLI
runs, so a CR CLI finding recorded on a branch AFTER that branch's first
deep-review is a miss by construction. Both gates write `gate_findings` rows to
the automation ledger (PRlaunch records deep-review's side too), so the miss set
AND the matched set are now computable from one file:

  branch   (repo with worktree suffixes folded, branch)
  t0       the branch's first deep_review row
  caught   deep_review findings on the branch
  escape   cr_cli findings with ts >= t0
           both deduped by (file, title[:80]); smoke rows excluded

Coverage discipline: a deep-reviewed branch with no CR run at or
after t0 is UNKNOWN, not clean, and is excluded from every rate; the coverage is
printed on every run because a rate over a shrinking denominator looks like
improvement. Clean CR runs are ledgered (findings_count 0), so "no cr_cli row"
really means CR never ran.

Classes are deterministic keyword/path rules seeded from a hand-labelled set of
372 major+ escapes (2026-10-16); first match wins and the rest land in an
explicit `other` bucket whose size is reported. No LLM, no network.

    hooks/cr-parity.py                       # weekly trend + repo x class table
    hooks/cr-parity.py --since 2026-09-01 --json
"""
import argparse
import collections
import datetime
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location(
    "scorecard_for_cr_parity", os.path.join(HERE, "..", "skills", "scorecard", "scorecard.py"))
_scorecard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_scorecard)
parse_ledger, parse_ts, LEDGER = _scorecard.parse_ledger, _scorecard.parse_ts, _scorecard.LEDGER

MAJOR_PLUS = ("critical", "major", "high")
WORKTREE_SUFFIX = re.compile(r"(\.dev-\d+|-dev\d+)$")
TITLE_KEY = 80

# (class, path regex, title regex): a finding matches if either matches. Order
# is precedence: the specific trust/data classes before the broad mechanical
# ones, and logic/contract last because its vocabulary is the most generic.
RULES = [
    ("secret_pii_consent", None,
     r"secret|credential|api[ _-]?key|password|\bpii\b|redact|plaintext|consent|opt[- ]?out|"
     r"unsubscribe|gdpr|tcpa|ghsa|\bcve\b|vulnerab|scrub|(email|phone|token)s?\b.*\b(log|expos|leak)"),
    ("authz_trust", None,
     r"\bauth|permission|tenant|cross[- ]?(org|tenant|account|user)|\borg[_ ]?id\b|\bscope|"
     r"trust|spoof|forg|impersonat|membership|\broles?\b|\badmin|owner|\bidor\b|signature|hmac|"
     r"csrf|ssrf|privilege|access control|bypass|(caller|client|user)[- ]supplied|\borgs?\b|"
     r"organi[sz]ation|scoping|\bmembers?\b|keychain|allowlist|\bgate\b|\bbind"),
    ("migration_backfill", r"(^|/)(alembic|migrations?)/",
     r"migration|backfill|legacy|existing rows|pre-existing (rows|data)|alembic|downgrade|"
     r"schema drift|\bexisting\b|pre-fix|forward[- ]compat|re-?key"),
    ("ci_runner", r"(^|/)\.github/|dockerfile|compose\.ya?ml|(^|/)(ops|deploy|infra|terraform)/",
     r"\bci\b|runner|workflow|deploy|provision|packag|lockfile|\bpip\b|\bnpm\b|docker|"
     r"github actions?|\benv(ironment)?\b|\bprod(uction)?\b|\bhost\b"),
    ("test_fixtures", r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|conftest",
     r"\btests?\b|fixture|\bmock|\bassert"),
    ("concurrency_stale", None,
     r"\brace|concurren|\bstale|idempot|double[- ]|twice|duplicate|\block(s|ed|ing)?\b|toctou|"
     r"atomic|in parallel|simultaneous|lost update|dedup|retr(y|ies)|replay|out[- ]of[- ]order|overwr|"
     r"collid|collision|between .* and|mid-run|re-?claim|shallow copy|shared with"),
    ("async_session", None,
     r"\basync|\bawait|event loop|blocking|\bsession|transaction|\bcommit|rollback|\btxn|thread|"
     r"connection|cursor|\bflush"),
    ("error_path", None,
     r"error|exception|\bfail|\braise|swallow|\bexcept\b|release|clean ?up|finally|partial|crash|"
     r"timeout|unhandled|fallback|\bleak|orphan|exit code|non-?zero|\blost\b|cancel|deadline|abort|"
     r"\|\| ?true|starv"),
    ("input_bounds", None,
     r"validat|bound|\blimit|overflow|negative|\bempty|\bnone\b|\bnull|regex|redos|length|\bmax|"
     r"\bmin\b|range|sanitiz|inject|escap|pars(e|ing)|encod|unicode|truncat|whitespace|"
     r"case[- ]?insensitiv|malformed|degenerate|quadratic|backtrack|\boom\b|allocat|metachar|clamp|"
     r"headroom|prefix|exact match|\bzero\b|spaces"),
    ("propagation", None,
     r"sibling|\bcallers?\b|registry|propagat|call ?sites?|also needs?|parity|mirror|out of sync|"
     r"not updated"),
    ("frontend_ui", r"\.(tsx|jsx|css|scss|vue|svelte)$|(^|/)components/",
     r"\bui\b|render|button|modal|dialog|\ba11y|aria|focus|hydrat|layout|tooltip|spinner|toast|"
     r"scroll|keyboard|click"),
    ("house_convention", None,
     r"convention|docstring|naming|claude\.md|\blint|style guide|readme|one[- ]brain|\brule\b|"
     r"swept into|copied"),
    ("domain_specific", None,
     r"\bbill|pric|\brates?\b|credit|invoice|\bpdf|font|margin|video|audio|geocod|\btax|currenc|"
     r"property|payout|stripe|usage|underbill|\btokens?\b|model|catalog|temperature|thinking|refusal|"
     r"budget|spend|cents|\bads?\b|campaign|lien|chapter|scene|grammar|persona|lore|heading|embedding|"
     r"knowledge|interview|prospect|brand"),
    ("logic_contract", None,
     r"contract|mismatch|wrong|incorrect|ignor|invert|off[- ]by[- ]one|never|always|silently|"
     r"instead of|does(n'?t| not) match|returns?\b|should|miss(es|ing)|drops?\b|unused|dead code|"
     r"not (applied|honou?red|respected)|accept|reject|exclud|preserv|honou?r|lacks?\b|omit|skip|"
     r"not recorded|keyed|mis-?classif|fake|treat|default|requir|discard|\bonly\b"),
]
_COMPILED = [(c, re.compile(p) if p else None, re.compile(t)) for c, p, t in RULES]
CLASSES = [c for c, _, _ in RULES] + ["other"]

PARITY_MISS_RATE = 0.05          # the proposed bar's major miss rate
MIN_CLASS_N = 10                 # below this a class's rate is noise


def norm_repo(repo):
    return WORKTREE_SUFFIX.sub("", str(repo or ""))


def norm_sev(s):
    """`major/minor (recurring)` -> `major`: the first word is the verdict."""
    m = re.match(r"[a-z]+", str(s or "").strip().lower())
    return m.group(0) if m else "unknown"


def major_plus(s):
    return norm_sev(s) in MAJOR_PLUS


def classify(file, title):
    path, text = str(file or "").lower(), str(title or "").lower()
    for cls, p, t in _COMPILED:
        if (p and p.search(path)) or t.search(text):
            return cls
    return "other"


def disposition(d):
    d = str(d or "").strip().lower()
    for k in ("fixed", "waived", "ticket"):
        if d.startswith(k):
            return k
    return "none"


def _is_smoke(f):
    return (str(f.get("title") or "").strip().lower() == "smoke"
            or f.get("disposition") == "waived: smoke test")


def _dedupe(runs):
    """[(ts, findings)] -> {(file, title[:80]): finding}, earliest run wins."""
    out = {}
    for _, findings in sorted(runs, key=lambda r: r[0]):
        for f in findings if isinstance(findings, list) else []:
            if not isinstance(f, dict) or _is_smoke(f):
                continue
            file = f.get("file")
            if not isinstance(file, str):
                file = str(file or "")
            out.setdefault((file, str(f.get("title") or "")[:TITLE_KEY]), f)
    return out


def branches(records):
    """Ledger records -> {(repo, branch): {t0, dr, esc, covered}}.

    t0 is None for a branch CR reviewed but deep-review never stamped: it is
    unknown, counted by coverage(), and never enters a rate.
    """
    runs = collections.defaultdict(lambda: {"deep_review": [], "cr_cli": []})
    for r in records:
        gate = r.get("gate")
        if r.get("event") != "gate_findings" or gate not in ("deep_review", "cr_cli"):
            continue
        ts, branch = parse_ts(r), r.get("branch")
        if ts is None or not branch or not isinstance(branch, str):
            continue
        runs[(norm_repo(r.get("repo")), branch)][gate].append((ts, r.get("findings")))
    out = {}
    for key, g in runs.items():
        t0 = min((ts for ts, _ in g["deep_review"]), default=None)
        after = [(ts, fs) for ts, fs in g["cr_cli"] if t0 is not None and ts >= t0]
        out[key] = {"t0": t0, "dr": _dedupe(g["deep_review"]), "esc": _dedupe(after),
                    "covered": bool(after)}
    return out


def _week(dt):
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _ratio(a, b):
    return a / b if b else None


def coverage(b, keep=lambda v: True):
    dr = [v for v in b.values() if v["t0"] is not None and keep(v)]
    cov = sum(1 for v in dr if v["covered"])
    return {"deep_reviewed": len(dr), "covered": cov, "coverage_pct": _ratio(cov, len(dr)),
            "cr_only": sum(1 for v in b.values() if v["t0"] is None)}


def _rates(vals):
    """Escape rates over the COVERED branches in `vals` (deep-reviewed ones)."""
    cov = [v for v in vals if v["covered"]]
    esc = [f for v in cov for f in v["esc"].values()]
    disp = collections.Counter(disposition(f.get("disposition")) for f in esc)
    n = len(cov)
    return {
        "deep_reviewed": len(vals),
        "covered": n,
        "coverage_pct": _ratio(n, len(vals)),
        "with_escape_pct": _ratio(sum(1 for v in cov if v["esc"]), n),
        "escapes_per_branch": _ratio(len(esc), n),
        "major_per_branch": _ratio(sum(1 for f in esc if major_plus(f.get("severity"))), n),
        "dr_per_branch": _ratio(sum(len(v["dr"]) for v in cov), n),
        "disposition": {k: disp.get(k, 0) for k in ("fixed", "waived", "ticket", "none")},
    }


def weekly(b):
    """One row per ISO week of t0."""
    by = collections.defaultdict(list)
    for v in b.values():
        if v["t0"] is not None:
            by[_week(v["t0"])].append(v)
    return [{"week": w, **_rates(by[w])} for w in sorted(by)]


def window(b, start, end):
    """Rates + major+ miss classes for branches first deep-reviewed in (start, end]."""
    vals = [v for v in b.values() if v["t0"] is not None and start < v["t0"] <= end]
    classes = collections.Counter(
        classify(f.get("file"), f.get("title"))
        for v in vals if v["covered"] for f in v["esc"].values() if major_plus(f.get("severity")))
    return {**_rates(vals), "classes": classes}


def class_table(b, since=None):
    """repo x class over covered branches (t0 >= since), major+ only.

    Every class gets a row for ALL and for every repo, zero included: a class
    silently absent reads as "no misses" when it means "never measured".
    """
    tally = collections.defaultdict(lambda: {"caught": 0, "misses": 0})
    nbr = collections.Counter()
    for (repo, _), v in b.items():
        if not v["covered"] or (since and v["t0"] < since):
            continue
        for scope in ("ALL", repo):
            nbr[scope] += 1
        for side, key in (("dr", "caught"), ("esc", "misses")):
            for f in v[side].values():
                if major_plus(f.get("severity")):
                    c = classify(f.get("file"), f.get("title"))
                    for scope in ("ALL", repo):
                        tally[(scope, c)][key] += 1
    rows = []
    for scope in ["ALL"] + sorted(s for s in nbr if s != "ALL"):
        for c in CLASSES:
            t = tally[(scope, c)]
            n = t["caught"] + t["misses"]
            rows.append({"repo": scope, "class": c, "branches": nbr[scope], **t, "n": n,
                         "miss_rate": _ratio(t["misses"], n)})
    return rows


def parity(rows):
    """Which classes deep-review matches CR on (ALL scope; `other` is no class)."""
    out = {"at_parity": [], "gap": [], "insufficient": []}
    for r in rows:
        if r["repo"] != "ALL" or r["class"] == "other":
            continue
        k = ("insufficient" if r["n"] < MIN_CLASS_N
             else "at_parity" if r["miss_rate"] < PARITY_MISS_RATE else "gap")
        out[k].append(r["class"])
    return out


def bar(b, since):
    """The proposed deprecation bar's inputs. Informational: not a decision."""
    crit = major = caught = 0
    for v in b.values():
        if not v["covered"] or (since and v["t0"] < since):
            continue
        for f in v["esc"].values():
            crit += norm_sev(f.get("severity")) == "critical"
            major += major_plus(f.get("severity"))
        caught += sum(1 for f in v["dr"].values() if major_plus(f.get("severity")))
    return {"critical_misses": crit, "major_plus_misses": major, "major_plus_caught": caught,
            "major_plus_miss_rate": _ratio(major, major + caught)}


def _pct(x):
    return "n/a" if x is None else f"{x:.0%}"


def _num(x, fmt="{:.2f}"):
    return "n/a" if x is None else fmt.format(x)


def render(weeks, cov, rows, par, bar_, since, ledger):
    out = [f"cr-parity: deep-review -> CodeRabbit CLI escapes  (ledger {ledger})", "",
           f"{'week':<9} {'dr_br':>6} {'cr_cov':>6} {'cov%':>5} {'w/esc%':>6} {'esc/br':>6} "
           f"{'maj+/br':>7} {'dr/br':>5}  fixed waived ticket none"]
    for w in weeks:
        d = w["disposition"]
        out.append(f"{w['week']:<9} {w['deep_reviewed']:>6} {w['covered']:>6} "
                   f"{_pct(w['coverage_pct']):>5} {_pct(w['with_escape_pct']):>6} "
                   f"{_num(w['escapes_per_branch']):>6} {_num(w['major_per_branch']):>7} "
                   f"{_num(w['dr_per_branch'], '{:.1f}'):>5}  {d['fixed']:>5} {d['waived']:>6} "
                   f"{d['ticket']:>6} {d['none']:>4}")
    out.append("")
    out.append(f"coverage: {cov['covered']} of {cov['deep_reviewed']} deep-reviewed branches "
               f"({_pct(cov['coverage_pct'])}) had a CR CLI run at/after their first deep-review; "
               f"the rest are unknown and excluded from every rate. {cov['cr_only']} CR-reviewed "
               f"branch(es) carry no deep-review stamp (unknown, excluded).")
    out.append("")
    out.append(f"repo x class, major+ only, covered branches first deep-reviewed since "
               f"{since.date().isoformat() if since else 'the start of the ledger'}:")
    out.append(f"{'repo':<28} {'class':<20} {'br':>5} {'caught':>6} {'misses':>6} {'n':>5} "
               f"{'miss%':>6}")
    for r in rows:
        out.append(f"{r['repo'][:28]:<28} {r['class']:<20} {r['branches']:>5} {r['caught']:>6} "
                   f"{r['misses']:>6} {r['n']:>5} {_pct(r['miss_rate']):>6}")
    other = next(r for r in rows if r["repo"] == "ALL" and r["class"] == "other")
    total = sum(r["misses"] for r in rows if r["repo"] == "ALL")
    out.append("")
    out.append(f"other (unclassified): {other['misses']} of {total} major+ misses "
               f"({_pct(_ratio(other['misses'], total))})")
    out.append(f"parity statement (informational; at parity = <{PARITY_MISS_RATE:.0%} major+ miss "
               f"rate with n>={MIN_CLASS_N}): at parity: {', '.join(par['at_parity']) or 'none'}; "
               f"gap: {', '.join(par['gap']) or 'none'}; insufficient data: "
               f"{', '.join(par['insufficient']) or 'none'}")
    out.append("proposed CR-deprecation bar (a proposal that needs the owner's sign-off; "
               "informational only, nothing is gated on it): 0 critical misses and <5% major miss "
               "rate, sustained 4 weeks over >=300 deep-reviewed diffs, CR kept in shadow. "
               f"This window: critical misses {bar_['critical_misses']}, major+ miss rate "
               f"{_pct(bar_['major_plus_miss_rate'])} ({bar_['major_plus_misses']}/"
               f"{bar_['major_plus_misses'] + bar_['major_plus_caught']}), covered branches "
               f"{other['branches']}.")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--since", help="class table: branches first deep-reviewed on/after "
                                    "YYYY-MM-DD (default: 28 days before --now)")
    ap.add_argument("--now", help="override 'now' as ISO8601 UTC (testing)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    now = (parse_ts({"ts": a.now}) if a.now else None) or datetime.datetime.now(datetime.timezone.utc)
    if a.since:
        try:
            since = datetime.datetime.fromisoformat(a.since)
            ok = since.date().isoformat() == a.since
        except ValueError:
            ok = False
        if not ok:
            ap.error(f"--since must be an exact YYYY-MM-DD date (got {a.since!r})")
        since = since.replace(tzinfo=datetime.timezone.utc)
    else:
        since = now - datetime.timedelta(days=28)

    b = branches(parse_ledger(a.ledger))
    # CR-only branches still render: their cr_only count is the coverage gap.
    if not b and not a.json:
        print(f"cr-parity: no data -- no deep_review/cr_cli gate_findings rows in {a.ledger}")
        return 0
    weeks, cov = weekly(b), coverage(b)
    rows = class_table(b, since)
    par, bar_ = parity(rows), bar(b, since)
    if a.json:
        print(json.dumps({"weekly": weeks, "coverage": cov, "classes": rows,
                          "parity": par, "bar": bar_, "since": since.isoformat()}))
    else:
        print(render(weeks, cov, rows, par, bar_, since, a.ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
