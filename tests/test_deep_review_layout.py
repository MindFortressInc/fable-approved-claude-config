"""deep-review's core is inline in PRlaunch, the rest is in references.

commands/deep-review.md reached 100,887 bytes at v7.7, past what one plain Read
loads, and PRlaunch only named its path, so most PRlaunch sessions never opened
it. The core that runs on every review now sits between markers in PRlaunch
section 1, and everything else moved unchanged to playbooks/deep-review/.
These tests pin that nothing fell out of reach.
"""
import collections
import glob
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT  # noqa: E402

PRLAUNCH = os.path.join(REPO_ROOT, "commands", "PRlaunch.md")
ROUTER = os.path.join(REPO_ROOT, "commands", "deep-review.md")
REF_DIR = os.path.join(REPO_ROOT, "playbooks", "deep-review")
START = "<!-- deep-review-core:start -->"
END = "<!-- deep-review-core:end -->"
# About 15KB. This is a ratchet: raise it only by moving a rule that must run
# on every review, never to avoid reading a reference.
CORE_MAX_BYTES = 15000
ROUTER_MAX_BYTES = 6000
# A plain Read stops at 25,000 tokens. A PRlaunch of 67,479 bytes measured
# 25,811 tokens (0.3825 per byte), which cut off phase 6 and Guardrails.
PRLAUNCH_MAX_BYTES = 65200
DETECTORS = 12
PATH_RE = re.compile(r"playbooks/deep-review/([A-Za-z0-9_-]+\.md)")
BARE_NAME_RE = re.compile(r"`([a-z][a-z-]+\.md)`")
RELATIVE_HOOK_RE = re.compile(r"(?<![\w~/.-])(?:\./)?hooks/[\w.-]+\.(?:sh|py)")
# Rules moved out of the old file, pinned where an agent now finds them.
MOVED_RULES = {
    "reviewer-prompts.md": ("Do not copy a lens into this document", "**Tenancy / scope-binding pass (v7.0):**",
                            "### Ticket resolution (v7.4)"),
    "refute-and-precedents.md": ("Try to prove it wrong", "**Deep tier:** 2 refuters", "**Standard tier:** 1 refuter",
                                 "**Refuted → downgrade to INFO**"),
    "codex-lane.md": ("Codex-origin candidates are not exempt", "**Hard rule: no finding that originated",
                      "skip the lane with `CODEX_REVIEW_ENABLED=0`"),
    "publish-and-fix.md": ("Read `codex-lane.md` before you publish",),
    "consolidation-detail.md": ("**Effort axis for CRITICAL/HIGH", "### The code-judo question"),
    "validation.md": ("**Backstop, not a crutch (v7.1):**",),
    "mechanical-passes.md": ("**Turning a hit into a finding:**", "**Running `workflow-trust` by hand.**"),
    "principles-and-history.md": ("## Why gate findings record every severity", "Green CI before approval"),
}
HEADING_RE = re.compile(r"(#+)\s+(.*?)\s*$")

