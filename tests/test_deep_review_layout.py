"""deep-review's core is inline in PRlaunch, the rest is in references.

commands/deep-review.md reached 100,887 bytes at v7.7, past what one plain Read
loads, and PRlaunch only named its path, so most PRlaunch sessions never opened
it. The core that runs on every review now sits between markers in PRlaunch
section 1, and everything else moved unchanged to playbooks/deep-review/.
These tests pin that nothing fell out of reach.
"""
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
# split, minus "#" level and the version number on the title. Deleting one
# from here is deleting a section.
OLD_HEADINGS = [
    'Deep Review Process',
    '0. Input',
    'Determine review iteration',
    'Re-review process (2nd+ iteration)',
    'Key differences from first review',
    'Ticket resolution (v7.4)',
    '1. Triage',
    'Gather context',
    'CI / Checks status',
    'Classify depth',
    'File-size threshold (mechanical, v6.8)',
    'Large PRs (>10K lines)',
    'Check cross-PR conflicts',
    'Generated-code exclusions (v7.1)',
    '1.5. AI-Cheat Mechanical Pass (v7.2)',
    '1.6. Changed-Surface Enumeration (v7.3)',
    '2. Branch Alignment (mandatory)',
    'Process',
    'Multi-repo reviews: directory verification',
    'Squash-merge ancestry',
    'Orthogonal divergence',
    'Why this step exists',
    '3. Static Analysis (parallel agents)',
    'Severity criteria (include in every prompt)',
    'Required finding format (v6.9, include in every prompt)',
    'Repo invariants (v7.0, mandatory)',
    'Commonly missed (mandatory, every repo)',
    'Agent types',
    'Quality reviewer: detection prompts',
    'Ticket-compliance agent (v7.4)',
    'Multi-PR same-repo: branch contamination',
    'Diff format for agent prompts (v7.1)',
    'Codex second-opinion lane (optional, OFF by default, v7.5)',
    '3.5 Adversarial refute pass (before consolidation)',
    'Diff format for refuter prompts (v7.1)',
    'Precedent ledger loader (v7.1)',
    '4. Consolidation',
    'Severity decision table (v6.9)',
    'Ticket-compliance severity mapping (v7.4)',
    'Classify finding origin',
    'Codex second-opinion divergence (v7.5)',
    'Structural severity calibration (v6.8)',
    'The code-judo question (v6.8)',
    '5. Empirical Validation',
    '5a. Setup',
    '5b. Code validation',
    '5c. Runtime validation',
    '5d. Infrastructure validation',
    'Migrations (Alembic or equivalent)',
    'Docker / Deployment',
    'Environment & Config',
    'Integration Point Registration',
    'Rate Limiting',
    'Background Tasks (Celery or equivalent)',
    'Database',
    'External Services',
    '5e. Update document',
    '5f. Test-vacuity check (v7.0)',
    '6. Plan Mode (deep review only)',
    '7. LT Review',
    '8. Publish',
    'GitHub reviews',
    'Ticket-compliance publish block (v7.4)',
    'Codex-origin findings: publish gate (v7.5)',
    'Multi-repo publish discipline',
    'Communication',
    '9. Re-review',
    '10. LT Direct Fix (optional)',
    'When to use',
    'When NOT to use',
    'Process',
    'Key rules',
    'Principles',
    'On judgment',
    'On evidence',
    'On collaboration',
    'On safety',
    'Review Precedents (v7.1)',
    'Corpus-derived (our own dispositions)',
    'Adopted (curated from external precedent)',
    'Rejected — do not re-add',
    'Begin',
]


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _core():
    text = _read(PRLAUNCH)
    return text[text.index(START) + len(START):text.index(END)]


def _refs():
    return {os.path.basename(p): _read(p) for p in sorted(glob.glob(os.path.join(REF_DIR, "*.md")))}


def _headings(text):
    out, fence = set(), False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        m = None if fence else HEADING_RE.match(line)
        if m:
            out.add(m.group(2))
    return out


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
            self.assertNotRegex(text, r"(?i)gpt-\d|astra")
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

    def test_every_old_heading_survives_in_the_core_a_reference_or_the_map(self):
        have = _headings(self.core) | _headings(self.router)
        for text in self.refs.values():
            have |= _headings(text)
        missing = [h for h in OLD_HEADINGS if h not in have]
        self.assertEqual(missing, [])

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
