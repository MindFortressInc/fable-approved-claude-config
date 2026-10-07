"""Tests for hooks/prlaunch-gate.sh — the PRlaunch per-gate evidence ledger."""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (
    HookSandbox, add_commit, make_git_repo, make_worktree, run_hook_args, set_remote,
)


class PrlaunchGateTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()
        self.repo = os.path.join(self.sbx.dir, "myrepo")
        self.branch = "me/eng-42-x"
        self.head = make_git_repo(self.repo, self.branch, self.sbx.env())
        self.repo_name = os.path.basename(self.repo)

    def tearDown(self):
        self.sbx.close()

    # -- helpers ----------------------------------------------------------
    def gate(self, *args):
        return run_hook_args(self.sbx, "prlaunch-gate.sh", ["--repo-dir", self.repo, *args])

    def _scenarios_file(self):
        path = os.path.join(self.sbx.dir, "scen.md")
        with open(path, "w") as fh:
            fh.write("scenario 1: user sees rendered bold, PASS if no literal **\n")
        return path

    def _ledger(self):
        # the ledger is keyed on repo IDENTITY, so a test that calls
        # add_remote() (origin -> .../example-repo.git) no longer lands on
        # <dirname>--<branch>. Ask the gate where it wrote instead of rebuilding
        # the path -- exactly what `path` exists for, and what the hook's own
        # usage text tells callers to do.
        rc, out, _ = self.gate("path")
        assert rc == 0, out
        with open(out.strip()) as fh:
            return json.load(fh)

    def _record_full_valid(self):
        """Register scenarios + record all four gates at the current HEAD."""
        self.assertEqual(self.gate("record", "deep_review")[0], 0)
        self.assertEqual(self.gate("record", "cr_cli")[0], 0)
        self.assertEqual(self.gate("record", "scenarios", self._scenarios_file())[0], 0)
        self.assertEqual(self.gate("record", "outcome_eval")[0], 0)
        self.assertEqual(self.gate("record", "tests", "--cmd", "pytest -q")[0], 0)

    # -- record happy paths ----------------------------------------------
    def test_record_each_gate_happy_path(self):
        self._record_full_valid()
        led = self._ledger()
        self.assertEqual(set(led["gates"]), {"deep_review", "cr_cli", "outcome_eval", "tests"})
        for name, entry in led["gates"].items():
            self.assertEqual(entry["sha"], self.head, "gate %s should stamp HEAD" % name)
            self.assertIn("ts", entry)
        self.assertEqual(led["gates"]["tests"]["cmd"], "pytest -q")
        self.assertEqual(led["scenarios"]["path"], self._scenarios_file())
        self.assertIn("sha256", led["scenarios"])

    def test_check_passes_when_all_recorded_at_head(self):
        self._record_full_valid()
        rc, out, err = self.gate("check")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("OK", out)

    # -- outcome_eval / scenarios rules ----------------------------------
    def test_outcome_eval_refused_before_scenarios(self):
        rc, out, err = self.gate("record", "outcome_eval")
        self.assertEqual(rc, 1)
        self.assertIn("no scenarios registered", out + err)
        # nothing for outcome_eval should have been written
        path = self.sbx.ledger_path(self.repo_name, self.branch)
        if os.path.exists(path):
            self.assertNotIn("outcome_eval", self._ledger().get("gates", {}))

    def test_outcome_eval_na_allowed_without_scenarios(self):
        rc, out, err = self.gate("record", "outcome_eval", "--na", "no user-facing surface")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._ledger()["gates"]["outcome_eval"]["na"], "no user-facing surface")

    # -- cr_cli skip -----------------------------------------------------
    def test_cr_cli_skipped_with_reason_passes_check(self):
        self.assertEqual(self.gate("record", "deep_review")[0], 0)
        self.assertEqual(self.gate("record", "cr_cli", "--skipped", "rate limit")[0], 0)
        self.assertEqual(self.gate("record", "outcome_eval", "--na", "plumbing only")[0], 0)
        self.assertEqual(self.gate("record", "tests")[0], 0)
        rc, out, err = self.gate("check")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._ledger()["gates"]["cr_cli"]["skipped"], "rate limit")

    # -- reason-required / flag-scope errors -----------------------------
    def test_skipped_without_reason_errors(self):
        rc, out, err = self.gate("record", "cr_cli", "--skipped")
        self.assertEqual(rc, 1)
        self.assertIn("--skipped requires a reason", out + err)

    def test_na_without_reason_errors(self):
        rc, out, err = self.gate("record", "outcome_eval", "--na")
        self.assertEqual(rc, 1)
        self.assertIn("--na requires a reason", out + err)

    def test_skipped_only_valid_for_cr_cli(self):
        rc, out, err = self.gate("record", "deep_review", "--skipped", "x")
        self.assertEqual(rc, 1)
        self.assertIn("only valid for cr_cli", out + err)

    def test_na_only_valid_for_outcome_eval(self):
        rc, out, err = self.gate("record", "cr_cli", "--na", "x")
        self.assertEqual(rc, 1)
        self.assertIn("only valid for outcome_eval", out + err)

    # -- scenarios drift --------------------------------------------------
    # The registered sha256 is the whole point of pre-registering scenarios:
    # they must be written BEFORE the eval runs. Recording outcome_eval has to
    # re-hash and refuse, or the stored hash is decorative.
    def test_outcome_eval_refused_when_scenarios_drifted(self):
        scen = self._scenarios_file()
        self.assertEqual(self.gate("record", "scenarios", scen)[0], 0)
        with open(scen, "w") as fh:
            fh.write("scenario 1: rewritten AFTER the eval to match what shipped\n")
        rc, out, err = self.gate("record", "outcome_eval")
        self.assertEqual(rc, 1)
        self.assertIn("DRIFTED", out + err)
        self.assertNotIn("outcome_eval", self._ledger().get("gates", {}))

    def test_outcome_eval_refused_when_scenarios_file_missing(self):
        scen = self._scenarios_file()
        self.assertEqual(self.gate("record", "scenarios", scen)[0], 0)
        os.remove(scen)
        rc, out, err = self.gate("record", "outcome_eval")
        self.assertEqual(rc, 1)
        self.assertIn("no longer exists", out + err)
        self.assertNotIn("outcome_eval", self._ledger().get("gates", {}))

    def test_outcome_eval_records_the_verified_scenarios_sha(self):
        scen = self._scenarios_file()
        self.assertEqual(self.gate("record", "scenarios", scen)[0], 0)
        self.assertEqual(self.gate("record", "outcome_eval")[0], 0)
        led = self._ledger()
        self.assertEqual(
            led["gates"]["outcome_eval"]["scenarios_sha256"],
            led["scenarios"]["sha256"],
            "the verified hash belongs on the gate entry as durable evidence",
        )

    def test_outcome_eval_na_ignores_scenario_drift(self):
        # --na means "no user-facing surface", so there are no scenarios to
        # drift from; the check must not fire on that path.
        scen = self._scenarios_file()
        self.assertEqual(self.gate("record", "scenarios", scen)[0], 0)
        os.remove(scen)
        rc, out, err = self.gate("record", "outcome_eval", "--na", "no user-facing surface")
        self.assertEqual(rc, 0, out + err)
        entry = self._ledger()["gates"]["outcome_eval"]
        self.assertNotIn("scenarios_sha256", entry,
                         "--na must not carry stale scenario evidence")

    # -- check failure modes ---------------------------------------------
    def test_check_missing_gate_names_it(self):
        self.assertEqual(self.gate("record", "deep_review")[0], 0)
        rc, out, err = self.gate("check")
        self.assertEqual(rc, 1)
        self.assertIn("MISSING gate: cr_cli", out + err)

    def test_check_fails_naming_stale_gate_after_new_commit(self):
        self._record_full_valid()
        self.assertEqual(self.gate("check")[0], 0)
        new_head = add_commit(self.repo, self.sbx.env())
        self.assertNotEqual(new_head, self.head)
        rc, out, err = self.gate("check")
        self.assertEqual(rc, 1)
        self.assertIn("STALE gate: deep_review", out + err)
        self.assertIn(new_head[:8], out + err)  # prescriptive: re-run on new HEAD

    # -- append-only history ------------------------------------
    def test_history_keeps_both_stamps_across_two_heads(self):
        """The bug this ticket fixes: re-stamping the same gate at a new HEAD
        used to destroy the previous HEAD's record. `.history` must keep BOTH,
        while `.gates.<gate>.sha` still reflects only the latest stamp (existing
        readers are unaffected)."""
        self.assertEqual(self.gate("record", "deep_review")[0], 0)
        first_head = self.head
        second_head = add_commit(self.repo, self.sbx.env())
        self.assertNotEqual(second_head, first_head)
        self.assertEqual(self.gate("record", "deep_review")[0], 0)

        led = self._ledger()
        self.assertIn("history", led)
        hist = [e for e in led["history"] if e["gate"] == "deep_review"]
        self.assertEqual(len(hist), 2, hist)
        self.assertEqual({e["sha"] for e in hist}, {first_head, second_head})
        # the current-state view is untouched: latest sha only, no eviction visible
        self.assertEqual(led["gates"]["deep_review"]["sha"], second_head)

    def test_history_accumulates_across_different_gates_too(self):
        """Recording different gates each appends its own entry -- history is a
        flat log of every record call, not just re-stamps of one gate."""
        self.assertEqual(self.gate("record", "deep_review")[0], 0)
        self.assertEqual(self.gate("record", "cr_cli")[0], 0)
        led = self._ledger()
        self.assertEqual(len(led["history"]), 2)
        self.assertEqual([e["gate"] for e in led["history"]], ["deep_review", "cr_cli"])

    def test_gates_shape_unchanged_by_single_record(self):
        """Regression: a single record call's `.gates` entry must be identical in
        shape/values to the pre-history baseline -- existing readers (pr-gate.sh,
        babysit, `check`) must see zero change from the history addition."""
        self.assertEqual(self.gate("record", "tests", "--cmd", "pytest -q")[0], 0)
        led = self._ledger()
        entry = led["gates"]["tests"]
        self.assertEqual(set(entry.keys()), {"sha", "ts", "cmd"})
        self.assertEqual(entry["sha"], self.head)
        self.assertEqual(entry["cmd"], "pytest -q")
        # `.gates[gate]` must NOT pick up the `gate` key that `.history` entries carry
        self.assertNotIn("gate", entry)

    def test_check_emits_history_in_output(self):
        """Work item 4: `check`'s output must visibly show this is a ledger, not
        a latch -- i.e. it names the history, not just the current gate state."""
        self._record_full_valid()
        rc, out, err = self.gate("check")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("history", (out + err).lower())


