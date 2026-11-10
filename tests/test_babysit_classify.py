"""Regression tests for skills/babysit/babysit_classify.py.

Two layers:

* SWEEP tests drive the real `sweep` subcommand in a subprocess. Every `gh`
  call is served by a fake `gh` shim (written into a tempdir at import) that
  reads canned JSON fixtures keyed by argv from $BABYSIT_FIXTURES. An ABSENT
  fixture prints nothing and exits 0 -- the transient-empty response the real
  gh gives under parallel bursts, which the classifier must retry and then
  treat as FETCH_FAIL. The real gh is never called.
* PURE tests call module functions in-process (failing_check_names,
  _head_advanced, harvest_ruling_comments, _ruling_authors, ...).

Core incident tests (names referenced by the docs):
  test_a_pytest_unstable_is_red_ci   UNSTABLE + failing pytest is RED, never cosmetic
  test_b_all_rate_limited_bumps      a credit-blocked queue bumps (oldest first, capped)
  test_c_empty_is_fetch_fail         empty gh output after retries is FETCH_FAIL
  test_d_hyphenated_repos_parse      hyphenated repo names parse everywhere
  test_e_stall_math                  streak climbs while frozen; rate-limited resets it
  test_f_cosmetic_only_is_yellow     a failing deploy-preview check stays yellow
  test_g_greens_carry_number_and_pr  greens rows carry BOTH "number" and "pr"

Written as unittest.TestCase so it runs under pytest and unittest alike.
"""
import atexit
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "babysit")
SCRIPT = os.path.join(SKILL_DIR, "babysit_classify.py")

# FIX_ATTEMPT_CAP is read at import: keep an exported override out of it.
os.environ.pop("BABYSIT_FIX_ATTEMPT_CAP", None)
sys.path.insert(0, SKILL_DIR)
import babysit_classify as bc  # noqa: E402