# Every heading of commands/deep-review.md at v7.7, the last commit before the
# split, minus "#" level and the version number on the title, mapped to its
# non-blank body lines summed over every occurrence (fences counted, `---` and
# HTML comments not). Every section moved unchanged, so the new layout carries
# at least this many, less one line where a cross-reference was folded into
# another section. The title's body moved
# under principles-and-history.md's "## Version history".
OLD_BODY_LINES = {
    'Deep Review Process': 10,
    '0. Input': 1,
    'Determine review iteration': 4,
    'Re-review process (2nd+ iteration)': 14,
    'Key differences from first review': 4,
    'Ticket resolution (v7.4)': 15,
    '1. Triage': 0,
    'Gather context': 3,
    'CI / Checks status': 11,
    'Classify depth': 5,
    'File-size threshold (mechanical, v6.8)': 9,
    'Large PRs (>10K lines)': 8,
    'Check cross-PR conflicts': 3,
    'Generated-code exclusions (v7.1)': 9,
    '1.5. AI-Cheat Mechanical Pass (v7.2)': 30,
    '1.6. Changed-Surface Enumeration (v7.3)': 20,
    '2. Branch Alignment (mandatory)': 2,
    'Process': 47,
    'Multi-repo reviews: directory verification': 3,
    'Squash-merge ancestry': 3,
    'Orthogonal divergence': 12,
    'Why this step exists': 2,
    '3. Static Analysis (parallel agents)': 1,
    'Severity criteria (include in every prompt)': 5,
    'Required finding format (v6.9, include in every prompt)': 7,
    'Repo invariants (v7.0, mandatory)': 16,
    'Commonly missed (mandatory, every repo)': 8,
    'Agent types': 3,
    'Quality reviewer: detection prompts': 46,
    'Ticket-compliance agent (v7.4)': 10,
    'Multi-PR same-repo: branch contamination': 6,
    'Diff format for agent prompts (v7.1)': 9,
    'Codex second-opinion lane (optional, OFF by default, v7.5)': 17,
    '3.5 Adversarial refute pass (before consolidation)': 7,
    'Diff format for refuter prompts (v7.1)': 1,
    'Precedent ledger loader (v7.1)': 7,
    '4. Consolidation': 7,
    'Severity decision table (v6.9)': 21,
    'Ticket-compliance severity mapping (v7.4)': 7,
    'Classify finding origin': 7,
    'Codex second-opinion divergence (v7.5)': 10,
    'Structural severity calibration (v6.8)': 5,
    'The code-judo question (v6.8)': 1,
    '5. Empirical Validation': 0,
    '5a. Setup': 4,
    '5b. Code validation': 5,
    '5c. Runtime validation': 7,
    '5d. Infrastructure validation': 1,
    'Migrations (Alembic or equivalent)': 6,
    'Docker / Deployment': 4,
    'Environment & Config': 5,
    'Integration Point Registration': 5,
    'Rate Limiting': 4,
    'Background Tasks (Celery or equivalent)': 4,
    'Database': 5,
    'External Services': 4,
    '5e. Update document': 2,
    '5f. Test-vacuity check (v7.0)': 23,
    '6. Plan Mode (deep review only)': 3,
    '7. LT Review': 4,
    '8. Publish': 0,
    'GitHub reviews': 11,
    'Ticket-compliance publish block (v7.4)': 16,
    'Codex-origin findings: publish gate (v7.5)': 3,
    'Multi-repo publish discipline': 7,
    'Communication': 2,
    '9. Re-review': 12,
    '10. LT Direct Fix (optional)': 1,
    'When to use': 6,
    'When NOT to use': 3,
    'Key rules': 4,
    'Principles': 0,
    'On judgment': 3,
    'On evidence': 4,
    'On collaboration': 3,
    'On safety': 5,
    'Review Precedents (v7.1)': 3,
    'Corpus-derived (our own dispositions)': 36,
    'Adopted (curated from external precedent)': 16,
    'Rejected — do not re-add': 3,
    'Begin': 1,
}
BODY_SLACK = 1
# How often each heading occurred. Deleting one from here is deleting a section.
OLD_HEADINGS = collections.Counter({h: 1 for h in OLD_BODY_LINES})
OLD_HEADINGS["Process"] = 2  # Step 2's and Step 10's


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _core():
    text = _read(PRLAUNCH)
    return text[text.index(START) + len(START):text.index(END)]


def _refs():
    return {os.path.basename(p): _read(p) for p in sorted(glob.glob(os.path.join(REF_DIR, "*.md")))}


def _sections(text):
    """(heading text -> occurrences, heading text -> non-blank body lines)."""
    heads, body, cur, fence = collections.Counter(), collections.Counter(), None, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            if cur is not None:
                body[cur] += 1
            continue
        m = None if fence else HEADING_RE.match(line)
        if m:
            cur = m.group(2)
            heads[cur] += 1
        elif cur is not None and line.strip() and line.strip() != "---" and not line.startswith("<!--"):
            body[cur] += 1
    return heads, body


def _headings(text):
    return set(_sections(text)[0])


def _layout_sections():
    heads, body = collections.Counter(), collections.Counter()
    for text in [_core(), _read(ROUTER)] + list(_refs().values()):
        h, b = _sections(text)
        heads += h
        body += b
    return heads, body


