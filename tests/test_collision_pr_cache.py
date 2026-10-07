"""Probe E integration: real script/cache/git, only GitHub is stubbed."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "hooks/collision-check.sh"


class ProbeCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "bin").mkdir()
        self.calls = self.root / "calls"
        self.rows = self.root / "rows"
        self.rows.write_text("11\tfirst task\tme/eng7988-one\n12\tsecond task\tme/eng-7989-two\n")
        gh = self.root / "bin/gh"
        gh.write_text(r"""#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
if sys.argv[1:3] == ['auth', 'token']:
    print(os.environ['GH_TOKEN']); sys.exit(0)
with open(os.environ['CC_TEST_CALLS'], 'a') as f:
    f.write(' '.join(sys.argv[1:]) + '\n')
time.sleep(float(os.environ.get('CC_TEST_DELAY', '0')))
if os.environ.get('CC_TEST_FAIL'):
    print('HTTP 404 Not Found', file=sys.stderr); sys.exit(1)
if os.environ.get('CC_TEST_REST') and sys.argv[1] == 'pr':
    print('secondary rate limit', file=sys.stderr); sys.exit(1)
print(Path(os.environ['CC_TEST_ROWS']).read_text(), end='')
""")
        gh.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                        GH_TOKEN="fixture-token", CC_TEST_CALLS=str(self.calls),
                        CC_TEST_ROWS=str(self.rows), COLLISION_CHECK_SKIP_LINEAR="1",
                        COLLISION_CHECK_SKIP_REMOTE="1", COLLISION_CHECK_SELF_DIR=str(self.root),
                        COLLISION_CHECK_ROOTS=str(self.root / "clones"),
                        COLLISION_CHECK_PR_CACHE_DIR=str(self.root / "cache"),
                        COLLISION_CHECK_OWN_ORGS="example-org", LINEAR_BRANCH_PREFIX="eng")
        for name in ("LINEAR_API_KEY", "LINEAR_KEY_FILE", "LINEAR_DEV_TEAM_ID",
                     "COLLISION_CHECK_SKIP_PRS", "COLLISION_CHECK_RETIRED_REMOTES"):
            self.env.pop(name, None)

    def worker(self, num=7988, **env):
        return subprocess.run([str(SCRIPT), "--_pr", "example-org/service"],
                              env=dict(self.env, CC_NUM=str(num),
                                       CC_TOKEN_RE=f"[Ee][Nn][Gg]-?{num}([^0-9]|$)", **env),
                              capture_output=True, text=True, timeout=30)

    def count(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def cache_files(self):
        files = list((self.root / "cache").glob("*.json"))
        self.assertTrue(files, "successful fetch must publish a shared cache")
        return files

    def test_reuse_across_tickets_keeps_hyphenless_and_branch_matches(self):
        a, b = self.worker(), self.worker(7989)
        self.assertEqual((a.returncode, b.returncode), (0, 0), a.stderr + b.stderr)
        self.assertIn("eng7988-one", a.stdout)
        self.assertIn("eng-7989-two", b.stdout)
        self.assertNotIn("eng7988-one", b.stdout)
        self.assertEqual(self.count(), 1, "different tickets must share the unfiltered list")

    def test_concurrent_builders_share_one_refresh(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.worker(CC_TEST_DELAY="0.3"), range(4)))
        self.assertTrue(all(r.returncode == 0 and "eng7988" in r.stdout for r in results))
        self.assertEqual(self.count(), 1, "cold concurrent callers must coalesce")

    def test_expired_data_refreshes_and_cannot_hide_new_collision(self):
        self.rows.write_text("")
        self.assertEqual(self.worker().returncode, 0)
        for path in self.cache_files():
            data = json.loads(path.read_text())
            data["fetched_at"] = time.time() - 61
            path.write_text(json.dumps(data))
        self.rows.write_text("99\tnew task\tme/eng7988-new\n")
        self.assertIn("eng7988-new", self.worker().stdout)
        self.assertEqual(self.count(), 2)

    def test_corrupt_cache_is_refetched(self):
        self.worker()
        for path in self.cache_files():
            path.write_text("{broken")
        self.assertIn("eng7988", self.worker().stdout)
        self.assertEqual(self.count(), 2)

    def test_credentials_separate_cached_lists(self):
        self.worker()
        denied = self.worker(GH_TOKEN="different-token", CC_TEST_FAIL="1")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("::cc-fail", denied.stdout)
        self.assertEqual(self.count(), 2)

    def test_failed_fetch_is_never_cached(self):
        failed = self.worker(CC_TEST_FAIL="1")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("eng7988", self.worker().stdout)
        self.assertEqual(self.count(), 2)

    def test_expired_cache_is_not_used_when_refresh_fails(self):
        self.worker()
        for path in self.cache_files():
            data = json.loads(path.read_text())
            data["fetched_at"] = time.time() - 61
            path.write_text(json.dumps(data))
        result = self.worker(CC_TEST_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("eng7988", result.stdout)

    def test_incomplete_rest_list_matches_but_is_never_reused_as_clear(self):
        self.rows.write_text("1\ttask\tme/eng7988-one\n" * 100)
        found = self.worker(CC_TEST_REST="1")
        missing = self.worker(7989, CC_TEST_REST="1")
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn("eng7988", found.stdout)
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(self.count(), 8, "both calls must fetch all capped pages afresh")

    def test_full_graphql_limit_never_reports_clear_or_caches(self):
        self.rows.write_text("1\tother task\tme/other\n" * 1000)
        self.assertNotEqual(self.worker().returncode, 0)
        self.rows.write_text("1\ttask\tme/eng7988-new\n")
        self.assertIn("eng7988-new", self.worker().stdout)
        self.assertEqual(self.count(), 2)

    def test_unusable_cache_falls_back_to_live_query(self):
        (self.root / "cache").write_text("not a directory")
        result = self.worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("eng7988", result.stdout)
        self.assertEqual(self.count(), 1)

    def clone(self, name, slug):
        repo = self.root / "clones" / name
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        f"https://github.com/{slug}.git"], check=True)

    def test_unset_own_orgs_degrades_without_querying_github(self):
        # No org-specific default: an unscoped probe E did not run, so the
        # verdict is UNKNOWN (exit 3), never a clear.
        self.clone("owned", "example-org/service")
        env = dict(self.env)
        env.pop("COLLISION_CHECK_OWN_ORGS")
        result = subprocess.run([str(SCRIPT), "ENG-7988"], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("COLLISION_CHECK_OWN_ORGS", result.stdout)
        self.assertNotIn("CLEAR", result.stdout)
        self.assertEqual(self.count(), 0)

    def test_scope_skips_third_party_without_hiding_owned_collision(self):
        self.clone("owned", "example-org/service")
        self.clone("external", "thirdparty/service")
        result = subprocess.run([str(SCRIPT), "ENG-7988"], env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("eng7988-one", result.stdout)
        self.assertIn("thirdparty/service", result.stdout)
        self.assertIn("not an own-org remote", result.stdout)
        self.assertEqual(self.count(), 1)

    def test_owner_override_is_case_insensitive(self):
        self.clone("custom", "CustomOrg/service")
        result = subprocess.run([str(SCRIPT), "ENG-7988"],
                                env=dict(self.env, COLLISION_CHECK_OWN_ORGS="customorg"),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("eng7988-one", result.stdout)
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