OWNER = "your-org"
AUTHOR = "babysit-bot"          # the trusted ruling/harvest login (fake)
CR = "coderabbitai[bot]"
NOW = datetime(2026, 7, 6, 20, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------------------------------------------------------------------
# fake gh shim
# ---------------------------------------------------------------------------
SHIM_SRC = r'''
"""Fake gh for babysit_classify tests: serves canned JSON fixtures keyed by
argv from $BABYSIT_FIXTURES. Absent fixture -> print nothing, exit 0."""
import json
import os
import sys

argv = sys.argv[1:]
fx = os.environ.get("BABYSIT_FIXTURES", "")
log = os.environ.get("BABYSIT_GH_LOG", "")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(argv) + "\n")


def emit(name):
    path = os.path.join(fx, name)
    if os.path.exists(path):
        with open(path) as fh:
            sys.stdout.write(fh.read())
    sys.exit(0)


def emit_search(name, args):
    """`--owner` scopes the search exactly as GitHub would."""
    want = [args[i + 1].lower() for i, a in enumerate(args)
            if a == "--owner" and i + 1 < len(args)]
    path = os.path.join(fx, name)
    if not want or not os.path.exists(path):
        emit(name)
    with open(path) as fh:
        rows = json.load(fh)
    kept = [r for r in rows
            if r["repository"]["nameWithOwner"].split("/")[0].lower() in want]
    sys.stdout.write(json.dumps(kept))
    sys.exit(0)


if not argv:
    sys.exit(0)
if argv[0] == "search":
    emit_search("search_merged.json" if "--merged" in argv else "search_open.json", argv)
elif argv[0] == "api":
    endpoint = argv[1] if len(argv) > 1 else ""
    if endpoint == "graphql" and any("viewer{pullRequests" in a for a in argv):
        # The PR-store cross-check behind an empty discovery search; a later
        # page is keyed by its `-f after=<cursor>`.
        after = [a.split("=", 1)[1] for a in argv if a.startswith("after=")]
        emit("graphql_viewer_prs%s.json" % ("_" + after[0] if after else ""))
    if endpoint == "graphql":
        f = dict(a.split("=", 1) for a in argv[2:] if "=" in a)
        emit("graphql_threads_%s_%s_%s.json" % (f.get("o", ""), f.get("r", ""), f.get("n", "")))
    endpoint = endpoint.split("?", 1)[0].strip("/")
    emit("api_" + endpoint.replace("/", "_") + ".json")
elif "pr" in argv and "view" in argv:
    owner = repo = num = ""
    if "-R" in argv:
        owner, _, repo = argv[argv.index("-R") + 1].partition("/")
    vi = argv.index("view")
    if vi + 1 < len(argv):
        num = argv[vi + 1]
    emit("prview_%s_%s_%s.json" % (owner, repo, num))
sys.exit(0)
'''

SHIM_DIR = tempfile.mkdtemp(prefix="babysit-gh-shim-")
atexit.register(shutil.rmtree, SHIM_DIR, True)
SHIM = os.path.join(SHIM_DIR, "gh")
with open(SHIM, "w") as _fh:
    _fh.write("#!" + sys.executable + "\n" + SHIM_SRC)
os.chmod(SHIM, os.stat(SHIM).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _w(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)


def default_head(num):
    return ("%07d" % num) + "abcdef" + "0" * 27


def viewer_page(nwos, cursor=None):
    """One GraphQL `viewer.pullRequests` page; `cursor` = more pages follow."""
    return {"data": {"viewer": {"pullRequests": {
        "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
        "nodes": [{"repository": {"name": n.partition("/")[2], "nameWithOwner": n}}
                  for n in nwos]}}}}


class Fixtures:
    """Builds the fixtures dir the fake gh shim reads."""

    def __init__(self, d):
        self.d = d
        self.search_open = []

    def _f(self, name):
        return os.path.join(self.d, name)

    def add_pr(self, repo, num, *, title=None, labels=None, draft=False,
               created=None, owner=OWNER, mergeable="MERGEABLE", mss="CLEAN",
               base="main", branch=None, scr=None, reviews=None, inline=None,
               issues=None, commit_date=None, write_view=True, head_oid=None,
               review_threads=None):
        """Register a PR in the open search and write its endpoint fixtures.
        write_view=False leaves every per-PR endpoint unservable (FETCH_FAIL)."""
        branch = branch or f"feat/pr-{num}"
        self.search_open.append({
            "repository": {"name": repo, "nameWithOwner": f"{owner}/{repo}"},
            "number": num, "title": title if title is not None else f"pr {num}",
            "url": f"https://example.invalid/{repo}/{num}", "isDraft": draft,
            "labels": labels or [], "createdAt": created or ago(120),
        })
        if not write_view:
            return
        _w(self._f(f"prview_{owner}_{repo}_{num}.json"), {
            "state": "OPEN", "headRefName": branch, "mergeable": mergeable,
            "mergeStateStatus": mss, "baseRefName": base,
            "statusCheckRollup": scr or [],
            "headRefOid": head_oid or default_head(num),
        })
        base_ep = f"api_repos_{owner}_{repo}"
        _w(self._f(f"{base_ep}_pulls_{num}_reviews.json"), reviews or [])
        _w(self._f(f"{base_ep}_pulls_{num}_comments.json"), inline or [])
        _w(self._f(f"{base_ep}_issues_{num}_comments.json"), issues or [])
        _w(self._f(f"{base_ep}_commits_{branch.replace('/', '_')}.json"),
           {"commit": {"committer": {"date": commit_date or ago(600)}}})
        _w(self._f(f"graphql_threads_{owner}_{repo}_{num}.json"),
           {"data": {"repository": {"pullRequest": {"reviewThreads": {
               "nodes": review_threads or []}}}}})

    def finalize(self):
        _w(self._f("search_open.json"), self.search_open)
        _w(self._f("search_merged.json"), [])
        # The PR store an empty search is cross-checked against. Mirrors
        # search_open -- a healthy GitHub, where the two backends agree.
        _w(self._f("graphql_viewer_prs.json"), viewer_page(
            [p["repository"]["nameWithOwner"] for p in self.search_open]))


def cr_issue(body, at, login=CR):
    return {"user": {"login": login}, "created_at": at, "body": body}


def cr_review(body, at, rid=None, login=CR):
    return {"id": rid, "user": {"login": login}, "submitted_at": at, "body": body}


def cr_inline(cid, body, at, reply_to=None, review_id=None, login=CR):
    return {"id": cid, "user": {"login": login}, "created_at": at,
            "in_reply_to_id": reply_to, "pull_request_review_id": review_id,
            "body": body}


def resolved_thread(root_id, resolved=True):
    return {"isResolved": resolved, "comments": {"nodes": [{"databaseId": root_id}]}}


def harvest(at, head, *, unapplied, critical=0, major=0, minor=0, trivial=0,
            login=AUTHOR):
    total = critical + major + minor + trivial
    body = ("**CodeRabbit CLI review (local)**\n"
            f"severity: critical={critical} major={major} minor={minor} trivial={trivial}\n"
            f"Reviewed head `{head[:9]}` -- {total} findings, unapplied={unapplied}\n")
    return {"user": {"login": login}, "created_at": at, "body": body}


def clean_harvest(at, head, login=AUTHOR):
    body = ("**CodeRabbit CLI review (local)**\n"
            "severity: critical=0 major=0 minor=0 trivial=0\n"
            f"Reviewed head `{head[:9]}` -- **no findings.** Clean.\n")
    return {"user": {"login": login}, "created_at": at, "body": body}


def ruling(key, at, *, authorized, reason="refuted with evidence", login=AUTHOR):
    body = bc.render_ruling_comment(key, reason, "alice", authorized_by_human=authorized)
    return {"user": {"login": login}, "created_at": at, "body": body}


NO_ACTIONABLE_BODY = "**Actionable comments posted: 0**\n\nNo actionable comments were generated."
RATE_BODY = "> [!TIP]\n> Rate limit exceeded. Please wait -- the CodeRabbit credit balance is 0."
REVIEW_LIMIT_BODY = ("> [!WARNING]\n> ## Review limit reached\n>\n"
                     "> This organization has used its review credits for this cycle.")
STACKED_BODY = "> [!NOTE]\n> Auto reviews are disabled on this stacked branch."
TRIGGERED_BODY = "> [!NOTE]\n> Review triggered.\n>\n> CodeRabbit is reviewing this PR."
MAJOR = "_\U0001f7e0 Major_\n\nThis leaks a file handle."
WIDGET_BODY = ("<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
               "[![Review Change Stack](https://example.invalid/stack.svg)]"
               "(https://example.invalid/change-stack)")


def run_sweep(fxdir, state_path, *, repos="", quiet="no:test", progress=None,
              extra_env=None, with_stderr=False, owner=OWNER):
    """Run `babysit_classify.py sweep` against the fake gh with a pinned env.
    `progress` (a dict) becomes the $BABYSIT_PROGRESS store for this run."""
    env = dict(os.environ)
    for k in ("BABYSIT_FIX_ATTEMPT_CAP", "BABYSIT_WAIVE_AUTHORIZED_BY", "BABYSIT_GH_LOG"):
        env.pop(k, None)
    progress_path = state_path + ".progress.json"
    if progress is not None:
        _w(progress_path, progress)
    env.update({
        "BABYSIT_FIXTURES": fxdir,
        "BABYSIT_NOW": NOW_ISO,
        "BABYSIT_RETRY_BACKOFF": "0",
        "BABYSIT_QUIET_OVERRIDE": quiet,
        "BABYSIT_CONCURRENCY": "1",
        "BABYSIT_PROGRESS": progress_path,
        "BABYSIT_RULING_AUTHORS": AUTHOR,
        "BABYSIT_REPOS_ROOT": os.path.join(fxdir, "nonexistent-repos-root"),
    })
    for k, v in (extra_env or {}).items():   # a None value UNSETS the var
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    args = [sys.executable, SCRIPT, "sweep", "--state", state_path,
            "--gh-bin", SHIM, "--owner", owner]
    if repos:
        args += ["--repos", repos]
    p = subprocess.run(args, capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, f"sweep rc={p.returncode}\nSTDERR:\n{p.stderr}"
    out = json.loads(p.stdout)
    return (out, p.stderr) if with_stderr else out


class SweepCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="babysit-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fx = os.path.join(self.tmp, "fx")
        os.makedirs(self.fx)
        self.state = os.path.join(self.tmp, "state.json")

    @staticmethod
    def pr(out, num):
        return next(p for p in out["prs"] if p["number"] == num)

    @staticmethod
    def acts(out, kind):
        return [a["pr"] for a in out["actions"] if a["type"] == kind]

    @staticmethod
    def tier_prs(out, tier):
        return [g["pr"] for g in out["greens"][tier]]


class InProcessCase(unittest.TestCase):
    """Pins the ruling-author env for in-process calls (other test modules
    set it at import, so it is patched per test and restored after)."""

    def setUp(self):
        p = mock.patch.dict(os.environ, {"BABYSIT_RULING_AUTHORS": AUTHOR})
        p.start()
        self.addCleanup(p.stop)


# ===========================================================================
# core regression tests
# ===========================================================================
class BabysitClassifyTests(SweepCase):
    def test_a_pytest_unstable_is_red_ci(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 101, mss="UNSTABLE",
                 issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))],
                 scr=[{"__typename": "CheckRun", "name": "pytest", "conclusion": "FAILURE"},
                      {"__typename": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS"}])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        pr = self.pr(out, 101)
        self.assertEqual(pr["state"], "CLEAN")
        self.assertEqual(pr["tier"], "red_ci")
        self.assertIn("pytest", pr["failing_checks"])
        self.assertIn(101, self.tier_prs(out, "red_ci"))
        self.assertNotIn(101, self.tier_prs(out, "cosmetic_yellow"))
        self.assertNotIn(101, self.tier_prs(out, "strict"))
        self.assertIn(101, self.acts(out, "ci_triage"))

    def test_b_all_rate_limited_bumps(self):
        f = Fixtures(self.fx)
        cands = [(200 + i, 400 - 20 * i) for i in range(1, bc.BUMP_CAP + 3)]
        for num, age in cands:
            f.add_pr("acme-api", num, mss="BLOCKED", issues=[cr_issue(RATE_BODY, ago(age))])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        bumps = [a for a in out["actions"] if a["type"] == "bump"]
        self.assertEqual([b["pr"] for b in bumps], [n for n, _ in cands][:bc.BUMP_CAP])
        for b in bumps:
            self.assertEqual(b["comments"], "@coderabbitai review")
            self.assertTrue(b["verify_open"])
        self.assertTrue(all(p["state"] == "RATE_LIMITED" for p in out["prs"]))
        self.assertEqual(out["decision"], "PROGRESSING")
        self.assertEqual(out["streak"], 0)

    def test_c_empty_is_fetch_fail(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 103, write_view=False)
        f.finalize()
        out = run_sweep(self.fx, self.state)
        pr = self.pr(out, 103)
        self.assertEqual(pr["state"], "FETCH_FAIL")
        for tier in ("strict", "cosmetic_yellow", "red_ci"):
            self.assertNotIn(103, self.tier_prs(out, tier))
        # FETCH_FAIL rows keep the full entry shape
        for key in ("cli_findings_open", "ruled_via_pr_comment", "green_via", "head_oid"):
            self.assertIn(key, pr)

    def test_d_hyphenated_repos_parse(self):
        f = Fixtures(self.fx)
        for repo, num in (("acme-api", 8), ("acme-web-app", 7), ("acme-other", 9)):
            f.add_pr(repo, num, issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))])
        f.finalize()
        out = run_sweep(self.fx, self.state, repos="acme-api,acme-web-app")
        self.assertEqual(sorted(p["repo"] for p in out["prs"]), ["acme-api", "acme-web-app"])
        for p in out["prs"]:
            self.assertEqual(p["state"], "CLEAN", f"{p['repo']} FETCH_FAILed")
            self.assertEqual(p["tier"], "strict")
            self.assertEqual(p["lane"], "owner")
        self.assertEqual(sorted(g["repo"] for g in out["greens"]["strict"]),
                         ["acme-api", "acme-web-app"])

    def test_e_stall_math(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 300, created=ago(10))       # NO_REVIEW_YET, too young to bump
        f.finalize()
        r1 = run_sweep(self.fx, self.state)
        self.assertEqual(r1["prs"][0]["state"], "NO_REVIEW_YET")
        self.assertEqual(r1["streak"], 0)
        self.assertGreater(r1["pending"], 0)
        self.assertFalse(self.acts(r1, "bump"))
        r2 = run_sweep(self.fx, self.state)
        self.assertEqual(r2["fingerprint"], r1["fingerprint"])
        self.assertEqual(r2["streak"], 1)
        self.assertEqual(r2["decision"], "PROGRESSING")
        self.assertEqual(run_sweep(self.fx, self.state)["streak"], 2)

        _w(self.state, {"pending_fingerprint": "deadbeef", "no_progress_streak": 9,
                        "pending_count": 1, "last_iter_at": NOW_ISO})
        fx2 = os.path.join(self.tmp, "fx2")
        os.makedirs(fx2)
        f2 = Fixtures(fx2)
        f2.add_pr("acme-api", 301, mss="BLOCKED", issues=[cr_issue(RATE_BODY, ago(10))])
        f2.finalize()
        r4 = run_sweep(fx2, self.state)
        self.assertEqual(r4["prs"][0]["state"], "RATE_LIMITED")
        self.assertFalse(self.acts(r4, "bump"))           # < 50 min: the STATE resets it
        self.assertEqual(r4["streak"], 0)
        self.assertEqual(r4["decision"], "PROGRESSING")

    def test_f_cosmetic_only_is_yellow(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 106, mss="UNSTABLE",
                 issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))],
                 scr=[{"__typename": "StatusContext", "context": "Vercel - acme-web",
                       "state": "FAILURE"}])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        self.assertEqual(self.pr(out, 106)["tier"], "cosmetic_yellow")
        self.assertEqual(self.pr(out, 106)["red_failing"], [])
        self.assertIn(106, self.tier_prs(out, "cosmetic_yellow"))
        self.assertNotIn(106, self.tier_prs(out, "red_ci"))

    def test_g_greens_carry_number_and_pr(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 400, issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))])
        f.finalize()
        strict = run_sweep(self.fx, self.state)["greens"]["strict"]
        self.assertEqual(len(strict), 1)
        self.assertEqual(strict[0]["pr"], 400)
        self.assertEqual(strict[0]["number"], 400)
        self.assertEqual(strict[0]["green_via"], "cloud")