class CoreIsInlineTest(unittest.TestCase):
    def setUp(self):
        self.core = _core()

    def test_core_is_inside_prlaunch_section_1(self):
        text = _read(PRLAUNCH)
        self.assertEqual(text.count(START), 1)
        self.assertEqual(text.count(END), 1)
        self.assertLess(text.index("## 1. Deep review loop"), text.index(START))
        self.assertLess(text.index(END), text.index("## 2. CR CLI loop"))

    def test_core_size_is_bounded(self):
        size = len(self.core.encode("utf-8"))
        self.assertLessEqual(size, CORE_MAX_BYTES, "core is %d bytes" % size)

    def test_prlaunch_as_a_whole_fits_one_plain_read(self):
        size = len(_read(PRLAUNCH).encode("utf-8"))
        self.assertLessEqual(size, PRLAUNCH_MAX_BYTES, "PRlaunch is %d bytes" % size)

    def test_core_says_to_launch_independent_reviewers(self):
        self.assertIn("Launch agents in parallel with calibrated prompts.", self.core)
        self.assertIn("Classify depth", self.core)
        for row in ("**Deep**", "**Standard**", "**Quick**"):
            self.assertIn(row, self.core)
        self.assertIn("Quick row only", self.core)

    def test_core_carries_the_rules_that_run_on_every_review(self):
        for needle in (
            "Review Invariants",              # Step 3 loader
            "commonly-missed.py prompt",      # Step 3 lens loader
            "codex-review.sh run",            # Step 3 optional lane
            "spawn an independent **refuter**",  # Step 3.5
            "**Assign severity via the decision table below**",  # Step 4
            "No evidence ⇒ max MEDIUM",
            "**IN-SCOPE**",                   # origin table PRlaunch relies on
            "CONFIRMED / PARTIALLY CONFIRMED / NOT CONFIRMED",  # 5b
            "#### Check cross-PR conflicts",
            "#### 5e. Update document",
            "not a special case here",        # how an invariant gets added
            "### 5f. Test-vacuity check",
            "It MUST fail. Quote the failure output.",
        ):
            self.assertIn(needle, self.core, needle)

    def test_every_step_1_5_detector_is_named_and_has_a_checklist_row(self):
        # No scripted cheat-detect.sh ships here, so the core names each
        # detector and mechanical-passes.md carries one table row per name.
        line = re.search(r"^Detectors: (.*)\.$", self.core, re.M)
        self.assertIsNotNone(line, "core must name the Step 1.5 detectors")
        names = re.findall(r"`([a-z-]+)`", line.group(1))
        self.assertEqual(len(names), DETECTORS)
        self.assertEqual(len(set(names)), DETECTORS)
        self.assertIn("workflow-trust", names)
        table = _refs()["mechanical-passes.md"]
        rows = re.findall(r"^\| `([a-z-]+)` \|", table, re.M)
        self.assertEqual(rows, names)
        self.assertIn("**%d structural detectors**" % DETECTORS, table)

    def test_codex_lane_stays_off_by_default(self):
        self.assertIn("Skip this unless you turned it on", self.core)
        for text in [self.core, _refs()["codex-lane.md"]]:
            self.assertNotIn("ON by default", text)
            self.assertIn("CODEX_REVIEW_ENABLED=1", text)
        self.assertIn("Off unless you turn it on.", _refs()["codex-lane.md"])

    def test_test_vacuity_never_reverts_with_git_stash(self):
        self.assertNotIn("`git stash` the source", self.core)
        self.assertIn("Never `git stash` it", self.core)

    def test_core_headings_do_not_collide_with_prlaunch_phases(self):
        fence = False
        for line in self.core.splitlines():
            if line.lstrip().startswith("```"):
                fence = not fence
                continue
            m = None if fence else HEADING_RE.match(line)
            if m:
                self.assertGreaterEqual(len(m.group(1)), 3, line)