def add_remote(path, env, url="https://github.com/example-org/example-repo.git"):
    """Add an `origin` remote to the throwaway repo so `gh api {owner}/{repo}/...`
    has something to resolve. Real gh is never invoked in this suite -- see
    GH_STUB below -- so the URL need not be reachable."""
    subprocess.run(
        ["git", "-C", path, "remote", "add", "origin", url],
        check=True, env=env, capture_output=True, text=True,
    )


# Stub `gh` for the review-gate/cr-cli publish path -- same technique
# hooks/cr-review.test.sh uses to stub `coderabbit`: a fake executable placed
# first on PATH inside a throwaway sandbox, so no real commit status is ever
# posted. Records every invocation's argv (NUL-safe-ish via \x1e, since a
# description can contain spaces/newlines-free text) to $GH_CAPTURE_FILE, one
# line per call, so a test can assert exactly which endpoint/state/context/
# description prlaunch-gate.sh sent. $GH_STUB_FAIL=1 simulates a network/auth
# failure (non-zero exit, message on stderr) without touching the network.
GH_STUB = r"""#!/bin/bash
if [ -n "${GH_CAPTURE_FILE:-}" ]; then
  { for a in "$@"; do printf '%s\x1e' "$a"; done; printf '\n'; } >> "$GH_CAPTURE_FILE"
fi
if [ "${GH_STUB_FAIL:-0}" = "1" ]; then
  echo "gh: simulated failure (network/auth)" >&2
  exit 1
fi
exit 0
"""