# ===========================================================================
# fix 1: rate bounces (incl. "review limit reached" and review-body bounces)
# ===========================================================================
class RateBounceTests(SweepCase):
    def test_is_rate_bounce_helper(self):
        self.assertTrue(bc.is_rate_bounce(REVIEW_LIMIT_BODY))
        self.assertTrue(bc.is_rate_bounce(RATE_BODY))
        self.assertFalse(bc.is_rate_bounce(NO_ACTIONABLE_BODY))
        self.assertFalse(bc.is_rate_bounce(None))

    def test_review_limit_reached_is_rate_limited(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 4360, issues=[cr_issue(REVIEW_LIMIT_BODY, ago(5))])
        f.finalize()
        pr = self.pr(run_sweep(self.fx, self.state), 4360)
        self.assertEqual(pr["state"], "RATE_LIMITED")
        self.assertEqual(pr["tier"], "")

    def test_stale_clean_is_not_revived_by_a_newer_limit_bounce(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 4361, commit_date=ago(300),
                 issues=[cr_issue(NO_ACTIONABLE_BODY, ago(600)),
                         cr_issue(REVIEW_LIMIT_BODY, ago(100))])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        pr = self.pr(out, 4361)
        self.assertEqual(pr["state"], "RATE_LIMITED")
        self.assertEqual((pr["tier"], pr["green_via"]), ("", ""))
        self.assertIn(4361, self.acts(out, "bump"))

    def test_rate_bounce_in_a_review_body_is_rate_limited(self):
        # Security fix: a bounce submitted as a REVIEW used to fall through to
        # `cr_reviews non-empty -> CLEAN` and reach strict unreviewed.
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 4362, reviews=[cr_review(RATE_BODY, ago(90), rid=1)])
        f.add_pr("acme-api", 4363, reviews=[cr_review(REVIEW_LIMIT_BODY, ago(90), rid=2)])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        for num in (4362, 4363):
            self.assertEqual(self.pr(out, num)["state"], "RATE_LIMITED")
            self.assertNotIn(num, self.tier_prs(out, "strict"))

    def test_a_bounce_buried_by_the_bump_ack_is_still_rate_limited(self):
        """The sweep's own bump of a RATE_LIMITED PR is answered with a
        "Review triggered" ack that becomes the newest comment. The bounce is
        still the latest VERDICT, so an older clean summary must not revive."""
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 4364, commit_date=ago(300),
                 issues=[cr_issue(NO_ACTIONABLE_BODY, ago(600)),
                         cr_issue(REVIEW_LIMIT_BODY, ago(100)),
                         cr_issue(TRIGGERED_BODY, ago(50))])
        f.finalize()
        pr = self.pr(run_sweep(self.fx, self.state), 4364)
        self.assertEqual(pr["state"], "RATE_LIMITED")
        self.assertEqual(pr["tier"], "")

    def test_a_clean_summary_newer_than_the_bounce_is_clean(self):
        """Control: CodeRabbit recovered and reviewed after bouncing."""
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 4365, commit_date=ago(600),
                 issues=[cr_issue(REVIEW_LIMIT_BODY, ago(300)),
                         cr_issue(NO_ACTIONABLE_BODY, ago(100)),
                         cr_issue(TRIGGERED_BODY, ago(50))])
        f.finalize()
        pr = self.pr(run_sweep(self.fx, self.state), 4365)
        self.assertEqual(pr["state"], "CLEAN")
        self.assertEqual(pr["tier"], "strict")


# ===========================================================================
# fix 2: CR identity is an exact login allowlist
# ===========================================================================
class CrLoginAllowlistTests(SweepCase):
    def test_is_cr_exact_match(self):
        for login in ("coderabbitai[bot]", "coderabbitai", "CodeRabbitAI[bot]"):
            self.assertTrue(bc.is_cr({"user": {"login": login}}), login)
        for login in ("coderabbit-fan", "coderabbitai-helper", "not-coderabbitai[bot]", ""):
            self.assertFalse(bc.is_cr({"user": {"login": login}}), login)
        self.assertFalse(bc.is_cr({}))

    def test_lookalike_login_cannot_manufacture_clean(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 500,
                 issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20), login="coderabbit-fan")])
        f.add_pr("acme-api", 501,                       # control: the real bare login
                 issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20), login="coderabbitai")])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        self.assertEqual(self.pr(out, 500)["state"], "NO_REVIEW_YET")
        self.assertEqual(self.pr(out, 500)["tier"], "")
        self.assertNotIn(500, self.tier_prs(out, "strict"))
        self.assertEqual(self.pr(out, 501)["state"], "CLEAN")
        self.assertIn(501, self.tier_prs(out, "strict"))


# ===========================================================================
# fix 3: inline roots must be genuine, not withdrawn, not resolved
# ===========================================================================
class InlineFindingTests(SweepCase):
    def test_genuine_finding_markers(self):
        g = bc._is_genuine_cr_finding
        self.assertTrue(g(MAJOR))
        self.assertTrue(g("_\U0001f534 Critical_ boom"))
        self.assertTrue(g("<details><summary>Prompt for AI Agents</summary>fix</details>"))
        self.assertTrue(g("**Actionable comments posted: 2**"))
        self.assertFalse(g("**Actionable comments posted: 0**"))
        self.assertFalse(g(WIDGET_BODY))
        self.assertFalse(g(None))
        # CodeRabbit's category tags mark a finding even without a severity
        # tag or an AI-prompt block.
        self.assertTrue(g("_\u26a0\ufe0f Potential issue_\n\nThis crashes on None."))
        self.assertTrue(g("_\U0001f4a1 Verification agent_\n\nCheck the caller."))
        self.assertTrue(g("_\U0001f9f9 Nitpick_\n\nRename this."))

    def _pr(self, f, num, inline, threads=None):
        f.add_pr("acme-api", num, commit_date=ago(10), inline=inline,
                 review_threads=threads)

    def test_widget_root_is_not_actionable_but_genuine_finding_is(self):
        f = Fixtures(self.fx)
        self._pr(f, 761, [cr_inline(6, WIDGET_BODY, ago(5))])
        self._pr(f, 762, [cr_inline(7, MAJOR, ago(5))])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        self.assertEqual(self.pr(out, 761)["state"], "CLEAN")
        self.assertNotIn(761, self.acts(out, "fix"))
        self.assertEqual(self.pr(out, 762)["state"], "HAS_ACTIONABLE")
        self.assertIn(762, self.acts(out, "fix"))

    def test_withdrawn_roots_are_settled(self):
        prose = "You are right. **I withdraw this finding.**"
        f = Fixtures(self.fx)
        self._pr(f, 752, [cr_inline(4, MAJOR, ago(5)),
                          cr_inline(5, "Agreed.\n<!-- <review_comment_withdrawn> -->",
                                    ago(2), reply_to=4)])
        self._pr(f, 753, [cr_inline(10, MAJOR, ago(5)),
                          cr_inline(11, prose, ago(2), reply_to=10)])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        for num in (752, 753):
            self.assertEqual(self.pr(out, num)["state"], "CLEAN", num)
            self.assertNotIn(num, self.acts(out, "fix"))

    def test_resolved_thread_is_settled(self):
        f = Fixtures(self.fx)
        self._pr(f, 754, [cr_inline(20, MAJOR, ago(5)),
                          cr_inline(21, "Thanks for the context.", ago(2), reply_to=20)],
                 threads=[resolved_thread(20)])
        f.finalize()
        self.assertEqual(self.pr(run_sweep(self.fx, self.state), 754)["state"], "CLEAN")

    def test_live_findings_survive_every_detector(self):
        f = Fixtures(self.fx)
        self._pr(f, 756, [cr_inline(40, MAJOR, ago(5))],
                 threads=[resolved_thread(40, resolved=False)])
        self._pr(f, 757, [cr_inline(50, MAJOR, ago(5)),
                          cr_inline(51, "I am not withdrawing this finding.", ago(2),
                                    reply_to=50)])
        self._pr(f, 758, [cr_inline(60, MAJOR, ago(5))],
                 threads=[resolved_thread(99)])          # a DIFFERENT thread resolved
        f.add_pr("acme-api", 759, commit_date=ago(10),   # impostor login: not CR
                 inline=[cr_inline(70, MAJOR, ago(5), login="coderabbit-fan")])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        for num in (756, 757, 758):
            self.assertEqual(self.pr(out, num)["state"], "HAS_ACTIONABLE", num)
        self.assertEqual(self.pr(out, 759)["state"], "NO_REVIEW_YET")

    def test_graphql_fetched_only_when_something_is_actionable(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 770, issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))])
        self._pr(f, 771, [cr_inline(80, MAJOR, ago(5))])
        f.finalize()
        log = os.path.join(self.tmp, "gh.log")
        run_sweep(self.fx, self.state, extra_env={"BABYSIT_GH_LOG": log})
        with open(log) as fh:
            calls = [json.loads(line) for line in fh]
        gql = [c for c in calls if c[:2] == ["api", "graphql"]]
        self.assertEqual(len(gql), 1, gql)
        self.assertIn("n=771", gql[0])

    def test_stale_root_older_than_push_is_not_actionable(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 772, commit_date=ago(10), inline=[cr_inline(90, MAJOR, ago(30))])
        f.finalize()
        self.assertEqual(self.pr(run_sweep(self.fx, self.state), 772)["state"], "CLEAN")


