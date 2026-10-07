"""Tests for skills/done-without-evidence/done_without_evidence.py.

The RECURRING "Done is not evidence" sweep: it flags tickets closed (state
type "completed") with none of the three evidence signals Linear actually
carries -- startedAt, a GitHub PR attachment, a GitHub commit attachment --
and not whitelisted via the `no-code-expected` label.

The classification/report-rendering functions are PURE (no network) and are
tested directly against fixture issue dicts shaped like real Linear GraphQL
responses (Attachment.url carries the github.com/.../pull/N or /commit/<sha>
pattern; Issue.startedAt is null until the issue enters a started-type
state). The live Linear calls (gql, fetch, ensure_label,
find_or_create_standing_ticket, post_comment) are exercised only through a
stubbed `gql` -- no test ever reaches the network -- proving the CLI
orchestration (report-only: dry-run touches nothing; live run creates the
label/ticket lazily and posts exactly one comment) without live credentials.
"""
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(os.path.dirname(HERE), "skills", "done-without-evidence")
MODULE_PATH = os.path.join(SKILL_DIR, "done_without_evidence.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("done_without_evidence_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dwe = _load_module()


# ---------------------------------------------------------------------------
# Fixture issue builders — shaped like real Linear GraphQL `issues` nodes
# ---------------------------------------------------------------------------

def make_issue(identifier="ENG-1", title="Some ticket", url="https://linear.app/x/issue/ENG-1",
                state_type="completed", state_name="Done", started_at=None,
                attachments=None, labels=None):
    return {
        "id": "uuid-" + identifier,
        "identifier": identifier,
        "title": title,
        "url": url,
        "startedAt": started_at,
        "state": {"name": state_name, "type": state_type},
        "labels": {"nodes": [{"name": n} for n in (labels or [])]},
        "attachments": {"nodes": [{"url": u} for u in (attachments or [])]},
    }


class EvidenceSignalTests(unittest.TestCase):
    """Each of the three evidence signals, checked in isolation."""

    def test_pr_attachment_is_evidence(self):
        iss = make_issue(attachments=["https://github.com/example-org/api/pull/3537"])
        self.assertTrue(dwe.has_pr_evidence(iss))
        self.assertFalse(dwe.has_commit_evidence(iss))

    def test_commit_attachment_is_evidence(self):
        iss = make_issue(attachments=[
            "https://github.com/example-org/web/commit/67ea55348ccea79cdf4a9f1f5d4f12f0bc35c846"
        ])
        self.assertTrue(dwe.has_commit_evidence(iss))
        self.assertFalse(dwe.has_pr_evidence(iss))

    def test_unrelated_attachment_is_not_evidence(self):
        # e.g. a Figma/Slack/Sentry link — not a PR or commit.
        iss = make_issue(attachments=["https://www.figma.com/file/abc123/Design"])
        self.assertFalse(dwe.has_pr_evidence(iss))
        self.assertFalse(dwe.has_commit_evidence(iss))

    def test_no_attachments_is_no_evidence(self):
        iss = make_issue(attachments=[])
        self.assertFalse(dwe.has_pr_evidence(iss))
        self.assertFalse(dwe.has_commit_evidence(iss))

    def test_started_at_present_means_started(self):
        self.assertTrue(dwe.has_started(make_issue(started_at="2026-07-01T00:00:00.000Z")))
        self.assertFalse(dwe.has_started(make_issue(started_at=None)))

    def test_whitelist_label_case_insensitive(self):
        self.assertTrue(dwe.is_whitelisted(make_issue(labels=["No-Code-Expected"])))
        self.assertTrue(dwe.is_whitelisted(make_issue(labels=["no-code-expected"])))
        self.assertFalse(dwe.is_whitelisted(make_issue(labels=["Bug Fix"])))
        self.assertFalse(dwe.is_whitelisted(make_issue(labels=[])))

    def test_only_completed_state_type_is_closed(self):
        self.assertTrue(dwe.is_closed(make_issue(state_type="completed")))
        self.assertFalse(dwe.is_closed(make_issue(state_type="canceled")))
        self.assertFalse(dwe.is_closed(make_issue(state_type="started")))
        self.assertFalse(dwe.is_closed(make_issue(state_type="backlog")))


class ClassifyIssueTests(unittest.TestCase):
    """The actual sweep predicate: closed AND no started AND no PR/commit AND not whitelisted."""

    def test_flags_the_exact_violation_shape(self):
        iss = make_issue(identifier="ENG-9001", title="Bulk-closed with nothing behind it",
                          state_type="completed", started_at=None, attachments=[], labels=[])
        v = dwe.classify_issue(iss)
        self.assertIsNotNone(v)
        self.assertEqual(v["identifier"], "ENG-9001")
        self.assertEqual(v["title"], "Bulk-closed with nothing behind it")

    def test_not_flagged_if_started(self):
        iss = make_issue(state_type="completed", started_at="2026-07-01T00:00:00.000Z",
                          attachments=[], labels=[])
        self.assertIsNone(dwe.classify_issue(iss))

    def test_not_flagged_if_pr_attached_even_without_started_at(self):
        # Real-world case observed live: an issue can carry a PR attachment
        # while startedAt is still null (state moved backlog->done directly).
        # PR evidence alone must still clear it.
        iss = make_issue(state_type="completed", started_at=None,
                          attachments=["https://github.com/org/repo/pull/8"], labels=[])
        self.assertIsNone(dwe.classify_issue(iss))

    def test_not_flagged_if_commit_attached(self):
        iss = make_issue(state_type="completed", started_at=None,
                          attachments=["https://github.com/org/repo/commit/" + "a" * 40], labels=[])
        self.assertIsNone(dwe.classify_issue(iss))

    def test_not_flagged_if_whitelisted(self):
        iss = make_issue(state_type="completed", started_at=None, attachments=[],
                          labels=["no-code-expected"])
        self.assertIsNone(dwe.classify_issue(iss))

    def test_not_flagged_if_not_closed(self):
        iss = make_issue(state_type="started", started_at=None, attachments=[], labels=[])
        self.assertIsNone(dwe.classify_issue(iss))

    def test_canceled_ticket_is_not_flagged(self):
        # Canceling a ticket doesn't claim work was done — only completed
        # ("Done") tickets carry the "done without evidence" claim.
        iss = make_issue(state_type="canceled", started_at=None, attachments=[], labels=[])
        self.assertIsNone(dwe.classify_issue(iss))


class SweepIssuesTests(unittest.TestCase):
    def test_filters_a_mixed_batch_to_only_violations(self):
        issues = [
            make_issue(identifier="ENG-1", state_type="completed", started_at=None, attachments=[]),
            make_issue(identifier="ENG-2", state_type="completed",
                       started_at="2026-01-01T00:00:00.000Z"),
            make_issue(identifier="ENG-3", state_type="completed", started_at=None,
                       attachments=["https://github.com/o/r/pull/1"]),
            make_issue(identifier="ENG-4", state_type="completed", started_at=None,
                       attachments=[], labels=["no-code-expected"]),
            make_issue(identifier="ENG-5", state_type="started", started_at=None, attachments=[]),
            make_issue(identifier="ENG-6", state_type="completed", started_at=None, attachments=[]),
        ]
        out = dwe.sweep_issues(issues)
        self.assertEqual([v["identifier"] for v in out], ["ENG-1", "ENG-6"])

    def test_empty_input_yields_empty_output(self):
        self.assertEqual(dwe.sweep_issues([]), [])


class RenderReportTests(unittest.TestCase):
    def test_clean_sweep_message(self):
        import datetime
        now = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.timezone.utc)
        out = dwe.render_report([], "Eng", now)
        self.assertIn("clean", out.lower())
        self.assertNotIn("| ENG-", out)

    def test_violations_render_as_a_table_row_per_ticket(self):
        import datetime
        now = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.timezone.utc)
        violations = [
            {"identifier": "ENG-42", "title": "Some | pipe | title", "url": "https://linear.app/x/ENG-42",
             "state": "Done"},
        ]
        out = dwe.render_report(violations, "Eng", now)
        self.assertIn("ENG-42", out)
        self.assertIn("https://linear.app/x/ENG-42", out)
        # pipe characters in the title must not break the markdown table
        self.assertIn("Some \\| pipe \\| title", out)
        self.assertIn("1", out)  # count of violations appears somewhere

    def test_embedded_newline_in_title_does_not_break_the_table_row(self):
        import datetime
        now = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.timezone.utc)
        violations = [
            {"identifier": "ENG-43", "title": "Multi\nline\r\ntitle", "url": "https://linear.app/x/ENG-43",
             "state": "Done"},
        ]
        out = dwe.render_report(violations, "Eng", now)
        table_line = next(line for line in out.splitlines() if line.startswith("| [ENG-43]"))
        self.assertNotIn("\n", table_line)
        self.assertNotIn("\r", table_line)
        self.assertIn("Multi line  title", table_line)