def parse_gh_calls(capture_path):
    """Parse GH_STUB's capture file into [{'endpoint':..., 'fields': {...}}, ...].

    Invocations look like `[timeout 15] gh api <endpoint> -f k=v -f k=v ...`;
    the stub only ever sees its own argv (i.e. everything after `gh`), so the
    first token is the `api` subcommand and the second is the endpoint.
    """
    if not os.path.exists(capture_path):
        return []
    calls = []
    with open(capture_path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            args = [a for a in line.split("\x1e") if a != ""]
            if not args or args[0] != "api" or len(args) < 2:
                continue
            endpoint = args[1]
            fields = {}
            i = 2
            while i < len(args):
                if args[i] == "-f" and i + 1 < len(args):
                    k, _, v = args[i + 1].partition("=")
                    fields[k] = v
                    i += 2
                else:
                    i += 1
            calls.append({"endpoint": endpoint, "fields": fields})
    return calls


class PublishReviewGateStatusTest(unittest.TestCase):
    """`record cr_cli` additionally publishes a `review-gate/cr-cli` commit
    status at the exact recorded sha. Covers: success/pending
    verdicts, the bare `review-gate` context is never written, fail-soft
    behaviour (no gh / no remote / gh failure never breaks the ledger write),
    sha correctness across a stale re-record, and the optional --seat/--run-id
    plumbing into the description."""

    def setUp(self):
        self.sbx = HookSandbox()
        self.repo = os.path.join(self.sbx.dir, "myrepo")
        self.branch = "me/eng-42-x"
        self.head = make_git_repo(self.repo, self.branch, self.sbx.env())
        self.repo_name = os.path.basename(self.repo)

    def tearDown(self):
        self.sbx.close()

    def gate(self, *args, extra_env=None):
        # Status publishing is opt-in; this class exercises the publisher, so
        # it opts in. UnconfiguredDefaultsTest covers the default (off) path.
        env = {"PRLAUNCH_PUBLISH_CR_STATUS": "1"}
        env.update(extra_env or {})
        return run_hook_args(
            self.sbx, "prlaunch-gate.sh", ["--repo-dir", self.repo, *args],
            extra_env=env,
        )

    def _ledger(self):
        # the ledger is keyed on repo IDENTITY, so a test that calls
        # add_remote() (origin -> .../example-repo.git) no longer lands on
        # <dirname>--<branch>. Ask the gate where it wrote instead of rebuilding
        # the path -- exactly what `path` exists for, and what the hook's own
        # usage text tells callers to do.
        rc, out, _ = self.gate("path")
        assert rc == 0, out
        with open(out.strip()) as fh:
            return json.load(fh)

    def _install_gh_stub(self, fail=False):
        binp = os.path.join(self.sbx.dir, "ghbin")
        os.makedirs(binp, exist_ok=True)
        shim = os.path.join(binp, "gh")
        with open(shim, "w") as fh:
            fh.write(GH_STUB)
        os.chmod(shim, 0o755)
        capture = os.path.join(self.sbx.dir, "gh-capture.txt")
        extra_env = {
            "GH_CAPTURE_FILE": capture,
            "PATH": binp + os.pathsep + os.environ.get("PATH", ""),
        }
        if fail:
            extra_env["GH_STUB_FAIL"] = "1"
        return capture, extra_env

    # -- (1) clean record publishes success at the correct head sha ------
    def test_cr_cli_clean_publishes_success_at_head_sha(self):
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        rc, out, err = self.gate("record", "cr_cli", extra_env=extra_env)
        self.assertEqual(rc, 0, out + err)
        calls = parse_gh_calls(capture)
        self.assertEqual(len(calls), 1, calls)
        # The endpoint carries an EXPLICIT owner/repo slug resolved from the
        # checkout's origin remote, not gh's `{owner}/{repo}` placeholder
        #: babysit publishes for repos it is not cd-ed into, so the
        # shared publisher can no longer infer the target from cwd.
        self.assertEqual(
            calls[0]["endpoint"],
            "repos/example-org/example-repo/statuses/" + self.head)
        self.assertEqual(calls[0]["fields"]["state"], "success")
        self.assertEqual(calls[0]["fields"]["context"], "review-gate/cr-cli")

    # -- (2) a --skipped record publishes pending, never success ---------
    def test_cr_cli_skipped_publishes_pending_never_success(self):
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        rc, out, err = self.gate(
            "record", "cr_cli", "--skipped", "all seats spent (exit 75)",
            extra_env=extra_env,
        )
        self.assertEqual(rc, 0, out + err)
        calls = parse_gh_calls(capture)
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["fields"]["state"], "pending")
        self.assertNotEqual(calls[0]["fields"]["state"], "success")
        self.assertNotEqual(calls[0]["fields"]["state"], "failure")

    def test_a_missing_publisher_does_not_break_the_ledger_or_check(self):
        """`check` is what the global pr-gate hook runs on EVERY `gh pr create`.
        A clone missing review-gate-status.sh must lose the advisory status, not
        block every PR in the fleet."""
        import shutil
        lone = os.path.join(self.sbx.dir, "lonely-hooks")
        os.makedirs(lone, exist_ok=True)
        hook = os.path.join(lone, "prlaunch-gate.sh")
        shutil.copy(os.path.realpath(self.sbx.hook_path("prlaunch-gate.sh")), hook)
        shutil.copy(os.path.realpath(self.sbx.hook_path("ledger-append.sh")),
                    os.path.join(lone, "ledger-append.sh"))
        self.assertFalse(os.path.exists(os.path.join(lone, "review-gate-status.sh")))

        def run(*args):
            return subprocess.run(
                ["bash", hook, "--repo-dir", self.repo, *args],
                capture_output=True, text=True, env=self.sbx.env())

        self.assertEqual(run("record", "cr_cli").returncode, 0)
        self.assertEqual(run("record", "deep_review").returncode, 0)
        scen = os.path.join(self.sbx.dir, "s.md")
        with open(scen, "w") as fh:
            fh.write("scenario\n")
        self.assertEqual(run("record", "scenarios", scen).returncode, 0)
        self.assertEqual(run("record", "outcome_eval").returncode, 0)
        self.assertEqual(run("record", "tests", "--cmd", "pytest").returncode, 0)
        proc = run("check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    # -- (3) the bare `review-gate` context is NEVER posted by this path -
    def test_publish_never_writes_bare_review_gate_context(self):
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        self.assertEqual(self.gate("record", "cr_cli", extra_env=extra_env)[0], 0)
        add_commit(self.repo, self.sbx.env())
        self.assertEqual(
            self.gate("record", "cr_cli", "--skipped", "rate limit", extra_env=extra_env)[0],
            0,
        )
        calls = parse_gh_calls(capture)
        self.assertGreaterEqual(len(calls), 2, calls)
        for call in calls:
            self.assertEqual(call["fields"].get("context"), "review-gate/cr-cli")
            self.assertNotEqual(call["fields"].get("context"), "review-gate")

    # -- (4) fail-soft: publish failure never breaks the local ledger write
    def test_publish_best_effort_survives_gh_missing(self):
        add_remote(self.repo, self.sbx.env())
        # A PATH with everything prlaunch-gate.sh needs (git/jq/bash/coreutils)
        # but no gh, simulating a box where the CLI isn't installed.
        no_gh_env = {"PATH": "/usr/bin:/bin:/sbin"}
        rc, out, err = self.gate("record", "cr_cli", extra_env=no_gh_env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("gh not on PATH", out + err)
        self.assertEqual(self._ledger()["gates"]["cr_cli"]["sha"], self.head)

    def test_publish_best_effort_survives_no_remote(self):
        # No `git remote add` here -- a genuinely local-only repo.
        capture, extra_env = self._install_gh_stub()
        rc, out, err = self.gate("record", "cr_cli", extra_env=extra_env)
        self.assertEqual(rc, 0, out + err)
        # A repo with no origin resolves to an empty slug, which is the shared
        # publisher's skip condition.
        self.assertIn("no owner/repo slug", out + err)
        self.assertEqual(parse_gh_calls(capture), [])
        self.assertEqual(self._ledger()["gates"]["cr_cli"]["sha"], self.head)

    def test_publish_best_effort_survives_gh_failure(self):
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub(fail=True)
        rc, out, err = self.gate("record", "cr_cli", extra_env=extra_env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("WARNING", out + err)
        self.assertIn("simulated failure", out + err)
        self.assertEqual(self._ledger()["gates"]["cr_cli"]["sha"], self.head)

    # -- correct sha under a stale-gate re-record -------------------------
    def test_publish_uses_current_head_not_stale_sha(self):
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        self.assertEqual(self.gate("record", "cr_cli", extra_env=extra_env)[0], 0)
        old_head = self.head
        new_head = add_commit(self.repo, self.sbx.env())
        self.assertNotEqual(new_head, old_head)
        self.assertEqual(self.gate("record", "cr_cli", extra_env=extra_env)[0], 0)
        calls = parse_gh_calls(capture)
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[0]["endpoint"].endswith("/statuses/" + old_head))
        self.assertTrue(calls[1]["endpoint"].endswith("/statuses/" + new_head))

    # -- description carries verdict + findings + seat + run id ----------
    def test_seat_and_run_id_appear_in_published_description(self):
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        rc, out, err = self.gate(
            "record", "cr_cli", "--seat", "seat-a", "--run-id", "run-42",
            extra_env=extra_env,
        )
        self.assertEqual(rc, 0, out + err)
        desc = parse_gh_calls(capture)[0]["fields"]["description"]
        self.assertIn("seat=seat-a", desc)
        self.assertIn("run=run-42", desc)
        self.assertIn("verdict=reviewed", desc)

    # -- seat/run-id must survive in the ledger (source of truth), not just
    # the best-effort GitHub status -- CR CLI review finding.
    def test_seat_and_run_id_persisted_in_ledger(self):
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        rc, out, err = self.gate(
            "record", "cr_cli", "--seat", "seat-a", "--run-id", "run-42",
            extra_env=extra_env,
        )
        self.assertEqual(rc, 0, out + err)
        entry = self._ledger()["gates"]["cr_cli"]
        self.assertEqual(entry["seat"], "seat-a")
        self.assertEqual(entry["run_id"], "run-42")

    def test_seat_and_run_id_absent_from_ledger_when_not_passed(self):
        """Backward compat: omitting the flags must not write empty/null keys."""
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        rc, out, err = self.gate("record", "cr_cli", extra_env=extra_env)
        self.assertEqual(rc, 0, out + err)
        entry = self._ledger()["gates"]["cr_cli"]
        self.assertNotIn("seat", entry)
        self.assertNotIn("run_id", entry)

    def test_findings_count_appears_in_published_description(self):
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        findings = json.dumps([{"severity": "medium"}, {"severity": "low"}])
        rc, out, err = self.gate(
            "record", "cr_cli", "--findings", findings, extra_env=extra_env,
        )
        self.assertEqual(rc, 0, out + err)
        desc = parse_gh_calls(capture)[0]["fields"]["description"]
        self.assertIn("findings=2", desc)

    def test_long_skip_reason_does_not_evict_seat_run_findings(self):
        """Outcome eval (live, real gh, real pushed sha): a verbose skip
        reason -- realistic, cr-review.sh's own exhaustion messages run long --
        must not silently drop seat/run/findings from the 140-char description
        just because `reason` was built first and ate the whole budget."""
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        long_reason = "all seats at cap (10/10 seat-a/seat-b/seat-c) -- cr-review.sh exit 75, " \
            "verified via cr-seats.sh list, no stale coderabbit process found anywhere"
        rc, out, err = self.gate(
            "record", "cr_cli", "--skipped", long_reason,
            "--seat", "pool-exhausted", "--run-id", "run-99",
            extra_env=extra_env,
        )
        self.assertEqual(rc, 0, out + err)
        desc = parse_gh_calls(capture)[0]["fields"]["description"]
        self.assertLessEqual(len(desc), 140, desc)
        self.assertIn("seat=pool-exhausted", desc, desc)
        self.assertIn("run=run-99", desc, desc)
        self.assertIn("findings=", desc, desc)

    def test_backward_compatible_without_seat_or_run_id(self):
        """Existing callers that pass neither flag keep working unchanged."""
        add_remote(self.repo, self.sbx.env())
        capture, extra_env = self._install_gh_stub()
        rc, out, err = self.gate("record", "cr_cli", extra_env=extra_env)
        self.assertEqual(rc, 0, out + err)
        calls = parse_gh_calls(capture)
        self.assertEqual(calls[0]["fields"]["state"], "success")
        self.assertIn("seat=n/a", calls[0]["fields"]["description"])
        self.assertIn("run=n/a", calls[0]["fields"]["description"])

    # -- --seat/--run-id are scoped to cr_cli, like --skipped/--na --------
    def test_seat_only_valid_for_cr_cli(self):
        rc, out, err = self.gate("record", "deep_review", "--seat", "seat-a")
        self.assertEqual(rc, 1)
        self.assertIn("only valid for cr_cli", out + err)

    def test_run_id_only_valid_for_cr_cli(self):
        rc, out, err = self.gate("record", "deep_review", "--run-id", "x")
        self.assertEqual(rc, 1)
        self.assertIn("only valid for cr_cli", out + err)


class LedgerKeyIdentityTest(unittest.TestCase):
    """The ledger key must identify the REPO, not the worktree dir.

    `basename "$(git rev-parse --show-toplevel)"` returns the WORKTREE directory
    inside a worktree, so two repos whose worktrees are both named after the
    ticket (`.../api-svc-worktrees/eng-8804` and
    `.../web-app-worktrees/eng-8804`) produced the identical key — one
    shared ledger for two repositories. Observed twice in production.
    """

    BRANCH = "me/eng-9254-x"

    def setUp(self):
        self.sbx = HookSandbox()
        self.e = self.sbx.env()
        # Two DIFFERENT repos ...
        self.svc = os.path.join(self.sbx.dir, "api-svc")
        self.fe = os.path.join(self.sbx.dir, "web-app")
        make_git_repo(self.svc, "trunk", self.e)
        make_git_repo(self.fe, "trunk", self.e)
        set_remote(self.svc, "https://github.com/Org/api-svc.git", self.e)
        set_remote(self.fe, "https://github.com/Org/web-app.git", self.e)
        # ... whose worktrees share a directory basename, on the same branch.
        self.svc_wt = os.path.join(self.sbx.dir, "svc-worktrees", "eng-9254")
        self.fe_wt = os.path.join(self.sbx.dir, "fe-worktrees", "eng-9254")
        self.svc_head = make_worktree(self.svc, self.svc_wt, self.BRANCH, self.e, "svc work")
        self.fe_head = make_worktree(self.fe, self.fe_wt, self.BRANCH, self.e, "fe work")
        self.assertNotEqual(self.svc_head, self.fe_head)
        self.assertEqual(os.path.basename(self.svc_wt), os.path.basename(self.fe_wt))

    def tearDown(self):
        self.sbx.close()

    # -- helpers ----------------------------------------------------------
    def gate(self, wt, *args):
        return run_hook_args(self.sbx, "prlaunch-gate.sh", ["--repo-dir", wt, *args])

    def scen_file(self, name):
        path = os.path.join(self.sbx.dir, name)
        with open(path, "w") as fh:
            fh.write("scenario 1: user sees X, PASS if Y\n")
        return path

    def ledgers(self):
        d = self.sbx.prlaunch_ok
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if f.endswith(".json"))

    def record_all(self, wt, scen):
        self.assertEqual(self.gate(wt, "record", "deep_review")[0], 0)
        self.assertEqual(self.gate(wt, "record", "cr_cli")[0], 0)
        self.assertEqual(self.gate(wt, "record", "scenarios", scen)[0], 0)
        self.assertEqual(self.gate(wt, "record", "outcome_eval")[0], 0)
        self.assertEqual(self.gate(wt, "record", "tests", "--cmd", "x")[0], 0)

    # -- the collision itself ---------------------------------------------
    def test_colliding_worktree_names_write_separate_ledgers(self):
        self.assertEqual(self.gate(self.svc_wt, "record", "deep_review")[0], 0)
        self.assertEqual(self.gate(self.fe_wt, "record", "deep_review")[0], 0)
        self.assertEqual(len(self.ledgers()), 2, self.ledgers())
        self.assertIn("api-svc--me-eng-9254-x.json", self.ledgers())
        self.assertIn("web-app--me-eng-9254-x.json", self.ledgers())

    def test_ledger_repo_field_is_the_real_repo_not_the_worktree_dir(self):
        self.assertEqual(self.gate(self.svc_wt, "record", "deep_review")[0], 0)
        path = self.sbx.ledger_path("api-svc", self.BRANCH)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["repo"], "api-svc")

    # -- the sha-INDEPENDENT hole: a foreign scenarios registration -------
    def test_foreign_scenarios_cannot_satisfy_outcome_eval(self):
        """The one gate precondition that never compares shas.

        Before the fix repo B's `record outcome_eval` returned rc=0 on repo A's
        scenarios file — a gate precondition satisfied by another repository's
        evidence, with no sha anywhere in the decision.
        """
        self.assertEqual(
            self.gate(self.svc_wt, "record", "scenarios", self.scen_file("svc.md"))[0], 0)
        rc, out, err = self.gate(self.fe_wt, "record", "outcome_eval")
        self.assertEqual(rc, 1, "frontend must NOT inherit the backend's scenarios")
        self.assertIn("no scenarios registered", out + err)

    # -- a foreign record must not destroy a passing check ----------------
    def test_foreign_record_cannot_stale_a_passing_check(self):
        self.record_all(self.svc_wt, self.scen_file("svc.md"))
        self.assertEqual(self.gate(self.svc_wt, "check")[0], 0)
        # the other repo runs its own gate on the same-named worktree
        self.assertEqual(self.gate(self.fe_wt, "record", "deep_review")[0], 0)
        rc, out, err = self.gate(self.svc_wt, "check")
        self.assertEqual(rc, 0, "svc committed nothing; its gates must still pass\n" + out + err)

    def test_foreign_record_does_not_overwrite_recorded_evidence(self):
        self.record_all(self.svc_wt, self.scen_file("svc.md"))
        self.assertEqual(self.gate(self.fe_wt, "record", "tests", "--cmd", "vitest run")[0], 0)
        with open(self.sbx.ledger_path("api-svc", self.BRANCH)) as fh:
            svc = json.load(fh)
        self.assertEqual(svc["gates"]["tests"]["cmd"], "x")
        self.assertEqual(svc["gates"]["tests"]["sha"], self.svc_head)

    # -- `path` subcommand (so callers stop reconstructing) ---------------
    def test_path_subcommand_reports_ledger_scenarios_and_repo(self):
        rc, out, err = self.gate(self.svc_wt, "path")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.strip(),
                         self.sbx.ledger_path("api-svc", self.BRANCH))
        rc, out, err = self.gate(self.svc_wt, "path", "--scenarios")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.strip().endswith(
            "api-svc--me-eng-9254-x.scenarios.md"), out)
        rc, out, err = self.gate(self.svc_wt, "path", "--repo")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.strip(), "api-svc")

    def test_path_matches_where_record_actually_writes(self):
        """The whole point of exposing `path`: it cannot drift from the writer."""
        rc, out, _ = self.gate(self.svc_wt, "path")
        declared = out.strip()
        self.assertEqual(self.gate(self.svc_wt, "record", "deep_review")[0], 0)
        self.assertTrue(os.path.isfile(declared), declared)

    # -- fallbacks ---------------------------------------------------------
    def test_repo_without_a_remote_keys_off_the_primary_clone(self):
        """No origin: fall back to the main clone's dirname, never the worktree's."""
        plain = os.path.join(self.sbx.dir, "no-remote-repo")
        make_git_repo(plain, "trunk", self.e)
        wt = os.path.join(self.sbx.dir, "elsewhere", "eng-9254")
        make_worktree(plain, wt, self.BRANCH, self.e, "plain work")
        rc, out, err = self.gate(wt, "path", "--repo")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.strip(), "no-remote-repo")

    def test_primary_clone_key_is_unchanged(self):
        """Non-worktree behaviour must not move: basename of the clone."""
        rc, out, err = self.gate(self.svc, "path", "--repo")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.strip(), "api-svc")

    # -- deliberate: SAME repo, two working copies, one ledger ------------
    def test_two_clones_of_the_same_repo_share_one_ledger_per_branch(self):
        """Keying on identity means a second working copy of the SAME repo maps
        to the SAME key -- `web-app-review`, a clone whose origin is the
        local `web-app` path, no longer gets a ledger of its own.

        That is intended, not collateral: the ledger's contract is repo+branch
        +HEAD, and two checkouts of one branch either agree on HEAD (same
        shipping bytes, one ledger is right) or they don't (STALE is the honest
        verdict -- you should not open a PR from the stale one). The pre-fix
        per-dirname separation was an artifact of the bug being fixed here.
        """
        review = os.path.join(self.sbx.dir, "api-svc-review")
        make_git_repo(review, "trunk", self.e)
        set_remote(review, self.svc, self.e)  # clone-of-a-clone: origin is a LOCAL path
        rc, out, err = self.gate(review, "path", "--repo")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.strip(), "api-svc",
                         "a second working copy is still the same repository")

    # -- defence in depth: never overwrite a foreign ledger ---------------
    def test_record_aborts_when_the_ledger_belongs_to_another_repo(self):
        path = self.sbx.ledger_path("api-svc", self.BRANCH)
        os.makedirs(self.sbx.prlaunch_ok, exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"repo": "someone-else", "branch": self.BRANCH,
                       "gates": {"deep_review": {"sha": "dead", "ts": "t"}}}, fh)
        rc, out, err = self.gate(self.svc_wt, "record", "cr_cli")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("someone-else", out + err)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["repo"], "someone-else",
                             "the foreign ledger must be left untouched")

    # -- legacy ledgers are not adopted, but they ARE explained -----------
    def test_check_names_a_legacy_worktree_keyed_ledger(self):
        legacy = self.sbx.ledger_path("eng-9254", self.BRANCH)  # the pre-fix key
        os.makedirs(self.sbx.prlaunch_ok, exist_ok=True)
        with open(legacy, "w") as fh:
            json.dump({"repo": "eng-9254", "branch": self.BRANCH, "gates": {
                g: {"sha": self.svc_head, "ts": "t"}
                for g in ("deep_review", "cr_cli", "outcome_eval", "tests")}}, fh)
        rc, out, err = self.gate(self.svc_wt, "check")
        self.assertEqual(rc, 1, "a worktree-dirname-keyed ledger must NOT satisfy check")
        self.assertIn(os.path.basename(legacy), out + err)