# ===========================================================================
# fix 4: review-body findings CR could not place inline
# ===========================================================================
class BodyActionableTests(SweepCase):
    BODY1 = "**Actionable comments posted: 1**\n\nSee below."

    def test_body_count_without_inline_root_is_actionable(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 900, commit_date=ago(10),
                 reviews=[cr_review(self.BODY1, ago(5), rid=9000)])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        self.assertEqual(self.pr(out, 900)["state"], "HAS_ACTIONABLE")
        self.assertIn(900, self.acts(out, "fix"))
        self.assertNotIn(900, self.tier_prs(out, "strict"))

    def test_outside_diff_range_section_is_actionable_even_when_placed_root_resolved(self):
        body = self.BODY1 + ("\n<details><summary>Outside diff range comments (1)"
                             "</summary>stale cache key</details>")
        f = Fixtures(self.fx)
        for num, rbody in ((901, body), (902, self.BODY1)):
            rid = num * 10
            f.add_pr("acme-api", num, commit_date=ago(10),
                     reviews=[cr_review(rbody, ago(5), rid=rid)],
                     inline=[cr_inline(rid + 1, MAJOR, ago(5), review_id=rid)],
                     review_threads=[resolved_thread(rid + 1)])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        self.assertEqual(self.pr(out, 901)["state"], "HAS_ACTIONABLE")
        # control: every counted finding placed inline AND resolved -> settled
        self.assertEqual(self.pr(out, 902)["state"], "CLEAN")
        self.assertIn(902, self.tier_prs(out, "strict"))

    def test_body_count_older_than_push_or_zero_is_not_actionable(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 903, commit_date=ago(10),
                 reviews=[cr_review("**Actionable comments posted: 2**", ago(30), rid=1)])
        f.add_pr("acme-api", 904, commit_date=ago(10),
                 reviews=[cr_review(NO_ACTIONABLE_BODY, ago(5), rid=2)])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        for num in (903, 904):
            self.assertEqual(self.pr(out, num)["state"], "CLEAN", num)

    def test_unplaced_helper(self):
        inline = [cr_inline(1, MAJOR, ago(5), review_id=7)]
        u = bc._body_has_unplaced_findings
        self.assertFalse(u({"id": 7}, "actionable comments posted: 1", 1, inline))
        self.assertTrue(u({"id": 7}, "actionable comments posted: 2", 2, inline))
        self.assertTrue(u({}, "actionable comments posted: 1", 1, inline))      # no id
        self.assertTrue(u({"id": 7}, "duplicate comments (2)", 1, inline))
        self.assertTrue(u({"id": 7}, "", 1, [cr_inline(1, WIDGET_BODY, ago(5), review_id=7)]))


# ===========================================================================
# fix 5: "auto reviews are disabled" anywhere in history -> STACKED_BLOCKED
# ===========================================================================
class StackedBlockedTests(SweepCase):
    def test_buried_auto_disabled_notice_still_stacked(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 510, base="feat/pr-509",
                 issues=[cr_issue(STACKED_BODY, ago(300)),
                         cr_issue("Review finished.", ago(100))])
        f.add_pr("acme-api", 511, base="feat/pr-509",          # control: ack only
                 issues=[cr_issue("Review finished.", ago(100))])
        f.finalize()
        out = run_sweep(self.fx, self.state, quiet="yes:test")
        self.assertEqual(self.pr(out, 510)["state"], "STACKED_BLOCKED")
        self.assertIn(510, self.acts(out, "cli_launch"))
        self.assertEqual(self.pr(out, 511)["state"], "CLEAN")
        self.assertNotIn(511, self.acts(out, "cli_launch"))


# ===========================================================================
# fix 6: statusCheckRollup supersession
# ===========================================================================
def _run(name, concl, *, wf="CI", completed=None, started=None):
    return {"__typename": "CheckRun", "name": name, "conclusion": concl,
            "workflowName": wf, "completedAt": completed, "startedAt": started}


T1, T2 = "2026-07-06T08:00:00Z", "2026-07-06T09:00:00Z"


class RollupDedupeTests(SweepCase):
    def test_newer_success_supersedes_older_failure(self):
        old_bad, new_ok = _run("guards", "FAILURE", completed=T1), _run("guards", "SUCCESS", completed=T2)
        self.assertEqual(bc.failing_check_names([old_bad, new_ok]), [])
        self.assertEqual(bc.failing_check_names([new_ok, old_bad]), [])   # order irrelevant

    def test_newer_failure_stays_red(self):
        scr = [_run("guards", "SUCCESS", completed=T1), _run("guards", "FAILURE", completed=T2)]
        self.assertEqual(bc.failing_check_names(scr), ["guards"])

    def test_started_at_is_the_fallback_timestamp(self):
        scr = [_run("pytest", "FAILURE", started=T1), _run("pytest", "SUCCESS", started=T2)]
        self.assertEqual(bc.failing_check_names(scr), [])

    def test_queued_rerun_never_supersedes(self):
        scr = [_run("pytest", "FAILURE", completed=T1), _run("pytest", "", started=T2)]
        self.assertEqual(bc.failing_check_names(scr), ["pytest"])
        self.assertEqual(bc.failing_check_names([_run("pytest", "", started=T2)]), [])

    def test_app_checks_without_a_workflow_never_supersede(self):
        """Nothing in the rollup tells two apps' same-named checks apart, so
        a newer SUCCESS from one must not hide another's FAILURE."""
        scr = [_run("build", "FAILURE", wf="", completed=T1),
               _run("build", "SUCCESS", wf="", completed=T2)]
        self.assertEqual(bc.failing_check_names(scr), ["build"])
        ctx = [{"__typename": "StatusContext", "context": "deploy", "state": "FAILURE",
                "startedAt": T1},
               {"__typename": "StatusContext", "context": "deploy", "state": "SUCCESS",
                "startedAt": T2}]
        self.assertEqual(bc.failing_check_names(ctx), ["deploy"])

    def test_two_workflows_do_not_cross_launder(self):
        scr = [_run("lint", "FAILURE", wf="CI", completed=T1),
               _run("lint", "SUCCESS", wf="Frontend", completed=T2)]
        self.assertEqual(bc.failing_check_names(scr), ["lint"])

    def test_exact_tie_and_missing_timestamps_fail_closed(self):
        tie = [_run("pytest", "SUCCESS", completed=T1), _run("pytest", "FAILURE", completed=T1)]
        self.assertEqual(bc.failing_check_names(tie), ["pytest"])
        bare = [_run("pytest", "FAILURE"), _run("pytest", "SUCCESS", completed=T2)]
        self.assertEqual(bc.failing_check_names(bare), ["pytest"])

    def test_unnamed_entries_never_group(self):
        scr = [{"conclusion": "FAILURE", "completedAt": T1},
               {"conclusion": "SUCCESS", "completedAt": T2}]
        self.assertEqual(bc.failing_check_names(scr), ["?"])

    def test_cancelled_matrix_placeholder_dropped_but_failed_one_kept(self):
        ph = "pytest (${{ matrix.shard }})"
        self.assertEqual(bc.failing_check_names([_run(ph, "CANCELLED", completed=T1)]), [])
        self.assertEqual(bc.failing_check_names([_run(ph, "SKIPPED", completed=T1)]), [])
        self.assertEqual(bc.failing_check_names([_run(ph, "FAILURE", completed=T1)]), [ph])

    def test_superseded_failure_greens_in_a_sweep(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 610, issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))],
                 scr=[_run("pytest", "FAILURE", completed=T1),
                      _run("pytest", "SUCCESS", completed=T2)])
        f.add_pr("acme-api", 611, mss="UNSTABLE", issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))],
                 scr=[_run("pytest", "SUCCESS", completed=T1),
                      _run("pytest", "FAILURE", completed=T2)])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        self.assertEqual(self.pr(out, 610)["tier"], "strict")
        self.assertEqual(self.pr(out, 611)["tier"], "red_ci")


