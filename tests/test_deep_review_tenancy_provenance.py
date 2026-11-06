"""Step 3's tenancy pass must ask where the scope value came from.

The v7.0 tenancy pass only asked that each scope column be bound, which a
whole class of authz escapes satisfied by binding it to a value the caller
supplied. These tests pin that the pass carries the provenance step.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT  # noqa: E402

REVIEWER_PROMPTS = os.path.join(REPO_ROOT, "playbooks", "deep-review", "reviewer-prompts.md")
HEADING = "**Tenancy / scope-binding pass (v7.0):**"


def _tenancy_pass():
    with open(REVIEWER_PROMPTS, encoding="utf-8") as fh:
        text = fh.read()
    start = text.index(HEADING)
    end = text.index("\n**", start + len(HEADING))
    # Join the blockquote's wrapped lines so a phrase can span a line break.
    return " ".join(text[start:end].replace("\n>", "\n").split())


class TenancyPassProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.block = _tenancy_pass()

    def test_pass_still_binds_every_scope_column(self):
        self.assertIn("quote the line in this query that binds it", self.block)

    def test_pass_classifies_each_value_into_the_four_sources(self):
        for source in (
            "verified principal",
            "server lookup keyed by a verified value",
            "caller-supplied",
            "unsigned claim",
        ):
            self.assertIn(source, self.block)

    def test_untrusted_source_is_high_unless_a_quoted_line_makes_it_safe(self):
        self.assertIn("finding at HIGH or above", self.block)
        self.assertIn("unless you quote the line that makes it safe", self.block)
        self.assertIn("a check of the value against the principal", self.block)
        self.assertIn("a binding of the same query or action to a scope value from a trusted source", self.block)

    def test_scope_headers_need_a_quoted_middleware_binding(self):
        for header in ("X-Org-Id", "X-Tenant", "X-Host-*"):
            self.assertIn(header, self.block)
        self.assertIn("middleware line that binds it to the authenticated principal", self.block)

    def test_writable_non_http_sources_count_as_caller_supplied(self):
        self.assertIn("a file, env var, queue message or row that a less trusted party can write", self.block)

    def test_every_new_path_to_a_guarded_action_quotes_its_check(self):
        self.assertIn("for each path the diff adds to a guarded action, quote the check that runs before it", self.block)


if __name__ == "__main__":
    unittest.main()