if __name__ == "__main__":
    unittest.main()


class PublishCrCliSubcommandTest(unittest.TestCase):
    """`publish-cr-cli` republishes the recorded cr_cli attestation.

    /PRlaunch records every gate in phases 1-4, on the LOCAL branch, and pushes
    in phase 5. GitHub's Statuses API 422s on a sha it has never seen, so the
    inline publish inside `record cr_cli` no-ops in the standard flow and the
    attestation essentially never reaches the PR. This subcommand is what phase
    5 calls after the push.

    It republishes, never re-records: it cannot launder a stale gate into a
    fresh one, and it refuses outright when the ledger's sha is not HEAD.
    """

    def setUp(self):
        self.sbx = HookSandbox()
        self.repo = os.path.join(self.sbx.dir, "myrepo")
        self.branch = "me/eng-42-x"
        self.head = make_git_repo(self.repo, self.branch, self.sbx.env())
        add_remote(self.repo, self.sbx.env())

    def tearDown(self):
        self.sbx.close()

    def _stub(self, fail=False):
        binp = os.path.join(self.sbx.dir, "ghbin")
        os.makedirs(binp, exist_ok=True)
        shim = os.path.join(binp, "gh")
        with open(shim, "w") as fh:
            fh.write(GH_STUB)
        os.chmod(shim, 0o755)
        self.capture = os.path.join(self.sbx.dir, "gh-capture.txt")
        env = {"GH_CAPTURE_FILE": self.capture,
               "PATH": binp + os.pathsep + os.environ.get("PATH", "")}
        if fail:
            env["GH_STUB_FAIL"] = "1"
        return env

    def gate(self, *args, extra_env=None):
        env = {"PRLAUNCH_PUBLISH_CR_STATUS": "1"}  # opt in; see the class above
        env.update(extra_env or {})
        return run_hook_args(self.sbx, "prlaunch-gate.sh",
                             ["--repo-dir", self.repo, *args], extra_env=env)

    def calls(self):
        return parse_gh_calls(self.capture)

    def test_republishes_the_same_status_record_produced(self):
        """The whole point: the post-push republish must be byte-identical to
        what the pre-push record tried and failed to send."""
        env = self._stub()
        self.gate("record", "cr_cli", "--findings", '[{"severity":"minor"}]',
                  "--seat", "seat-c", "--run-id", "r-7", extra_env=env)
        recorded = self.calls()[-1]
        rc, out, err = self.gate("publish-cr-cli", extra_env=env)
        self.assertEqual(rc, 0, out + err)
        republished = self.calls()[-1]
        self.assertEqual(republished["endpoint"], recorded["endpoint"])
        self.assertEqual(republished["fields"], recorded["fields"])
        self.assertEqual(republished["fields"]["state"], "success")
        self.assertIn("seat=seat-c", republished["fields"]["description"])
        self.assertIn("run=r-7", republished["fields"]["description"])

    def test_a_skipped_entry_republishes_pending_never_success(self):
        env = self._stub()
        self.gate("record", "cr_cli", "--skipped", "rate limit", extra_env=env)
        rc, out, err = self.gate("publish-cr-cli", extra_env=env)
        self.assertEqual(rc, 0, out + err)
        call = self.calls()[-1]
        self.assertEqual(call["fields"]["state"], "pending")
        self.assertIn("verdict=skipped", call["fields"]["description"])
        self.assertIn("rate limit", call["fields"]["description"])

    def test_refuses_when_the_ledger_sha_is_not_head(self):
        """A commit landed after the gate ran. Republishing would attest a
        review that never saw these bytes."""
        env = self._stub()
        self.gate("record", "cr_cli", extra_env=env)
        before = len(self.calls())
        new_head = add_commit(self.repo, self.sbx.env())
        rc, out, err = self.gate("publish-cr-cli", extra_env=env)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.calls()), before, "must not post on a stale gate")
        self.assertIn(new_head[:8], out + err)

    def test_refuses_when_no_cr_cli_gate_is_recorded(self):
        env = self._stub()
        rc, out, err = self.gate("publish-cr-cli", extra_env=env)
        self.assertNotEqual(rc, 0)
        self.assertIn("cr_cli", out + err)
        self.assertEqual(self.calls(), [])

    def test_never_writes_the_bare_review_gate_context(self):
        env = self._stub()
        self.gate("record", "cr_cli", extra_env=env)
        self.gate("publish-cr-cli", extra_env=env)
        calls = self.calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["fields"].get("context"), "review-gate/cr-cli")

    def test_is_idempotent(self):
        env = self._stub()
        self.gate("record", "cr_cli", extra_env=env)
        self.assertEqual(self.gate("publish-cr-cli", extra_env=env)[0], 0)
        self.assertEqual(self.gate("publish-cr-cli", extra_env=env)[0], 0)
        last_two = self.calls()[-2:]
        self.assertEqual(last_two[0], last_two[1])

    def test_does_not_re_record_or_refresh_the_ledger_entry(self):
        """Republish must not restamp the gate — that would let a late publish
        silently revalidate a gate the re-gate rule had staled."""
        env = self._stub()
        self.gate("record", "cr_cli", extra_env=env)
        # ask the gate rather than rebuilding from the dirname —
        # this fixture sets an origin remote, so identity != dirname.
        rc, path_out, _ = self.gate("path")
        assert rc == 0, path_out
        path = path_out.strip()
        with open(path) as fh:
            before = json.load(fh)["gates"]["cr_cli"]
        self.gate("publish-cr-cli", extra_env=env)
        with open(path) as fh:
            after = json.load(fh)["gates"]["cr_cli"]
        self.assertEqual(before, after)

    def test_a_publish_failure_is_reported_but_not_fatal(self):
        env = self._stub()
        self.gate("record", "cr_cli", extra_env=env)
        env["GH_STUB_FAIL"] = "1"
        rc, out, err = self.gate("publish-cr-cli", extra_env=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("WARNING", out + err)