# ===========================================================================
# fix 7: a SET-but-blank BABYSIT_RULING_AUTHORS is the empty set
# ===========================================================================
class RulingAuthorsTests(unittest.TestCase):
    def test_blank_or_comma_only_is_empty(self):
        for raw in ("", "  ", ",", " , ,"):
            with mock.patch.dict(os.environ, {"BABYSIT_RULING_AUTHORS": raw}):
                self.assertEqual(bc._ruling_authors(), set(), repr(raw))

    def test_unset_trusts_nobody(self):
        """The shipped default is EMPTY: an install that never configured
        BABYSIT_RULING_AUTHORS must not trust a placeholder login anyone could
        register (it shipped as "your-github-login", an unclaimed account)."""
        self.assertEqual(bc.RULING_AUTHORS_DEFAULT, "")
        with mock.patch.dict(os.environ):
            os.environ.pop("BABYSIT_RULING_AUTHORS", None)
            self.assertEqual(bc._ruling_authors(), set())

    def test_unset_disables_ruling_harvest(self):
        cs = [ruling("src/a.py:R1", ago(5), authorized=True),
              ruling("src/b.py:R2", ago(5), authorized=True, login="your-github-login")]
        with mock.patch.dict(os.environ):
            os.environ.pop("BABYSIT_RULING_AUTHORS", None)
            self.assertEqual(bc.harvest_ruling_comments(cs), {})

    def test_list_is_trimmed_and_lowered(self):
        with mock.patch.dict(os.environ, {"BABYSIT_RULING_AUTHORS": " Alice-Bot , bob "}):
            self.assertEqual(bc._ruling_authors(), {"alice-bot", "bob"})

    def test_blank_disables_ruling_harvest(self):
        c = ruling("src/a.py:R1", ago(5), authorized=True)
        with mock.patch.dict(os.environ, {"BABYSIT_RULING_AUTHORS": ""}):
            self.assertEqual(bc.harvest_ruling_comments([c]), {})
        with mock.patch.dict(os.environ, {"BABYSIT_RULING_AUTHORS": AUTHOR}):
            self.assertIn("src/a.py:R1", bc.harvest_ruling_comments([c]))


# ===========================================================================
# fix 8: ruling rank (authorized_by_human, created_at); store never downgraded
# ===========================================================================
class RulingRankTests(InProcessCase):
    K = "src/a.py:R1"

    def _one(self, comments):
        return bc.harvest_ruling_comments(comments)[self.K]

    def test_unauthorized_newer_duplicate_cannot_revoke(self):
        auth = ruling(self.K, ago(60), authorized=True, reason="human ruled")
        twin = ruling(self.K, ago(59), authorized=False, reason="malformed twin")
        for order in ([auth, twin], [twin, auth]):
            got = self._one(order)
            self.assertTrue(got["authorized_by_human"])
            self.assertEqual(got["reason"], "human ruled")

    def test_authorized_later_ruling_still_wins(self):
        got = self._one([ruling(self.K, ago(60), authorized=False, reason="agent"),
                         ruling(self.K, ago(30), authorized=True, reason="human")])
        self.assertEqual((got["authorized_by_human"], got["reason"]), (True, "human"))
        got = self._one([ruling(self.K, ago(60), authorized=True, reason="first"),
                         ruling(self.K, ago(30), authorized=True, reason="revised")])
        self.assertEqual(got["reason"], "revised")

    def test_recency_breaks_ties_within_unauthorized_tier(self):
        got = self._one([ruling(self.K, ago(30), authorized=False, reason="newer"),
                         ruling(self.K, ago(60), authorized=False, reason="older")])
        self.assertEqual(got["reason"], "newer")

    def test_untrusted_author_is_never_parsed(self):
        spoof = ruling(self.K, ago(5), authorized=True, login="drive-by-user")
        self.assertEqual(bc.harvest_ruling_comments([spoof]), {})


class StoreAuthorizationTests(SweepCase):
    K = "src/a.py:R1"

    def _fixture(self, num):
        head = default_head(num)
        f = Fixtures(self.fx)
        f.add_pr("acme-api", num,
                 issues=[harvest(ago(5), head, unapplied=1, critical=1),
                         ruling(self.K, ago(4), authorized=False)])
        f.finalize()

    def test_store_authorization_is_not_downgraded_by_a_comment(self):
        self._fixture(801)
        store = {"waived_findings": {"acme-api#801": {
            self.K: {"reason": "human ruled", "authorized_by_human": True}}}}
        out, err = run_sweep(self.fx, self.state, progress=store, with_stderr=True)
        pr = self.pr(out, 801)
        self.assertIsNone(pr["cli_findings_open"])
        self.assertEqual((pr["tier"], pr["green_via"]), ("strict", "cli"))
        self.assertIn("keeping the store's authorization", err)
        self.assertEqual(pr["ruled_via_pr_comment"], [self.K])

    def test_without_the_store_the_critical_stays_floored(self):
        self._fixture(802)
        pr = self.pr(run_sweep(self.fx, self.state), 802)
        self.assertEqual(pr["cli_findings_open"]["findings"], 1)
        self.assertTrue(pr["cli_findings_open"]["floor_held"])
        self.assertEqual(pr["tier"], "")


# ===========================================================================
# fix 9: unauthorized-ruling floor (critical OR major OR >5 raised)
# ===========================================================================
class RulingFloorTests(SweepCase):
    K = "src/b.py:R2"

    def _add(self, f, num, *, rule=None, **sev):
        issues = [cr_issue(NO_ACTIONABLE_BODY, ago(20)),
                  harvest(ago(5), default_head(num), unapplied=1, **sev)]
        if rule is not None:
            issues.append(ruling(self.K, ago(4), authorized=rule))
        f.add_pr("acme-api", num, issues=issues)

    def test_floor_mirrors_the_store_bar(self):
        f = Fixtures(self.fx)
        self._add(f, 820, rule=False, major=1)        # unauthorized ruling on a major
        self._add(f, 821, rule=False, minor=6)        # >5 raised
        self._add(f, 822, rule=False, critical=1)
        self._add(f, 823, rule=False, minor=2)        # not risky: clears
        self._add(f, 824, rule=True, major=1)         # authorized: clears
        self._add(f, 825, major=1)                    # plain unruled major
        f.finalize()
        out = run_sweep(self.fx, self.state)
        for num in (820, 821, 822):
            pr = self.pr(out, num)
            self.assertEqual(pr["cli_findings_open"]["findings"], 1, num)
            self.assertTrue(pr["cli_findings_open"]["floor_held"], num)
            self.assertEqual(pr["tier"], "", num)
            self.assertNotIn(num, self.tier_prs(out, "strict"))
        for num in (823, 824):
            pr = self.pr(out, num)
            self.assertIsNone(pr["cli_findings_open"], num)
            self.assertEqual(pr["tier"], "strict", num)
        plain = self.pr(out, 825)
        self.assertTrue(plain["cli_findings_open"]["major"])
        self.assertNotIn("floor_held", plain["cli_findings_open"])
        self.assertEqual(plain["tier"], "strict", "a plain unruled major still greens")

    def test_severity_parsers(self):
        self.assertTrue(bc._has_major("severity: critical=0 major=1 minor=0 trivial=0"))
        self.assertFalse(bc._has_major("severity: critical=0 major=0 minor=3 trivial=0"))
        self.assertEqual(bc._total_raised("critical=1 major=2 minor=3 trivial=0"), 6)
        self.assertIsNone(bc._total_raised("major=2 only"))


# ===========================================================================
# fix 10: a clean CLI harvest does not green while the cloud is TRIGGERED_WAITING
# ===========================================================================
class TriggeredWaitingTests(SweepCase):
    def test_triggered_waiting_blocks_cli_green(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 830, issues=[clean_harvest(ago(10), default_head(830)),
                                          cr_issue(TRIGGERED_BODY, ago(5))])
        f.add_pr("acme-api", 831, issues=[clean_harvest(ago(10), default_head(831))])
        f.finalize()
        out = run_sweep(self.fx, self.state)
        waiting = self.pr(out, 830)
        self.assertEqual(waiting["state"], "TRIGGERED_WAITING")
        self.assertEqual((waiting["tier"], waiting["green_via"]), ("", ""))
        control = self.pr(out, 831)
        self.assertEqual(control["state"], "NO_REVIEW_YET")
        self.assertEqual((control["tier"], control["green_via"]), ("strict", "cli"))

    def test_harvest_for_another_head_does_not_green(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 832, issues=[clean_harvest(ago(10), "1234567" + "f" * 33)])
        f.finalize()
        self.assertEqual(self.pr(run_sweep(self.fx, self.state), 832)["tier"], "")




# ===========================================================================
# an UNSET BABYSIT_RULING_AUTHORS trusts no CR-CLI harvest (fail closed)
# ===========================================================================
class UnsetRulingAuthorsSweepTests(SweepCase):
    def test_unset_ruling_authors_trusts_no_harvest(self):
        """With BABYSIT_RULING_AUTHORS unset, no clean CR-CLI harvest greens a
        PR -- not even one from the old placeholder login."""
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 833, issues=[clean_harvest(ago(10), default_head(833))])
        f.add_pr("acme-api", 834, issues=[clean_harvest(ago(10), default_head(834),
                                                        login="your-github-login")])
        f.finalize()
        out = run_sweep(self.fx, self.state, extra_env={"BABYSIT_RULING_AUTHORS": None})
        for num in (833, 834):
            with self.subTest(pr=num):
                self.assertEqual((self.pr(out, num)["tier"], self.pr(out, num)["green_via"]),
                                 ("", ""))
        control = run_sweep(self.fx, self.state)          # AUTHOR trusted
        self.assertEqual((self.pr(control, 833)["tier"], self.pr(control, 833)["green_via"]),
                         ("strict", "cli"))


