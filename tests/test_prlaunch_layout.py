"""PRlaunch keeps its rules inline and its history in playbooks/prlaunch.

commands/PRlaunch.md carries the deep-review core inline, so it is trimmed to
stay under the one-plain-read budget by moving incident history and
measurements into playbooks/prlaunch/rationale.md. These tests pin the split:
each rule an agent must act on stays in the command, and every rationale
section the command cites exists.
"""
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRLAUNCH = os.path.join(REPO_ROOT, "commands", "PRlaunch.md")
RATIONALE = os.path.join(REPO_ROOT, "playbooks", "prlaunch", "rationale.md")
CITE_RE = re.compile(r"`~/\.claude/playbooks/prlaunch/rationale\.md` § ([A-Za-z][\w -]*?)(?=[).,;]|$)", re.M)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class RulesStayInlineTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(PRLAUNCH)

    def test_cr_exit_codes_are_all_in_the_command(self):
        for rule in ("**75 — limit-blown skip (authorized):**",
                     "**70 — the wrapper failed internally**",
                     "**71 — the CodeRabbit CLI rejected our arguments**",
                     "never infer the gate from stdout",
                     "never pipe it through `| tail -N`",
                     "RETRY it once",
                     "Never wait out long timers, and never retry more than once (71: not at all)."):
            self.assertIn(rule, self.text)

    def test_seat_pool_stays_optional(self):
        self.assertIn("**Seat pool (optional, off by default):**", self.text)
        self.assertIn("with no seats registered, none of this runs", self.text)

    def test_filing_buckets_point_at_the_taxonomy_instead_of_restating_it(self):
        self.assertIn("`## Work Taxonomy` of `~/.claude/CLAUDE.md`", self.text)
        self.assertNotIn("Big idea surfaced mid-session", self.text)

    def test_guardrails_point_at_the_right_steps(self):
        guard = self.text[self.text.index("## Guardrails"):]
        self.assertIn("(phase 5 step 4)", guard)
        self.assertIn("**Never merge.**", guard)
        self.assertNotIn("phase 5 step 3", self.text)
        self.assertNotIn("§7 report", self.text)


class HistoryIsInThePlaybookTest(unittest.TestCase):
    def test_command_names_the_playbook_by_its_installed_path(self):
        self.assertIn("~/.claude/playbooks/prlaunch/rationale.md", _read(PRLAUNCH))

    def test_every_cited_section_exists(self):
        cites = set(CITE_RE.findall(_read(PRLAUNCH)))
        self.assertGreaterEqual(cites, {"CR CLI", "Outcome eval", "publish-cr-cli ordering",
                                        "Ops steps as tickets", "One brain"})
        headings = set(re.findall(r"^## (.+?)\s*$", _read(RATIONALE), re.M))
        self.assertEqual(sorted(cites - headings), [])

    def test_incidents_moved_with_their_rules(self):
        ref = _read(RATIONALE)
        for fact in ("Exit 71", "picsum", "HTTP 422", "full-screen dialog", "storage bucket"):
            self.assertIn(fact, ref)


if __name__ == "__main__":
    unittest.main()
