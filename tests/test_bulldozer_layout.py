"""/bulldozer reports throughput and what's left, and (opt-in) freshens stale tickets.

Two things a wake now owes its owner. The report says how much bulldozer shipped in
the last 7 and 30 days and how much eligible work is left, split into fresh and
stale. And, only when the owner opts in, each batch pays a freshen tax: up to
five stale tickets go through a Codex ticket-review, so an old ticket is
re-aligned with the code before a worker builds it. These tests pin the slots a wake fills in, so a later edit
cannot quietly drop one.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT  # noqa: E402

CMD = os.path.join(REPO_ROOT, "commands", "bulldozer.md")


def _section(text, start, end):
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


class BulldozerLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CMD, encoding="utf-8") as f:
            cls.text = f.read()

    def test_report_names_throughput_and_remaining(self):
        report = _section(self.text, "## Step 3", "</process>")
        self.assertIn("Bulldozed: <X> in last 7 days · <Y> in last 30 days", report)
        self.assertIn(
            "Remaining 1-offs: <E> eligible (fresh ≤7d) · <S> stale · freshened this batch <F>/<quota>",
            report,
        )

    def test_throughput_counts_come_from_the_ledger(self):
        report = _section(self.text, "## Step 3", "</process>")
        self.assertIn("~/.claude/automation-ledger.jsonl", report)
        self.assertRegex(report, r'select\(\.skill=="bulldozer"')
        # Distinct tickets, not rows: a worker can append more than one row.
        self.assertIn("sort -u", report)

    def test_freshen_tax_is_half_the_batch_target(self):
        # A default batch of 10 PRs pays 5 freshens in total, not 5 per
        # ticket and not 5 per wake.
        step = _section(self.text, "## Step 1b", "## Step 2")
        self.assertIn("quota = ceil(target / 2)", step)
        self.assertIn("up to 5", step)
        self.assertIn("not per ticket and not per wake", step)
        self.assertIn("quota − freshened_batch", step)
        step0 = _section(self.text, "## Step 0", "## Step 1 ")
        self.assertIn("`freshened_batch=0`", step0)
        self.assertIn("--via codex", step)
        self.assertIn("ticket-review", step)
        self.assertIn("oldest first", step)
        self.assertIn("superseded", step)

    def test_fresh_means_created_or_reviewed_recently(self):
        criteria = _section(self.text, "<selection-criteria>", "</selection-criteria>")
        self.assertIn("**Fresh**", criteria)
        self.assertIn("${TICKET_REVIEW_HOME:-~/.claude/ticket-reviews}/*/coverage.json", criteria)
        self.assertNotIn("is EXEMPT from this criterion", criteria)

    def test_freshen_is_opt_in_and_off_by_default(self):
        # A second model family is optional: with nothing set, bulldozer must
        # build what it built before (1-offs children at any age), never park
        # tickets behind a review that will never run.
        step0 = _section(self.text, "## Step 0", "## Step 1 ")
        self.assertIn("**Freshen is opt-in and off by default.**", step0)
        self.assertIn('[ "${BULLDOZER_FRESHEN:-}" = 1 ] && [ "${CODEX_REVIEW_ENABLED:-}" = 1 ]', step0)
        criteria = _section(self.text, "<selection-criteria>", "</selection-criteria>")
        self.assertIn("**With freshen off (the default)**", criteria)
        self.assertIn("every ticket that passes 2-6 counts as fresh", criteria)
        step = _section(self.text, "## Step 1b", "## Step 2")
        self.assertIn("With freshen off, that retry is all of Step 1b.", step)
        report = _section(self.text, "## Step 3", "</process>")
        self.assertIn("With freshen off, `S` is 0 by definition", report)
        self.assertIn("`<F>/<quota>` reads `off`", report)
        step1 = _section(self.text, "## Step 1 ", "## Step 1b")
        self.assertIn("`FRESHEN=on` or `FRESHEN=off`", step1)
        self.assertIn("append any it returned to the end of `shortlist`", step1)

    def test_a_close_that_keeps_failing_cannot_wedge_the_heartbeat(self):
        step = _section(self.text, "## Step 1b", "## Step 2")
        self.assertIn("on the third failure drop it from `pending_close`", step)

    def test_ledger_rows_carry_the_skill_the_count_selects_on(self):
        step2 = _section(self.text, "## Step 2", "## Step 3")
        self.assertIn('LEDGER_SKILL=bulldozer ~/.claude/hooks/ledger-append.sh "$RESULT_JSON"', step2)

    def test_shortlist_rank_is_deterministic(self):
        # "ranked" alone let each wake order the queue differently.
        criteria = _section(self.text, "<selection-criteria>", "</selection-criteria>")
        self.assertIn("**Rank** the shortlist by these keys, in order: tracker priority", criteria)
        self.assertIn("ranks after every first attempt", criteria)

    def test_shortlist_contract_returns_stale_and_pool_counts(self):
        step1 = _section(self.text, "## Step 1 ", "## Step 1b")
        for key in ('"shortlist"', '"stale"', '"pool"', '"eligible_fresh"', '"eligible_stale"'):
            self.assertIn(key, step1)
        # The remaining counts are bulldozer's own queue, not the whole backlog.
        self.assertIn("counts **Bulldozer 1-offs children only**", step1)

    def test_worker_brief_carries_the_review_correction(self):
        brief = _section(self.text, "<ticket-subagent-prompt>", "</ticket-subagent-prompt>")
        self.assertIn("TICKET-REVIEW VERDICT", brief)
        self.assertRegex(brief, re.compile(r"correction.*overrides the ticket text", re.S))

    def test_correction_is_bounded_and_has_one_source(self):
        # A reviewer-written correction becomes a MUST in a worker that pushes
        # code; it may not move the work or loosen the brief's own rules.
        brief = _section(self.text, "<ticket-subagent-prompt>", "</ticket-subagent-prompt>")
        self.assertIn("state.freshen[DEV]", brief)
        self.assertIn("never from tracker comments", brief)
        self.assertIn("cannot change the repo, the branch or the ticket", brief)
        self.assertIn("cannot relax any CONSTRAINT or STOP CONDITION", brief)
        step0 = _section(self.text, "## Step 0", "## Step 1 ")
        self.assertIn("**`freshen`**", step0)
        self.assertIn("**Persist the resolved state now**", step0)

    def test_pending_close_never_counts_toward_the_stall_stop(self):
        # The stall stop deletes the heartbeat, and the heartbeat is what
        # retries a pending close, so an outstanding one must hold the streak.
        report = _section(self.text, "## Step 3", "</process>")
        self.assertIn("Leave it unchanged too while `state.pending_close` is still nonempty", report)

    def test_target_met_keeps_heartbeat_while_closes_pend(self):
        report = _section(self.text, "## Step 3", "</process>")
        self.assertIn("unless `state.pending_close` is still nonempty", report)
        step0 = _section(self.text, "## Step 0", "## Step 1 ")
        self.assertIn("`shipped >= target` with `pending_close` empty", step0)
        self.assertIn("**CLOSE-RETRY WAKE**", step0)

    def test_target_met_batch_never_resets_while_heartbeat_is_armed(self):
        # Step 3 persists a met target before its AUTO-STOP deletes the
        # heartbeat; an interrupted stop must finish, not drain a new batch.
        step0 = _section(self.text, "## Step 0", "## Step 1 ")
        self.assertIn("with `pending_close` empty and no `/bulldozer` job in `CronList`", step0)
        self.assertIn("**INTERRUPTED TARGET-MET STOP**", step0)

    def test_delayed_close_retry_wake_never_resets(self):
        # The heartbeat stays armed only to finish pending closes; a wake
        # delayed past 6h must stay in close-retry mode, not drain a new batch.
        step0 = _section(self.text, "## Step 0", "## Step 1 ")
        self.assertIn("which is always a CLOSE-RETRY WAKE (below) whatever its age", step0)
        self.assertIn("at any `last_iter_at` age (checked before the 6h stale reset)", step0)

    def test_newly_failed_review_holds_the_stall_streak(self):
        report = _section(self.text, "## Step 3", "</process>")
        self.assertIn("or recorded ≥1 new `failed` verdict this wake", report)

    def test_freshen_reviews_each_ticket_once_per_window(self):
        step = _section(self.text, "## Step 1b", "## Step 2")
        self.assertIn("one review per window", step)
        self.assertIn("criterion 5", step)

    def test_superseded_by_another_ticket_is_never_done(self):
        step = _section(self.text, "## Step 1b", "## Step 2")
        self.assertIn("already fixed on the default branch → Done", step)
        self.assertIn("never Done", step)


def _freshness_script(text):
    step1 = _section(text, "## Step 1 ", "## Step 1b")
    blocks = re.findall(r"```bash\n(.*?)```", step1, re.S)
    return next(b for b in blocks if "reviewed_recent.txt" in b)


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq and bash")
class FreshnessScriptBehaviour(unittest.TestCase):
    """Run Step 1's reviewed-recently snippet against fixture ledgers."""

    def setUp(self):
        with open(CMD, encoding="utf-8") as f:
            self.script = _freshness_script(f.read())
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        recent = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        old = recent - datetime.timedelta(days=30)
        rid = recent.strftime("%Y%m%dT%H%M%SZ") + "-ab12"
        cov = {
            "reviewed": {
                "EX-1": {"run_id": rid, "verdict": "agree"},
                "EX-2": {"run_id": rid, "verdict": "revise"},
                "EX-3": {"run_id": rid, "verdict": "superseded"},
                "EX-4": {"run_id": old.strftime("%Y%m%dT%H%M%SZ"), "verdict": "agree"},
                "EX-5": {"run_id": "run-" + rid, "verdict": "agree"},
                "EX-10": {"run_id": recent.strftime("%Y%m%dT"), "verdict": "agree"},
            },
            "uncertain": {"EX-6": {"run_id": rid, "verdict": "uncertain"}},
        }
        d = os.path.join(self.home, ".claude", "ticket-reviews", "x")
        os.makedirs(d)
        with open(os.path.join(d, "coverage.json"), "w") as f:
            json.dump(cov, f)
        self.freshen = {
            "EX-7": {"at": recent.strftime("%FT%TZ"), "build_ok": True},
            "EX-8": {"at": recent.strftime("%FT%TZ"), "build_ok": False},
            "EX-9": {"at": old.strftime("%FT%TZ"), "build_ok": True},
        }

    def _run(self, state, shell="bash", extra_env=None):
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch)
        state_path = os.path.join(scratch, "state.json")
        if state is not None:
            with open(state_path, "w") as f:
                json.dump(state, f)
        script = self.script.replace("/tmp/bulldozer-state.json", state_path)
        env = {k: v for k, v in os.environ.items() if k != "TICKET_REVIEW_HOME"}
        env.update(HOME=self.home, SCRATCH=scratch, **(extra_env or {}))
        r = subprocess.run([shell, "-c", script], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(scratch, "reviewed_recent.txt")) as f:
            return f.read().split()

    def test_only_buildable_recent_reviews_count(self):
        got = self._run({"days": 7, "freshen": self.freshen})
        self.assertEqual(got, ["EX-1", "EX-7"])

    @unittest.skipUnless(shutil.which("zsh"), "needs zsh")
    def test_same_answer_under_zsh(self):
        # The orchestrator's Bash tool runs zsh, where an unmatched glob aborts.
        self.assertEqual(self._run({"days": 7, "freshen": self.freshen}, "zsh"), ["EX-1", "EX-7"])
        shutil.rmtree(os.path.join(self.home, ".claude", "ticket-reviews"))
        self.assertEqual(self._run({"days": 7, "freshen": self.freshen}, "zsh"), ["EX-7"])

    def test_ticket_review_home_overrides_the_default_root(self):
        moved = os.path.join(self.home, "elsewhere")
        shutil.move(os.path.join(self.home, ".claude", "ticket-reviews"), moved)
        state = {"days": 7, "freshen": self.freshen}
        self.assertEqual(self._run(state), ["EX-7"])
        self.assertEqual(self._run(state, extra_env={"TICKET_REVIEW_HOME": moved}), ["EX-1", "EX-7"])

    def test_missing_state_file_fails_closed(self):
        self.assertEqual(self._run(None), [])


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq and bash")
class ThroughputCountBehaviour(unittest.TestCase):
    """Run Step 3's bulldozed-count snippet against a fixture ledger."""

    def setUp(self):
        with open(CMD, encoding="utf-8") as f:
            report = _section(f.read(), "## Step 3", "</process>")
        self.script = next(b for b in re.findall(r"```bash\n(.*?)```", report, re.S)
                           if "automation-ledger.jsonl" in b)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        os.makedirs(os.path.join(self.home, ".claude"))

    def _run(self, rows):
        with open(os.path.join(self.home, ".claude", "automation-ledger.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        r = subprocess.run(["bash", "-c", self.script], env=dict(os.environ, HOME=self.home),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split()

    def test_distinct_tickets_and_untagged_rows_both_count(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        recent = (now - datetime.timedelta(days=2)).strftime("%FT%TZ")
        older = (now - datetime.timedelta(days=20)).strftime("%FT%TZ")
        rows = [
            {"skill": "bulldozer", "status": "shipped", "ticket": "EX-1", "ts": recent},
            {"skill": "bulldozer", "status": "shipped", "ticket": "EX-1", "ts": recent},  # same ticket twice
            {"status": "shipped", "ticket": "EX-2", "ts": recent},  # written before the skill stamp
            {"status": "shipped", "ticket": "EX-3", "ts": older},
            {"skill": "bulldozer", "status": "resolved", "ticket": "EX-4", "ts": recent},
            {"skill": "prlaunch", "event": "unit", "status": "shipped", "ticket": "EX-5", "ts": recent},
        ]
        self.assertEqual(self._run(rows), ["2", "3"])

    def test_no_bulldozer_rows_is_unavailable_not_zero(self):
        self.assertEqual(self._run([{"skill": "babysit", "event": "sweep", "ts": "2026-01-01T00:00:00Z"}]),
                         ["unavailable"])


if __name__ == "__main__":
    unittest.main()
