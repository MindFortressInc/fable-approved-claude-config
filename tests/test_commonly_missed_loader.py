"""deep-review's optional commonly-missed lens loader (hooks/commonly-missed.py).

Step 3 of deep-review (the core, inline in PRlaunch section 1) runs
`commonly-missed.py prompt` and pastes its stdout into every agent prompt. The
loader is off until COMMONLY_MISSED_SOURCE names a lens file (a local path or
an https:// URL). These tests pin that the unset path does nothing at all, that
every invariant and class in the lens file reaches the assembled prompt, and
the fallback order for a URL source: fetch, else cached copy, else nothing.
"""
import http.client
import importlib.util
import io
import os
import re
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT, HookSandbox, _GIT_ENV  # noqa: E402

LOADER = os.path.join(REPO_ROOT, "hooks", "commonly-missed.py")
DEEP_REVIEW_CORE = os.path.join(REPO_ROOT, "commands", "PRlaunch.md")
CACHE_REL = os.path.join("review", "commonly-missed.md")
URL = "https://lenses.example.test/review/commonly-missed.md"

INVARIANT_RE = re.compile(r"^### .+$", re.M)
CLASS_RE = re.compile(r"^\d+\. \*\*[^*]+\*\*", re.M)

FIXTURE = """# Commonly missed

Preamble the loader must not paste.

## Generic Review Invariants

### 1. Fail closed

Body one.

### 2. A sibling-added invariant

Body two.

## Commonly Missed

Intro line.

1. **Tenancy / scope binding** first class.
2. **Input edges.** second class.
18. **Sibling-appended class.** added by a later ticket.

## Trailing section

Must not be pasted.
"""
TRAILING_H1 = FIXTURE.replace("## Trailing section\n\nMust not be pasted.\n", "") + "# Appendix\n\nPrivate notes.\n"


def _markers(content):
    return INVARIANT_RE.findall(content) + CLASS_RE.findall(content)


def _info_lines(err):
    return [line for line in err.splitlines() if line.startswith("INFO: commonly-missed:")]


