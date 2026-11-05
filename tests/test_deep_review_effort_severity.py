"""Doc contract for deep-review's effort-weighted severity + noise suppression (v7.7).

deep-review is prose an agent executes, so its rules only hold if
their wording survives future edits. These assertions pin the three things
v7.7 added and that an editor is most likely to "tidy" away:

  * Step 4's severity decision table carries an EFFORT axis for CRITICAL and
    HIGH, and a heavy-lift finding is merge-blocking or waived with a reason
    plus a filed ticket -- never silently deferred.
  * Step 8's publish rule names the three CodeRabbit-noise classes: model-id
    currency, lint/typing, shared-client convention.
  * A version-history line records the derivation AND the counterintuitive
    direction (severity is inversely correlated with getting fixed), so nobody
    "fixes" the policy back to escalate-by-severity.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT  # noqa: E402

REFS = os.path.join(REPO_ROOT, "playbooks", "deep-review")


def _read(name):
    with open(os.path.join(REFS, name), encoding="utf-8") as fh:
        return fh.read()


def _section(body, start, end):
    """Text from the `## <start>` heading up to the next `## <end>` heading."""
    m = re.search(r"^## %s.*?(?=^## %s)" % (re.escape(start), re.escape(end)),
                  body, re.S | re.M)
    if not m:
        raise AssertionError("no '## %s' section ending at '## %s'"
                             % (start, end))
    return m.group(0)


def _table_rows(text):
    return [ln for ln in text.splitlines() if ln.lstrip().startswith("|")]


class EffortWeightedSeverity(unittest.TestCase):
    def setUp(self):
        self.step4 = _read("consolidation-detail.md")

    def _rows(self, severity, effort):
        return [r for r in _table_rows(self.step4)
                if re.search(r"\*?\*?%s\*?\*?" % severity, r)
                and effort in r.lower()]

    def test_critical_and_high_rows_carry_both_effort_levels(self):
        for sev in ("CRITICAL", "HIGH"):
            for effort in ("quick-win", "heavy-lift"):
                self.assertTrue(self._rows(sev, effort),
                                "Step 4 table has no %s x %s row" % (sev, effort))

    def test_heavy_lift_is_blocking_or_waived_with_ticket(self):
        for sev in ("CRITICAL", "HIGH"):
            row = " ".join(self._rows(sev, "heavy-lift")).lower()
            self.assertIn("merge-blocking", row, "%s heavy-lift row" % sev)
            self.assertIn("waived", row, "%s heavy-lift row" % sev)
            self.assertIn("reason", row, "%s heavy-lift row" % sev)
            self.assertIn("ticket", row, "%s heavy-lift row" % sev)

    def test_effort_changes_disposition_never_severity(self):
        self.assertRegex(self.step4.lower(),
                         r"effort changes (the )?disposition, never (the )?severity")


class NoiseSuppressionClasses(unittest.TestCase):
    def setUp(self):
        self.step8 = _section(_read("publish-and-fix.md"), "8. Publish", "9. ").lower()

    def test_three_classes_named(self):
        for cls in ("model-id currency", "lint / typing", "shared-client convention"):
            self.assertIn(cls, self.step8, "Step 8 must name the %r class" % cls)

    def test_model_id_rule_suppresses_memorized_catalog_not_all_model_ids(self):
        # The rule is "don't flag against the reviewer's memorized catalog",
        # NOT "never flag model ids": contradicting the repo's own catalog is
        # still a finding.
        self.assertIn("memorized", self.step8)
        self.assertRegex(self.step8, r"contradicts the repo's (own )?authoritative model catalog")
        self.assertIn("still a valid finding", self.step8)

    def test_heavy_lift_disposition_is_published(self):
        self.assertIn("heavy-lift", self.step8)
        self.assertIn("waived", self.step8)


class ChangelogRecordsDirection(unittest.TestCase):
    def test_history_line_records_derivation_and_counterintuitive_direction(self):
        history = _read("principles-and-history.md").split("## Version history", 1)[1]
        lines = [ln for ln in history.splitlines() if "effort-weighted severity" in ln]
        self.assertEqual(len(lines), 1, "exactly one version line for effort-weighted severity")
        line = lines[0].lower()
        self.assertRegex(line, r"^\*\*v\d+\.\d+ → v7\.7:\*\*")
        self.assertIn("coderabbit review threads", line)   # derivation source
        self.assertIn("inversely", line)           # the observed direction
        self.assertIn("counterintuitive", line)
        self.assertRegex(line, r"do not .?fix.? it back")


if __name__ == "__main__":
    unittest.main()