# ===========================================================================
# fix 11: never bump a PR already in a green tier
# ===========================================================================
class BumpGreenExclusionTests(SweepCase):
    def test_green_rate_limited_pr_is_not_bumped(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 840, issues=[cr_issue(RATE_BODY, ago(120)),
                                          clean_harvest(ago(10), default_head(840))])
        f.add_pr("acme-api", 841, issues=[cr_issue(RATE_BODY, ago(120))])   # control
        f.finalize()
        out = run_sweep(self.fx, self.state)
        green = self.pr(out, 840)
        self.assertEqual((green["state"], green["tier"]), ("RATE_LIMITED", "strict"))
        self.assertNotIn(840, self.acts(out, "bump"))
        self.assertEqual(self.pr(out, 841)["tier"], "")
        self.assertIn(841, self.acts(out, "bump"))


# ===========================================================================
# fix 12: ruled_via_pr_comment telemetry deduped across sweeps
# ===========================================================================
class RulingTelemetryDedupeTests(SweepCase):
    def test_each_ruling_reported_once(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 850, issues=[ruling("k1", ago(30), authorized=False)])
        f.finalize()
        r1 = run_sweep(self.fx, self.state)
        self.assertEqual([(r["pr"], r["findings"]) for r in r1["ruled_via_pr_comment"]],
                         [(850, ["k1"])])
        with open(self.state) as fh:
            self.assertEqual(json.load(fh)["reported_rulings"], ["acme-api#850:k1"])
        r2 = run_sweep(self.fx, self.state)
        self.assertEqual(r2["ruled_via_pr_comment"], [])
        self.assertEqual(self.pr(r2, 850)["ruled_via_pr_comment"], ["k1"])  # per-PR stays

        f2 = Fixtures(self.fx)
        f2.add_pr("acme-api", 850, issues=[ruling("k1", ago(30), authorized=False),
                                           ruling("k2", ago(10), authorized=False)])
        f2.finalize()
        r3 = run_sweep(self.fx, self.state)
        self.assertEqual([(r["pr"], r["findings"]) for r in r3["ruled_via_pr_comment"]],
                         [(850, ["k2"])])


# ===========================================================================
# fix 13: fix/rebase attempt cap with head-move re-arm
# ===========================================================================
class AttemptCapPureTests(unittest.TestCase):
    def test_head_advanced_needs_two_real_shas_that_differ(self):
        a, b = "a" * 40, "b" * 40
        self.assertTrue(bc._head_advanced(a, b))
        self.assertFalse(bc._head_advanced(a, a))
        self.assertFalse(bc._head_advanced(a[:9], a))          # abbreviation of same
        self.assertFalse(bc._head_advanced("", b))
        self.assertFalse(bc._head_advanced("abc12", b))       # too short
        self.assertFalse(bc._head_advanced("feat/pr-1", b))   # branch name, not a sha

    def test_rebase_gate_never_touches_behind(self):
        fa = {"acme-api#1": {"count": 9, "head": "a" * 40}}
        e = {"repo": "acme-api", "number": 1, "lane": "owner", "head_oid": "a" * 40}
        self.assertIsNone(bc.rebase_attempts_exhausted(dict(e, mss="BEHIND"), fa))
        self.assertIsNotNone(bc.rebase_attempts_exhausted(dict(e, mss="DIRTY"), fa))
        self.assertIsNone(bc.rebase_attempts_exhausted(dict(e, mss="DIRTY", lane="team"), fa))

    def test_load_fix_attempts_survives_malformed_records(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "progress.json")
            _w(p, {"fix_attempts": {"a#1": {"count": "x"}, "a#2": "junk",
                                    "a#3": {"count": 2, "head": 1234567}}})
            got = bc.load_fix_attempts(p)
            self.assertEqual(set(got), {"a#3"})
            self.assertEqual(got["a#3"]["head"], "")


class AttemptCapSweepTests(SweepCase):
    def _attempts(self, records):
        return {"fix_attempts": {
            f"acme-api#{n}": {"count": c, "last": "validation failed", "at": ago(30),
                              "head": h, "guard_exit": g}
            for n, (c, h, g) in records.items()}}

    def _actionable(self, f, num):
        f.add_pr("acme-api", num, commit_date=ago(10), inline=[cr_inline(num, MAJOR, ago(5))])

    def test_fix_capped_at_same_head_rearms_on_moved_head(self):
        cap = bc.FIX_ATTEMPT_CAP
        f = Fixtures(self.fx)
        for num in (860, 861, 862, 863, 864):
            self._actionable(f, num)
        f.finalize()
        progress = self._attempts({
            860: (cap, default_head(860), ""),        # capped at the live head
            861: (cap - 1, default_head(861), ""),    # budget left
            862: (cap, "f" * 40, ""),                 # head moved -> re-armed
            863: (cap, "feat/pr-863", ""),            # unusable head -> stays capped
        })
        out = run_sweep(self.fx, self.state, progress=progress)
        fixes = self.acts(out, "fix")
        self.assertNotIn(860, fixes)
        self.assertNotIn(863, fixes)
        for num in (861, 862, 864):
            self.assertIn(num, fixes)
        capped = {r["pr"]: r for r in out["attempt_capped"]}
        self.assertEqual(sorted(capped), [860, 863])
        row = capped[860]
        self.assertEqual((row["repo"], row["lane"], row["path"], row["attempts"], row["last"]),
                         ("acme-api", "owner", "fix", cap, "validation failed"))
        self.assertIn("blurb", row)
        self.assertIn("guard_exit", row)
        self.assertEqual(self.pr(out, 860)["head_oid"], default_head(860))

    def test_cap_override_env(self):
        f = Fixtures(self.fx)
        self._actionable(f, 865)
        f.finalize()
        progress = self._attempts({865: (2, default_head(865), "")})
        out = run_sweep(self.fx, self.state, progress=progress,
                        extra_env={"BABYSIT_FIX_ATTEMPT_CAP": "3"})
        self.assertIn(865, self.acts(out, "fix"))
        self.assertEqual(out["attempt_capped"], [])

    def test_rebase_gated_on_conflict_but_never_on_behind(self):
        cap = bc.FIX_ATTEMPT_CAP
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 870, mss="DIRTY")
        f.add_pr("acme-api", 871, mss="CONFLICTING")
        f.add_pr("acme-api", 872, mss="BEHIND")
        f.finalize()
        progress = self._attempts({
            870: (cap, default_head(870), "2"),
            871: (cap, "f" * 40, ""),                 # head moved -> re-armed
            872: (cap + 5, default_head(872), ""),    # BEHIND is never gated
        })
        out = run_sweep(self.fx, self.state, progress=progress)
        rebases = self.acts(out, "rebase")
        self.assertNotIn(870, rebases)
        self.assertIn(871, rebases)
        self.assertIn(872, rebases)
        capped = {r["pr"]: r for r in out["attempt_capped"]}
        self.assertEqual(list(capped), [870])
        self.assertEqual((capped[870]["path"], capped[870]["guard_exit"]), ("rebase", "2"))


# ===========================================================================
# every action names the owner that holds its PR
# ===========================================================================
class OwnerOnEveryActionTests(SweepCase):
    """The runbook's per-action `gh` calls (the OPEN re-check, the bump
    comment, ci_triage's `gh run list`, the retarget parent lookup) address
    `<owner>/<repo>`. Only the action can say what `<owner>` is, so every
    action carries it -- never a hand-edited org placeholder in the prose."""

    ORG = "acme-co"   # deliberately NOT OWNER_DEFAULT

    def test_every_action_and_pr_row_carries_its_owner(self):
        f = Fixtures(self.fx)
        o = self.ORG
        f.add_pr("acme-api", 1201, owner=o, commit_date=ago(10),          # fix
                 reviews=[cr_review("**Actionable comments posted: 1**", ago(5), rid=1)])
        f.add_pr("acme-api", 1202, owner=o, mss="BLOCKED",                 # bump
                 issues=[cr_issue(RATE_BODY, ago(400))])
        f.add_pr("acme-api", 1203, owner=o, mss="DIRTY",                   # rebase
                 issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))])
        f.add_pr("acme-api", 1204, owner=o, mss="UNSTABLE",                # ci_triage
                 issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))],
                 scr=[{"__typename": "CheckRun", "name": "pytest", "conclusion": "FAILURE"}])
        f.add_pr("acme-api", 1205, owner=o, base="feat/pr-1204",          # cli_launch
                 issues=[cr_issue(STACKED_BODY, ago(30))])
        f.add_pr("acme-api", 1206, owner=o, write_view=False)              # FETCH_FAIL
        f.finalize()
        out = run_sweep(self.fx, self.state, quiet="yes:test", owner=o)
        types = {a["type"] for a in out["actions"]}
        self.assertTrue({"fix", "bump", "rebase", "ci_triage", "cli_launch"} <= types, types)
        self.assertEqual({(a["type"], a.get("owner")) for a in out["actions"]},
                         {(t, o) for t in types})
        self.assertEqual({p["number"]: p.get("owner") for p in out["prs"]},
                         {n: o for n in range(1201, 1207)})

    def test_runbook_per_action_calls_never_name_a_placeholder_org(self):
        with open(os.path.join(REPO_ROOT, "commands", "babysit-prs.md")) as fh:
            doc = fh.read()
        self.assertNotIn("<your-org>/<repo>", doc)
        for call in ("gh -R <owner>/<repo> pr view <pr> --json state",
                     "gh -R <owner>/<repo> pr comment <pr> --body \"@coderabbitai review\"",
                     "gh run list -R <owner>/<repo> --branch"):
            self.assertIn(call, doc)
        # No per-PR `gh pr`/`gh run` command (one naming a `<pr>`/`<id>`/...
        # placeholder) may lean on the cwd's repo either.
        implicit = [m.group(0) for m in re.finditer(r"`gh (?:pr|run) [^`]*<[a-z_]+>[^`]*`", doc)
                    if "-R <owner>/<repo>" not in m.group(0)]
        self.assertEqual(implicit, [])