class NothingFellOutOfReachTest(unittest.TestCase):
    def setUp(self):
        self.core = _core()
        self.refs = _refs()
        self.router = _read(ROUTER)

    def test_every_old_heading_survives_as_often_as_it_occurred(self):
        heads, _ = _layout_sections()
        missing = {h: n for h, n in OLD_HEADINGS.items() if heads[h] < n}
        self.assertEqual(missing, {})

    def test_every_old_section_body_moved_with_its_heading(self):
        # A heading kept over an emptied body is a deleted section too.
        _, body = _layout_sections()
        short = {h: (n, body[h]) for h, n in OLD_BODY_LINES.items()
                 if h != "Deep Review Process" and body[h] < n - BODY_SLACK}
        self.assertEqual(short, {})

    def test_every_reference_is_reachable_from_the_core_and_the_map(self):
        self.assertEqual(len(self.refs), 9, sorted(self.refs))
        for name in self.refs:
            self.assertIn(name, self.core, "core does not name %s" % name)
            self.assertIn(name, self.router, "deep-review.md does not name %s" % name)

    def test_every_named_reference_exists(self):
        for text in [self.core, self.router] + list(self.refs.values()):
            for name in PATH_RE.findall(text):
                self.assertIn(name, self.refs, name)
        # They are also named bare, relative to the stated directory.
        for text in [self.core, self.router] + list(self.refs.values()):
            for name in BARE_NAME_RE.findall(text):
                if name not in ("deep-review.md", "commonly-missed.md"):
                    self.assertIn(name, self.refs, name)

    def test_every_reference_has_a_trigger_row_in_the_core(self):
        rows = [ln for ln in self.core.splitlines() if ln.startswith("| `")]
        for name in self.refs:
            self.assertTrue([r for r in rows if r.startswith("| `%s` |" % name)], name)

    def test_each_reference_header_repeats_its_core_row(self):
        rows = dict(re.findall(r"^\| `([a-z-]+\.md)` \| (.*?) \|$", self.core, re.M))
        for name, text in self.refs.items():
            when = re.search(r"\*\*Read this file when:\*\* (.*)$", text, re.M).group(1)
            self.assertEqual(when[:len(rows[name])].lower(), rows[name].lower(), name)

    def test_references_resolve_from_the_live_config_not_the_reviewed_repo(self):
        self.assertIn("~/.claude/playbooks/deep-review/", self.core)
        self.assertNotRegex(self.router, r"`playbooks/deep-review/")
        self.assertNotRegex(self.core, r"`playbooks/deep-review/")

    def test_hooks_are_named_by_absolute_path(self):
        # A relative hooks/x.sh only resolves in a config checkout. In any other
        # repo it exits 127, and a PR could supply its own script there.
        for label, text in [("core", self.core), ("map", self.router)] + list(self.refs.items()):
            self.assertEqual(RELATIVE_HOOK_RE.findall(text), [], label)

    def test_moved_rules_are_where_the_map_says(self):
        for name, phrases in MOVED_RULES.items():
            for phrase in phrases:
                self.assertIn(phrase, self.refs[name], "%s: %s" % (name, phrase))

    def test_core_commands_use_one_target_branch(self):
        # The core keeps this repo's `origin/develop` convention and says how
        # to substitute the PR's real base; it must not mix in a second ref.
        self.assertIn("baseRefName", self.core)
        self.assertNotIn("origin/main", self.core)
        self.assertIn("origin/develop...HEAD", self.core)

    def test_step_5f_outranks_the_test_only_severity_cap(self):
        self.assertIn("**Step 5f takes precedence over this cap:**", self.refs["refute-and-precedents.md"])

    def test_precedent_loader_prints_all_twenty_precedents(self):
        ref = self.refs["refute-and-precedents.md"]
        cmd = re.search(r"^awk '.*Review Precedents.*$", ref, re.M).group(0)
        self.assertIn("~/.claude/playbooks/deep-review/refute-and-precedents.md", cmd)
        cmd = cmd.replace("~/.claude/", REPO_ROOT + "/")
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True).stdout
        self.assertIn("## Review Precedents", out)
        for n in (1, 15, 16, 20):
            self.assertIn("**%d. " % n, out)
        self.assertIn("### Rejected", out)


class MapTest(unittest.TestCase):
    def setUp(self):
        self.router = _read(ROUTER)

    def test_map_is_small_and_still_a_command(self):
        size = len(self.router.encode("utf-8"))
        self.assertLessEqual(size, ROUTER_MAX_BYTES, "deep-review.md is %d bytes" % size)
        self.assertIn("$ARGUMENTS", self.router)
        self.assertIn("Start at Step 0 (Input)", self.router)

    def test_map_prints_exactly_the_core(self):
        cmd = re.search(r"^awk .*deep-review-core.*$", self.router, re.M).group(0)
        cmd = cmd.replace("~/.claude/", REPO_ROOT + "/")
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True).stdout
        self.assertIn("### 5f. Test-vacuity check", out)
        self.assertIn("Launch agents in parallel", out)
        self.assertNotIn("## 2. CR CLI loop", out)
        self.assertNotIn(START, out)
        self.assertNotIn(END, out)


class VersionClaimsTest(unittest.TestCase):
    def test_no_current_version_claim_on_the_map_or_prlaunch(self):
        self.assertNotRegex(_read(ROUTER), r"Deep Review Process v\d")
        self.assertNotRegex(_read(PRLAUNCH), r"deep-review \*process\* \(v\d")

    def test_version_history_reaches_v7_7(self):
        history = _read(os.path.join(REF_DIR, "principles-and-history.md")).split("## Version history", 1)[1]
        self.assertIn("**v7.5 → v7.7:**", history)


if __name__ == "__main__":
    unittest.main()