class StateCacheTests(unittest.TestCase):
    """state.json round-trip — filesystem only, no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dwe-state-")
        self.state_path = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_missing_state_is_empty_dict(self):
        self.assertEqual(dwe.load_state(self.state_path), {})

    def test_save_then_load_round_trips(self):
        dwe.save_state(self.state_path, {"issue_id": "abc-123", "identifier": "ENG-9999"})
        self.assertEqual(dwe.load_state(self.state_path),
                          {"issue_id": "abc-123", "identifier": "ENG-9999"})

    def test_corrupt_state_file_is_treated_as_empty(self):
        with open(self.state_path, "w") as f:
            f.write("not json{{{")
        self.assertEqual(dwe.load_state(self.state_path), {})


class GqlStub:
    """Records calls and returns canned responses keyed by a substring of the
    query text — stands in for done_without_evidence.gql so tests never touch
    the network."""

    def __init__(self, responses):
        self.responses = responses  # list of (query_substring, callable(vars) -> data)
        self.calls = []

    def __call__(self, query, variables=None):
        self.calls.append((query, variables or {}))
        for needle, fn in self.responses:
            if needle in query:
                return fn(variables or {})
        raise AssertionError("GqlStub: no canned response for query containing: %r" % query[:80])


class EnsureLabelTests(unittest.TestCase):
    def test_existing_label_is_reused_not_recreated(self):
        stub = GqlStub([
            ("issueLabels", lambda v: {"issueLabels": {"nodes": [{"id": "label-1", "name": "no-code-expected"}]}}),
        ])
        lid, created = dwe.ensure_label("Eng", gql_fn=stub)
        self.assertEqual(lid, "label-1")
        self.assertFalse(created)
        # must not have attempted a team lookup / create mutation at all
        self.assertTrue(all("issueLabelCreate" not in q for q, _ in stub.calls))

    def test_lookup_is_case_insensitive_and_includes_workspace_labels(self):
        # Linear rejects a team label whose name collides (case-insensitively)
        # with an existing team OR workspace label — the lookup must see both.
        self.assertIn("eqIgnoreCase", dwe.LABEL_LOOKUP_QUERY)
        self.assertIn("{team: {null: true}}", dwe.LABEL_LOOKUP_QUERY)

    def test_missing_label_is_created_lazily(self):
        calls = {"create": 0}

        def fake_lookup(v):
            return {"issueLabels": {"nodes": []}}

        def fake_team(v):
            return {"teams": {"nodes": [{"id": "team-uuid"}]}}

        def fake_create(v):
            calls["create"] += 1
            self.assertEqual(v["input"]["teamId"], "team-uuid")
            self.assertEqual(v["input"]["name"], "no-code-expected")
            return {"issueLabelCreate": {"success": True, "issueLabel": {"id": "new-label", "name": "no-code-expected"}}}

        stub = GqlStub([
            ("issueLabelCreate", fake_create),
            ("issueLabels", fake_lookup),
            ("teams", fake_team),
        ])
        lid, created = dwe.ensure_label("Eng", gql_fn=stub)
        self.assertEqual(lid, "new-label")
        self.assertTrue(created)
        self.assertEqual(calls["create"], 1)


class StandingTicketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dwe-standing-")
        self.state_path = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cached_state_skips_search_entirely(self):
        dwe.save_state(self.state_path, {"team": "Eng", "issue_id": "cached-id", "identifier": "ENG-1",
                                          "url": "https://linear.app/x/ENG-1"})
        stub = GqlStub([])  # any call at all is a failure — cache must short-circuit
        iid, ident, url = dwe.find_or_create_standing_ticket(
            "Eng", gql_fn=stub, state_path=self.state_path)
        self.assertEqual(iid, "cached-id")
        self.assertEqual(stub.calls, [])

    def test_cache_for_another_team_is_a_miss(self):
        dwe.save_state(self.state_path, {"team": "Ops", "issue_id": "ops-ticket",
                                          "identifier": "OPS-1", "url": "https://linear.app/x/OPS-1"})
        stub = GqlStub([
            ("issues", lambda v: {"issues": {"nodes": [
                {"id": "eng-ticket", "identifier": "ENG-5", "url": "https://linear.app/x/ENG-5"}]}}),
        ])
        iid, _, _ = dwe.find_or_create_standing_ticket("Eng", gql_fn=stub, state_path=self.state_path)
        self.assertEqual(iid, "eng-ticket")
        self.assertEqual(stub.calls[0][1]["team"], "Eng")
        self.assertEqual(dwe.load_state(self.state_path)["team"], "Eng")

    def test_found_by_search_is_cached_not_recreated(self):
        stub = GqlStub([
            ("issueCreate", lambda v: (_ for _ in ()).throw(AssertionError("must not create — one was found"))),
            ("issues", lambda v: {"issues": {"nodes": [
                {"id": "found-id", "identifier": "ENG-77", "url": "https://linear.app/x/ENG-77"}]}}),
        ])
        iid, ident, url = dwe.find_or_create_standing_ticket(
            "Eng", gql_fn=stub, state_path=self.state_path)
        self.assertEqual(iid, "found-id")
        self.assertEqual(dwe.load_state(self.state_path)["issue_id"], "found-id")

    def test_created_when_none_exists_carries_project_and_priority(self):
        def fake_search(v):
            return {"issues": {"nodes": []}}

        def fake_team(v):
            return {"teams": {"nodes": [{"id": "team-uuid",
                                         "projects": {"nodes": [{"id": "proj-uuid", "name": "Platform"}]}}]}}

        def fake_create(v):
            inp = v["input"]
            self.assertEqual(inp["teamId"], "team-uuid")
            self.assertIn("projectId", inp)
            self.assertEqual(inp["projectId"], "proj-uuid")
            self.assertIn("priority", inp)
            self.assertIn(inp["priority"], (1, 2, 3, 4))
            return {"issueCreate": {"success": True, "issue": {
                "id": "brand-new-id", "identifier": "ENG-8888", "url": "https://linear.app/x/ENG-8888"}}}

        stub = GqlStub([
            ("issueCreate", fake_create),
            ("issues", fake_search),
            ("teams", fake_team),
        ])
        iid, ident, url = dwe.find_or_create_standing_ticket(
            "Eng", gql_fn=stub, state_path=self.state_path,
            project_name="Platform")
        self.assertEqual(iid, "brand-new-id")
        self.assertEqual(dwe.load_state(self.state_path)["issue_id"], "brand-new-id")

    def test_create_without_project_dies_before_any_write(self):
        # No default project is baked in: when the standing ticket has to be
        # created and --project wasn't given, fail loudly and file nothing.
        stub = GqlStub([
            ("issueCreate", lambda v: (_ for _ in ()).throw(AssertionError("must not create"))),
            ("issues", lambda v: {"issues": {"nodes": []}}),
        ])
        with self.assertRaises(SystemExit):
            dwe.find_or_create_standing_ticket("Eng", gql_fn=stub, state_path=self.state_path)
        self.assertEqual(dwe.load_state(self.state_path), {})


class PostCommentTests(unittest.TestCase):
    def test_posts_body_verbatim_and_returns_comment(self):
        def fake_comment(v):
            self.assertEqual(v["input"]["issueId"], "issue-1")
            self.assertEqual(v["input"]["body"], "## report\nhello")
            return {"commentCreate": {"success": True, "comment": {"id": "c-1", "url": "https://linear.app/x/c-1"}}}

        stub = GqlStub([("commentCreate", fake_comment)])
        c = dwe.post_comment("issue-1", "## report\nhello", gql_fn=stub)
        self.assertEqual(c["id"], "c-1")


class CliDryRunTests(unittest.TestCase):
    """dry-run must NEVER touch Linear — no label lookup/create, no standing
    ticket search/create, no comment post. Proves the report-only contract
    for the one CLI path that can never mutate anything."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dwe-cli-")
        self.state_path = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_only_fetches_and_prints_never_writes(self):
        issues = [make_issue(identifier="ENG-1", state_type="completed", started_at=None, attachments=[])]

        def fake_issues(v):
            return {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": issues}}

        stub = GqlStub([("issues", fake_issues)])
        out = dwe.run_sweep(team="Eng", dry_run=True, gql_fn=stub, state_path=self.state_path,
                            limit=None)
        self.assertIn("ENG-1", out["report"])
        self.assertTrue(out["dry_run"])
        self.assertIsNone(out.get("comment_url"))
        # only the issues-fetch call happened — no label/ticket/comment mutation calls
        for q, _ in stub.calls:
            self.assertNotIn("issueLabelCreate", q)
            self.assertNotIn("issueCreate", q)
            self.assertNotIn("commentCreate", q)

    def test_live_run_posts_exactly_one_comment_and_creates_label_once(self):
        issues = [make_issue(identifier="ENG-1", state_type="completed", started_at=None, attachments=[])]

        def fake_issues(v):
            return {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": issues}}

        def fake_label_lookup(v):
            return {"issueLabels": {"nodes": [{"id": "label-1", "name": "no-code-expected"}]}}

        def fake_ticket_search(v):
            return {"issues": {"nodes": [{"id": "standing-1", "identifier": "ENG-100",
                                          "url": "https://linear.app/x/ENG-100"}]}}

        comment_calls = {"n": 0}

        def fake_comment(v):
            comment_calls["n"] += 1
            self.assertEqual(v["input"]["issueId"], "standing-1")
            self.assertIn("ENG-1", v["input"]["body"])
            return {"commentCreate": {"success": True, "comment": {"id": "c-1", "url": "https://linear.app/x/c-1"}}}

        stub = GqlStub([
            ("commentCreate", fake_comment),
            ("issueLabels", fake_label_lookup),
            ("issues", fake_ticket_search),  # NOTE: also matches fetch — see ordering below
        ])
        # fetch closed issues needs its own distinguishable response; give it priority
        # by checking the fetch query text explicitly.
        stub.responses.insert(0, ("state { name type }", fake_issues))

        out = dwe.run_sweep(team="Eng", dry_run=False, gql_fn=stub, state_path=self.state_path,
                            limit=None)
        self.assertEqual(comment_calls["n"], 1)
        self.assertEqual(out["comment_url"], "https://linear.app/x/c-1")
        self.assertEqual(dwe.load_state(self.state_path)["issue_id"], "standing-1")


class TruncationTests(unittest.TestCase):
    """A sweep that stops with issues remaining must say so — a partial scan
    must never render as a "clean" verdict."""

    @staticmethod
    def _paged_stub(pages):
        state = {"i": 0}

        def fake_issues(v):
            i = state["i"]
            state["i"] += 1
            return {"issues": {"pageInfo": {"hasNextPage": i + 1 < pages, "endCursor": str(i)},
                               "nodes": [make_issue(identifier="ENG-%d" % i, started_at="x")]}}
        return GqlStub([("issues", fake_issues)])

    def test_backstop_hit_is_reported(self):
        issues, truncated = dwe.fetch_closed_issues("Eng", gql_fn=self._paged_stub(dwe.MAX_PAGES + 5))
        self.assertEqual(len(issues), dwe.MAX_PAGES)
        self.assertTrue(truncated)
        out = dwe.run_sweep(team="Eng", dry_run=True, gql_fn=self._paged_stub(dwe.MAX_PAGES + 5))
        self.assertIn("Partial sweep", out["report"])

    def test_limit_with_more_remaining_is_reported(self):
        issues, truncated = dwe.fetch_closed_issues("Eng", limit=2, gql_fn=self._paged_stub(5))
        self.assertEqual(len(issues), 2)
        self.assertTrue(truncated)

    def test_complete_fetch_is_not_truncated(self):
        issues, truncated = dwe.fetch_closed_issues("Eng", gql_fn=self._paged_stub(3))
        self.assertEqual(len(issues), 3)
        self.assertFalse(truncated)
        out = dwe.run_sweep(team="Eng", dry_run=True, gql_fn=self._paged_stub(3))
        self.assertNotIn("Partial sweep", out["report"])
        self.assertIn("clean", out["report"])


if __name__ == "__main__":
    unittest.main()