def _load_module():
    spec = importlib.util.spec_from_file_location("commonly_missed", LOADER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp(io.BytesIO):
    def __init__(self, body, url=URL):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DeepReviewRunsTheLoaderTest(unittest.TestCase):
    def setUp(self):
        with open(DEEP_REVIEW_CORE, encoding="utf-8") as fh:
            text = fh.read()
        self.step3 = text[text.index("### 3. Static Analysis"):text.index("### 3.5 Adversarial")]

    def test_step_3_runs_the_loader(self):
        self.assertIn("#### Commonly missed (mandatory, every repo)", self.step3)
        self.assertIn("python3 ~/.claude/hooks/commonly-missed.py prompt", self.step3)

    def test_step_3_says_to_paste_both_sections(self):
        self.assertIn("`## Generic Review Invariants`", self.step3)
        self.assertIn("`## Commonly Missed`", self.step3)

    def test_step_3_says_it_is_off_until_configured(self):
        self.assertIn("COMMONLY_MISSED_SOURCE", self.step3)
        self.assertIn("Off by default", self.step3)

    def test_the_only_source_is_the_configured_one(self):
        # No built-in clone path, repo slug or URL: unset must mean off.
        with open(LOADER, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotRegex(src, r"https://[\w.-]+/|gh api|repos/|git\b.*\bshow\b")
        self.assertEqual(re.findall(r"os\.environ\.get\(([^)]*)\)", src), ["ENV"])

    def test_the_cached_copy_is_gitignored(self):
        proc = subprocess.run(["git", "-C", REPO_ROOT, "check-ignore", "-q", "review/commonly-missed.md"],
                              env=dict(os.environ, **_GIT_ENV))
        self.assertEqual(proc.returncode, 0, "review/commonly-missed.md must stay untracked")


class SubprocessTestBase(unittest.TestCase):
    def setUp(self):
        self.sb = HookSandbox()
        self.cache = os.path.join(self.sb.claude, CACHE_REL)

    def tearDown(self):
        self.sb.close()

    def run_loader(self, cmd, source=None):
        env = self.sb.env()
        env.pop("COMMONLY_MISSED_SOURCE", None)  # ambient config must not leak in
        if source is not None:
            env["COMMONLY_MISSED_SOURCE"] = source
        proc = subprocess.run(["python3", LOADER, cmd], capture_output=True, text=True, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    def write(self, name, text):
        path = os.path.join(self.sb.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path


class OffByDefaultTest(SubprocessTestBase):
    def test_unset_prompt_prints_nothing_on_either_stream(self):
        self.assertEqual(self.run_loader("prompt"), (0, "", ""))

    def test_empty_value_is_off_too(self):
        self.assertEqual(self.run_loader("prompt", source="  "), (0, "", ""))

    def test_unset_sync_exits_0_and_writes_nothing(self):
        self.assertEqual(self.run_loader("sync"), (0, "", ""))
        self.assertFalse(os.path.exists(os.path.join(self.sb.claude, "review")))

    def test_unknown_command_is_a_usage_error(self):
        rc, _, err = self.run_loader("bogus")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)


class LocalFileSourceTest(SubprocessTestBase):
    def test_every_invariant_and_class_reaches_the_prompt(self):
        rc, out, err = self.run_loader("prompt", source=self.write("lens.md", FIXTURE))
        self.assertEqual(rc, 0, err)
        markers = _markers(FIXTURE)
        self.assertEqual(len(markers), 5)
        for marker in markers:
            self.assertIn(marker, out)
        self.assertIn("Check the diff against each Generic Review Invariant", out)
        self.assertNotIn("Preamble the loader must not paste", out)
        self.assertNotIn("Must not be pasted", out)
        self.assertEqual(err, "")

    def test_a_top_level_heading_ends_the_classes_section(self):
        rc, out, err = self.run_loader("prompt", source=self.write("lens.md", TRAILING_H1))
        self.assertEqual(rc, 0, err)
        self.assertIn("18. **Sibling-appended class.**", out)
        self.assertNotIn("Private notes", out)

    def test_a_local_file_is_read_in_place_never_cached(self):
        rc, out, _ = self.run_loader("sync", source=self.write("lens.md", FIXTURE))
        self.assertEqual(rc, 0)
        self.assertIn("lens.md", out)
        self.assertFalse(os.path.exists(self.cache))

    def test_tilde_is_expanded(self):
        with open(os.path.join(self.sb.home, "lens.md"), "w", encoding="utf-8") as fh:
            fh.write(FIXTURE)
        rc, out, err = self.run_loader("prompt", source="~/lens.md")
        self.assertEqual(rc, 0, err)
        self.assertIn("18. **Sibling-appended class.**", out)

    def test_info_lines_show_the_home_directory_as_tilde(self):
        _, _, err = self.run_loader("prompt", source="~/nope.md")
        self.assertIn("cannot read ~/nope.md", err)
        self.assertNotIn(self.sb.home, err)

    def test_a_missing_file_is_one_info_line_and_an_empty_prompt(self):
        rc, out, err = self.run_loader("prompt", source=os.path.join(self.sb.dir, "nope.md"))
        self.assertEqual((rc, out), (0, ""))
        info = _info_lines(err)
        self.assertEqual(len(info), 1, err)
        self.assertIn("built-in lenses", info[0])
        rc, _, _ = self.run_loader("sync", source=os.path.join(self.sb.dir, "nope.md"))
        self.assertEqual(rc, 1)

    def test_a_file_without_the_classes_section_is_rejected(self):
        broken = FIXTURE.replace("## Commonly Missed\n", "## Renamed\n")
        rc, out, err = self.run_loader("prompt", source=self.write("lens.md", broken))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("no '## Commonly Missed' section", err)
        self.assertEqual(len(_info_lines(err)), 1, err)

    def test_a_heading_with_trailing_whitespace_is_accepted(self):
        padded = FIXTURE.replace("## Commonly Missed\n", "## Commonly Missed  \n")
        rc, out, err = self.run_loader("prompt", source=self.write("lens.md", padded))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("18. **Sibling-appended class.**", out)

    def test_plain_http_is_refused(self):
        rc, out, err = self.run_loader("prompt", source="http://lenses.example.test/x.md")
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("only https:// URLs", err)


class UrlSourceTest(unittest.TestCase):
    """In-process, with the opener patched: no network, no real HOME."""

    def setUp(self):
        self.sb = HookSandbox()
        self.cache = os.path.join(self.sb.claude, CACHE_REL)
        self.mod = _load_module()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "COMMONLY_MISSED_SOURCE": URL})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.sb.close()

    def run_main(self, cmd, opener):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.mod, "_open", opener), \
                redirect_stdout(out), redirect_stderr(err):
            rc = self.mod.main(["commonly-missed.py", cmd])
        return rc, out.getvalue(), err.getvalue()

    def seed_cache(self, text=FIXTURE, source=URL):
        os.makedirs(os.path.dirname(self.cache))
        with open(self.cache, "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(self.cache + ".source", "w", encoding="utf-8") as fh:
            fh.write(self.mod._source_key(source) + "\n")

    @staticmethod
    def serves(text, final_url=URL):
        return lambda url: _Resp(text.encode("utf-8"), final_url)

    @staticmethod
    def fails(exc):
        def _open(url):
            raise exc
        return _open

    def test_a_fetch_is_cached_and_pasted(self):
        rc, out, err = self.run_main("prompt", self.serves(FIXTURE))
        self.assertEqual(rc, 0, err)
        for marker in _markers(FIXTURE):
            self.assertIn(marker, out)
        with open(self.cache, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), FIXTURE)

    def test_the_source_line_drops_the_query_string(self):
        os.environ["COMMONLY_MISSED_SOURCE"] = URL + "?token=s3cret#frag"
        rc, out, err = self.run_main("prompt", self.serves(FIXTURE))
        self.assertEqual(rc, 0, err)
        self.assertIn("Source: `%s`." % URL, out)
        self.assertNotIn("s3cret", out + err)

    def test_a_failed_fetch_falls_back_to_the_cached_copy(self):
        self.seed_cache()
        err_404 = urllib.error.HTTPError(URL, 404, "Not Found", {}, None)
        rc, out, err = self.run_main("prompt", self.fails(err_404))
        self.assertEqual(rc, 0)
        self.assertIn("18. **Sibling-appended class.**", out)
        self.assertIn("HTTP 404", err)
        self.assertIn("using the copy cached", err)
        self.assertEqual(len(_info_lines(err)), 1, err)
        rc, _, _ = self.run_main("sync", self.fails(err_404))
        self.assertEqual(rc, 1)

    def test_fetched_content_without_a_section_never_overwrites_the_cache(self):
        self.seed_cache()
        rc, out, err = self.run_main("prompt", self.serves("<html>rate limited</html>"))
        self.assertEqual(rc, 0)
        self.assertIn("or '## Commonly Missed' section", err)
        with open(self.cache, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), FIXTURE)
        self.assertIn("### 1. Fail closed", out)

    def test_no_fetch_and_no_cache_is_one_info_line_and_an_empty_prompt(self):
        rc, out, err = self.run_main("prompt", self.fails(urllib.error.URLError("dns")))
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(len(_info_lines(err)), 1, err)
        self.assertIn("built-in lenses", err)
        self.assertFalse(os.path.exists(self.cache))

    def test_failure_reasons_never_quote_the_upstream_error(self):
        rc, _, err = self.run_main("prompt", self.fails(urllib.error.URLError("203.0.113.7 refused")))
        self.assertEqual(rc, 0)
        self.assertNotIn("203.0.113.7", err)

    def test_an_unwritable_cache_still_serves_the_fetched_lenses(self):
        with open(os.path.join(self.sb.claude, "review"), "w") as fh:
            fh.write("a file where the review/ directory should be")
        rc, out, err = self.run_main("prompt", self.serves(FIXTURE))
        self.assertEqual(rc, 0, err)
        self.assertIn("18. **Sibling-appended class.**", out)
        self.assertEqual(len(_info_lines(err)), 1, err)
        self.assertIn("not cached", err)

    def test_a_redirect_off_https_is_refused(self):
        handler = self.mod._HttpsOnlyRedirects()
        req = urllib.request.Request(URL)
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example.test/x.md")
        self.assertIsNotNone(handler.redirect_request(req, None, 302, "Found", {}, URL + "?v=2"))

    def test_a_response_that_ended_off_https_is_not_used(self):
        rc, out, err = self.run_main("prompt", self.serves(FIXTURE, final_url="http://evil.example.test/x.md"))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("left https", err)
        self.assertFalse(os.path.exists(self.cache))

    def test_a_url_with_credentials_is_refused_without_echoing_them(self):
        os.environ["COMMONLY_MISSED_SOURCE"] = "https://alice:s3cret@lenses.example.test/x.md"
        for cmd in ("prompt", "sync"):
            rc, out, err = self.run_main(cmd, self.serves(FIXTURE))
            self.assertEqual(out, "")
            self.assertIn("credentials", err)
            self.assertNotIn("s3cret", err)
        self.assertEqual(rc, 1)

    def test_an_http_protocol_error_still_falls_back_to_the_cache(self):
        self.seed_cache()
        rc, out, err = self.run_main("prompt", self.fails(http.client.IncompleteRead(b"partial")))
        self.assertEqual(rc, 0)
        self.assertIn("IncompleteRead", err)
        self.assertIn("using the copy cached", err)
        self.assertIn("### 1. Fail closed", out)

    def test_a_copy_cached_from_another_url_is_never_used(self):
        self.seed_cache(source="https://other.example.test/lens.md")
        rc, out, err = self.run_main("prompt", self.fails(urllib.error.URLError("dns")))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("built-in lenses", err)

    def test_a_url_differing_only_by_query_has_its_own_cache(self):
        self.seed_cache()
        os.environ["COMMONLY_MISSED_SOURCE"] = URL + "?ref=team-b"
        rc, out, err = self.run_main("prompt", self.fails(urllib.error.URLError("dns")))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("built-in lenses", err)

    def test_the_cache_key_never_stores_the_query_string(self):
        os.environ["COMMONLY_MISSED_SOURCE"] = URL + "?token=s3cret"
        self.run_main("sync", self.serves(FIXTURE))
        with open(self.cache + ".source", encoding="utf-8") as fh:
            self.assertNotIn("s3cret", fh.read())

    def test_the_cached_source_line_names_the_url(self):
        self.seed_cache()
        rc, out, _ = self.run_main("prompt", self.fails(urllib.error.URLError("dns")))
        self.assertIn("Source: `cached copy of %s from " % URL, out)

    def test_a_slow_fetch_hits_the_overall_deadline(self):
        clock = iter([0, 0, self.mod.TIMEOUT + 1])
        with mock.patch.object(self.mod.time, "monotonic", lambda: next(clock)):
            rc, out, err = self.run_main("prompt", self.serves(FIXTURE))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("timed out", err)

    def test_an_unexpected_error_never_blocks_the_review(self):
        rc, out, err = self.run_main("prompt", self.fails(RuntimeError("boom")))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("loader failed (RuntimeError)", err)


if __name__ == "__main__":
    unittest.main()