# ===========================================================================
# an empty `gh search` is cross-checked before it can read as DRAINED
# ===========================================================================
class EmptySearchCrossCheckTests(SweepCase):
    """An empty `[]` from `gh search prs` is trusted only once a non-search
    source (GraphQL viewer.pullRequests, the PR store) agrees. A GitHub search
    incident returns `[]` with exit 0 while the PRs are still open; accepted,
    that reads DRAINED, and two in a row stop a `/loop`."""

    def setUp(self):
        super().setUp()
        Fixtures(self.fx).finalize()        # search_open.json == []
        _w(self.state, {"pending_count": 3, "no_progress_streak": 0})

    def _viewer(self, nwos, cursor=None, after=None):
        name = "graphql_viewer_prs%s.json" % ("_" + after if after else "")
        _w(os.path.join(self.fx, name), viewer_page(nwos, cursor))

    def test_empty_search_while_the_pr_store_has_rows_is_not_drained(self):
        self._viewer([f"{OWNER}/acme-api"])
        out = run_sweep(self.fx, self.state)
        self.assertNotEqual(out["decision"], "DRAINED")
        self.assertEqual(out.get("error"), "search_fetch_fail")
        self.assertEqual(out["pending"], 3, "prior pending carried, not zeroed")

    def test_empty_search_confirmed_by_the_pr_store_is_drained(self):
        self._viewer([])
        out = run_sweep(self.fx, self.state)
        self.assertNotIn("error", out)
        self.assertEqual(out["decision"], "DRAINED")

    def test_a_failed_cross_check_is_unverified_not_drained(self):
        # no fixture -> the shim prints nothing -> gh_json gives None
        os.unlink(os.path.join(self.fx, "graphql_viewer_prs.json"))
        out = run_sweep(self.fx, self.state)
        self.assertEqual(out.get("error"), "search_fetch_fail")
        self.assertNotEqual(out["decision"], "DRAINED")

    def test_a_page_that_fails_to_load_cannot_prove_absence(self):
        self._viewer(["someone-else/repo"], cursor="c1")    # no page c1 fixture
        self.assertEqual(run_sweep(self.fx, self.state).get("error"), "search_fetch_fail")

    def test_later_pages_are_walked_so_many_prs_elsewhere_still_drain(self):
        """More than one page of open PRs in OTHER owners: the walk must reach
        the end, not stop at page one and call it unverified forever."""
        self._viewer(["other-org/a"] * 100, cursor="c1")
        self._viewer(["other-org/b"] * 100, cursor="c2", after="c1")
        self._viewer(["other-org/c"], after="c2")
        self.assertEqual(run_sweep(self.fx, self.state)["decision"], "DRAINED")

    def test_a_pr_on_a_later_page_blocks_the_drain(self):
        self._viewer(["other-org/a"] * 100, cursor="c1")
        self._viewer([f"{OWNER}/acme-api"], after="c1")
        self.assertEqual(run_sweep(self.fx, self.state).get("error"), "search_fetch_fail")

    def test_a_walk_past_the_page_cap_is_unverified(self):
        with mock.patch.object(bc, "gh_json",
                               return_value=viewer_page(["other-org/a"], "again")) as g:
            self.assertFalse(bc.empty_search_confirmed("gh", OWNER))
        self.assertEqual(g.call_count, bc.VIEWER_OPEN_PRS_MAX_PAGES)

    def test_prs_outside_the_swept_owner_do_not_block_a_drain(self):
        self._viewer(["other-org/tool"])
        self.assertEqual(run_sweep(self.fx, self.state)["decision"], "DRAINED")

    def test_repos_filter_scopes_the_cross_check(self):
        self._viewer([f"{OWNER}/busy"])
        self.assertEqual(run_sweep(self.fx, self.state, repos="quiet")["decision"],
                         "DRAINED")
        self.assertEqual(run_sweep(self.fx, self.state, repos="busy").get("error"),
                         "search_fetch_fail")

    def test_an_unreadable_pr_store_answer_is_unverified(self):
        """In-process: shapes the function cannot read never confirm a drain."""
        def viewer(nodes, more=False, cursor=None):
            return {"data": {"viewer": {"pullRequests": {
                "pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}}}}
        ok = {"repository": {"name": "x", "nameWithOwner": "other/x"}}
        for name, payload, owner in (
                ("non-dict node", viewer(["junk"]), OWNER),
                ("null node", viewer([None]), OWNER),
                ("non-dict repository", viewer([{"repository": "x"}]), OWNER),
                ("no pageInfo", {"data": {"viewer": {"pullRequests": {"nodes": [ok]}}}}, OWNER),
                ("non-bool hasNextPage", viewer([ok], more="no"), OWNER),
                ("more pages, no cursor", viewer([ok], more=True), OWNER),
                ("no owner", viewer([ok]), "")):
            with self.subTest(name), mock.patch.object(bc, "gh_json", return_value=payload):
                self.assertFalse(bc.empty_search_confirmed("gh", owner), name)
        with mock.patch.object(bc, "gh_json", return_value=viewer([ok])):
            self.assertTrue(bc.empty_search_confirmed("gh", OWNER), "control")

    def test_a_non_empty_search_never_spends_the_cross_check(self):
        f = Fixtures(self.fx)
        f.add_pr("acme-api", 1301, issues=[cr_issue(NO_ACTIONABLE_BODY, ago(20))])
        f.finalize()
        log = os.path.join(self.tmp, "gh.log")
        run_sweep(self.fx, self.state, extra_env={"BABYSIT_GH_LOG": log})
        with open(log) as fh:
            self.assertNotIn("viewer{pullRequests", fh.read())


# ===========================================================================
# a clean /PRlaunch attestation at the live head counts as CLI coverage
# ===========================================================================
CLEAN_ATTEST = "verdict=reviewed findings=0 seat=seat-1 run=run-1"
CR_CLI_OK = {"__typename": "StatusContext", "context": "review-gate/cr-cli", "state": "SUCCESS"}


class AttestationCoverageTests(SweepCase):
    """/PRlaunch publishes `review-gate/cr-cli` (PRLAUNCH_PUBLISH_CR_STATUS=1)
    after its own CR CLI run. Babysit read only its own store row and harvest
    comment, so it re-reviewed commits /PRlaunch had already reviewed clean,
    and the PR could not green via `cli` on that review."""

    def _stacked(self, num, *, rollup=(CR_CLI_OK,), statuses=None, head=None,
                 issues=None, **kw):
        """A cloud-rejected stacked PR (the cli_launch population) whose live
        head carries `statuses` (newest first) on the commit-status API."""
        head = head or default_head(num)
        f = self.f
        f.add_pr("acme-api", num, base="feat/parent-%d" % num, head_oid=head,
                 scr=list(rollup), issues=issues or [cr_issue(STACKED_BODY, ago(30))],
                 **kw)
        if statuses is not None:
            _w(os.path.join(self.fx, f"api_repos_{OWNER}_acme-api_commits_{head}_statuses.json"),
               statuses)

    @staticmethod
    def status(desc=CLEAN_ATTEST, state="success", login=AUTHOR,
               context="review-gate/cr-cli"):
        return {"context": context, "state": state, "description": desc,
                "creator": {"login": login}}

    def setUp(self):
        super().setUp()
        self.f = Fixtures(self.fx)

    def sweep(self, **kw):
        self.f.finalize()
        return run_sweep(self.fx, self.state, quiet="yes:test", **kw)

    def assert_not_attested(self, out, num):
        """Not attested, so still launchable (`cli_covers_head` is the
        planner's gate; CLI_CAP bounds how many launch in one sweep)."""
        pr = self.pr(out, num)
        self.assertFalse(pr["cli_attested_at_head"], pr)
        self.assertFalse(pr["cli_covers_head"], pr)
        self.assertNotEqual(pr["green_via"], "cli", pr)

    def test_clean_attestation_at_head_plans_no_launch_and_greens_via_cli(self):
        self._stacked(1401, statuses=[self.status()])
        out = self.sweep()
        pr = self.pr(out, 1401)
        self.assertTrue(pr["cli_attested_at_head"], pr)
        self.assertTrue(pr["cli_covers_head"], pr)
        self.assertNotIn(1401, self.acts(out, "cli_launch"))
        self.assertEqual((pr["tier"], pr["green_via"]), ("strict", "cli"))

    def test_attestation_without_a_seat_or_run_still_counts(self):
        self._stacked(1402, statuses=[self.status("verdict=reviewed findings=0 seat=n/a run=n/a")])
        self.assertTrue(self.pr(self.sweep(), 1402)["cli_attested_at_head"])

    def test_non_clean_descriptions_still_launch(self):
        descs = ("verdict=reviewed findings=2 seat=s run=r",
                 "verdict=reviewed findings=n/a seat=s run=r",
                 "verdict=skipped seat=s run=r findings=n/a reason=rate-limit",
                 "verdict=reviewed findings=0 seat=s run=r extra",
                 "")
        for i, d in enumerate(descs):
            self._stacked(1410 + i, statuses=[self.status(d)])
        out = self.sweep()
        for i in range(len(descs)):
            with self.subTest(desc=descs[i]):
                self.assert_not_attested(out, 1410 + i)

    def test_only_the_newest_status_for_the_context_counts(self):
        self._stacked(1420, statuses=[self.status("verdict=reviewed findings=1 seat=s run=r"),
                                      self.status()])
        self.assert_not_attested(self.sweep(), 1420)

    def test_a_status_from_an_untrusted_login_does_not_count(self):
        """Anyone with write access, or a workflow with `statuses: write`, can
        post this context: the creator must be a trusted login."""
        self._stacked(1421, statuses=[self.status(login="drive-by-user")])
        self._stacked(1422, statuses=[{"context": "review-gate/cr-cli", "state": "success",
                                       "description": CLEAN_ATTEST}])   # no creator
        out = self.sweep()
        self.assert_not_attested(out, 1421)
        self.assert_not_attested(out, 1422)

    def test_unset_ruling_authors_trusts_no_status(self):
        """With BABYSIT_RULING_AUTHORS unset, no creator is trusted -- not even
        the old placeholder login."""
        self._stacked(1426, statuses=[self.status()])
        self._stacked(1427, statuses=[self.status(login="your-github-login")])
        out = self.sweep(extra_env={"BABYSIT_RULING_AUTHORS": None})
        self.assert_not_attested(out, 1426)
        self.assert_not_attested(out, 1427)
        self.assertTrue(self.pr(run_sweep(self.fx, self.state, quiet="yes:test"),
                                1426)["cli_attested_at_head"])   # control: AUTHOR set

    def test_a_non_success_rollup_or_fetched_state_does_not_count(self):
        pending = dict(CR_CLI_OK, state="PENDING")
        self._stacked(1423, rollup=(pending,), statuses=[self.status()])
        self._stacked(1424, statuses=[self.status(state="pending")])
        out = self.sweep()
        self.assert_not_attested(out, 1423)
        self.assert_not_attested(out, 1424)

    def test_unreadable_statuses_fail_closed(self):
        self._stacked(1425)                     # rollup says success, no statuses fixture
        out = self.sweep()
        self.assert_not_attested(out, 1425)
        self.assertIn(1425, self.acts(out, "cli_launch"))

    def test_a_status_on_another_commit_does_not_count(self):
        """The status API is keyed by sha: an attestation on an older head is
        simply not in the answer for the live one."""
        self._stacked(1426)
        _w(os.path.join(self.fx, f"api_repos_{OWNER}_acme-api_commits_{'e' * 40}_statuses.json"),
           [self.status()])
        self.assert_not_attested(self.sweep(), 1426)

    def test_a_different_context_does_not_count(self):
        other = dict(CR_CLI_OK, context="review-gate")
        self._stacked(1427, rollup=(other,), statuses=[self.status(context="review-gate")])
        self.assert_not_attested(self.sweep(), 1427)

    def test_live_cloud_finding_vetoes(self):
        self.f.add_pr("acme-api", 1430, head_oid=default_head(1430), commit_date=ago(10),
                      scr=[CR_CLI_OK],
                      reviews=[cr_review("**Actionable comments posted: 1**", ago(5), rid=1)])
        _w(os.path.join(self.fx, f"api_repos_{OWNER}_acme-api_commits_{default_head(1430)}_statuses.json"),
           [self.status()])
        out = self.sweep()
        pr = self.pr(out, 1430)
        self.assertEqual(pr["state"], "HAS_ACTIONABLE")
        self.assertFalse(pr["cli_attested_at_head"])
        self.assertEqual(pr["tier"], "")

    def test_open_findings_harvested_at_this_head_veto(self):
        head = default_head(1431)
        self._stacked(1431, statuses=[self.status()], commit_date=ago(60),
                      issues=[cr_issue(STACKED_BODY, ago(300)),
                              harvest(ago(10), head, unapplied=1, major=1)])
        out = self.sweep()
        pr = self.pr(out, 1431)
        self.assertIsNotNone(pr["cli_findings_open"])
        self.assertFalse(pr["cli_attested_at_head"])
        self.assertEqual(pr["tier"], "")

    def test_a_dirty_harvest_at_head_vetoes_even_when_it_is_not_the_newest(self):
        """Two CLI runs finished out of order: the harvest naming the live head
        has an open major, a NEWER clean one names an older head."""
        head = default_head(1436)
        self._stacked(1436, statuses=[self.status()], commit_date=ago(60),
                      issues=[cr_issue(STACKED_BODY, ago(300)),
                              harvest(ago(20), head, unapplied=1, major=1),
                              clean_harvest(ago(10), "d" * 40)])
        pr = self.pr(self.sweep(), 1436)
        self.assertIsNone(pr["cli_findings_open"])
        self.assertFalse(pr["cli_attested_at_head"], pr)
        self.assertEqual(pr["tier"], "")

    def test_a_dirty_harvest_at_head_vetoes_when_it_predates_the_push(self):
        """The ageing clock retires the harvest (cli_open stays None), but it
        still names THIS head with findings, so it is not clean."""
        head = default_head(1437)
        self._stacked(1437, statuses=[self.status()], commit_date=ago(5),
                      issues=[cr_issue(STACKED_BODY, ago(300)),
                              harvest(ago(10), head, unapplied=1, critical=1)])
        pr = self.pr(self.sweep(), 1437)
        self.assertIsNone(pr["cli_findings_open"])
        self.assertFalse(pr["cli_attested_at_head"], pr)
        self.assertEqual(pr["tier"], "")

    def test_open_findings_from_a_harvest_naming_an_older_head_veto(self):
        self._stacked(1438, statuses=[self.status()], commit_date=ago(60),
                      issues=[cr_issue(STACKED_BODY, ago(300)),
                              harvest(ago(10), "d" * 40, unapplied=1, major=1)])
        pr = self.pr(self.sweep(), 1438)
        self.assertIsNotNone(pr["cli_findings_open"])
        self.assertFalse(pr["cli_attested_at_head"], pr)
        self.assertEqual(pr["tier"], "")

    def test_cloud_review_still_in_flight_vetoes(self):
        """CodeRabbit's own "reviewing now" placeholder: the cloud channel may
        still have more to say, so a clean CLI pass must not wave it through."""
        self.f.add_pr("acme-api", 1439, head_oid=default_head(1439), scr=[CR_CLI_OK],
                      issues=[cr_issue(TRIGGERED_BODY, ago(5))])
        _w(os.path.join(self.fx, f"api_repos_{OWNER}_acme-api_commits_{default_head(1439)}_statuses.json"),
           [self.status()])
        pr = self.pr(self.sweep(), 1439)
        self.assertEqual(pr["state"], "TRIGGERED_WAITING")
        self.assertFalse(pr["cli_attested_at_head"], pr)
        self.assertEqual(pr["tier"], "")

    def test_store_row_with_findings_at_this_head_vetoes(self):
        head = default_head(1432)
        self._stacked(1432, statuses=[self.status()])
        progress = {"cli_reviewed": {"acme-api#1432": {
            "head": head[:9], "sev": {"critical": 0, "major": 1, "minor": 0, "trivial": 0},
            "total": 1, "rounds": 1}}}
        self.f.finalize()
        out = run_sweep(self.fx, self.state, quiet="yes:test", progress=progress)
        self.assertFalse(self.pr(out, 1432)["cli_attested_at_head"])

    def test_red_ci_is_still_never_green(self):
        self._stacked(1433, statuses=[self.status()], mss="UNSTABLE",
                      rollup=(CR_CLI_OK, {"__typename": "CheckRun", "name": "pytest",
                                          "conclusion": "FAILURE"}))
        pr = self.pr(self.sweep(), 1433)
        self.assertTrue(pr["cli_attested_at_head"])
        self.assertEqual(pr["tier"], "red_ci")

    def test_no_context_in_the_rollup_spends_no_request(self):
        self._stacked(1434, rollup=())
        log = os.path.join(self.tmp, "gh.log")
        out = self.sweep(extra_env={"BABYSIT_GH_LOG": log})
        self.assert_not_attested(out, 1434)
        with open(log) as fh:
            self.assertNotIn("/statuses", fh.read())

    def test_fetch_fail_entry_carries_the_fields(self):
        self.f.add_pr("acme-api", 1435, write_view=False)
        pr = self.pr(self.sweep(), 1435)
        self.assertEqual((pr["cli_covers_head"], pr["cli_attested_at_head"]), (False, False))


if __name__ == "__main__":
    unittest.main()
