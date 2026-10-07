#!/bin/bash
# prlaunch-gate.sh -- the PRlaunch per-gate evidence ledger.
#
# The ledger lives at:
#   ~/.claude/prlaunch-ok/<repo>--<branch-slug>.json   (branch '/' -> '-')
# where <repo> is the repository's IDENTITY (origin remote basename, else the
# main clone's dirname) -- NOT the worktree directory name, which two repos can
# share. Ask for the path with `path`; never rebuild it.
# and records, per gate, the HEAD sha it ran against + a timestamp, so the
# pr-gate hook can mechanically prove all four PRlaunch quality gates ran on the
# EXACT bytes being shipped. Any commit after a gate ran changes HEAD and stales
# that gate's entry -- the re-gate rule, enforced as code instead of prose.
#
# Run from inside the target repo (it resolves repo/branch/HEAD itself), or pass
# --repo-dir <dir> anywhere on the command line.
#
# Subcommands:
#   record scenarios <path>
#       Register the outcome-eval scenario file BEFORE the eval runs.
#       Stores {path, sha256 of the file, ts}. Precondition for outcome_eval.
#   record deep_review|cr_cli|outcome_eval|tests [--skipped R] [--na R] [--cmd C]
#                                         [--findings JSON|@file] [--seat S] [--run-id R]
#       Stamp {sha: current HEAD, ts} for that gate. Rules:
#         * outcome_eval is REFUSED unless scenarios were registered first AND
#           the registered file still hashes to the sha256 stored at
#           registration -- editing or deleting the scenarios after the fact is
#           drift and is refused. The verified hash is stamped onto the entry as
#           scenarios_sha256. UNLESS --na is given (no user-facing surface needs
#           no scenarios).
#         * --skipped is only valid for cr_cli and requires a reason.
#         * --na is only valid for outcome_eval and requires a reason.
#         * --cmd records the verify command (used for tests).
#         * --findings (deep_review / cr_cli only) records the gate's WHOLE
#           finding set -- a JSON array, or @path to a file holding one; every
#           element needs a `severity`. Stored with a per-severity histogram,
#           and mirrored as a `gate_findings` row via ledger-append.sh.
#         * --seat S / --run-id R (both optional, cr_cli only) -- identifies
#           which CR seat and which cr-review.sh run produced the attestation.
#           Surfaced by cr-review.sh's stderr log lines. Persisted
#           into the gate's ledger entry (source of truth) AND into the
#           advisory GitHub status description below -- the ledger copy must
#           survive even when the status publish is skipped or fails.
#       Also APPENDS {gate, sha, ts, ...same conditional fields} to a top-level
#       `history` array. `.gates[gate]` stays the CURRENT-state view
#       (latest stamp only, unchanged shape -- every existing reader is
#       unaffected); `.history` is the append-only log that survives re-stamps,
#       so which trees a gate ever passed on can still be reconstructed after
#       branch reuse. Backfill for ledgers written before this change is
#       impossible -- their prior stamps were already overwritten.
#
#       `record cr_cli` ALSO publishes a GitHub commit status, context `review-gate/cr-cli`,
#       at the exact sha just recorded ($head, same invocation -- never a stale
#       re-read of the ledger, since a later record overwrites it).
#       This is the ADVISORY attestation only: it NEVER writes the required
#       `review-gate` context -- if you run a review-gate workflow, that
#       workflow is its sole writer ("two contexts, one authority"). A clean record
#       publishes `success`; a `--skipped` record (incl. exit-75 seat
#       exhaustion) publishes `pending` -- never `success`, because an
#       authorised skip is not a review, and the classic Statuses API has no
#       `neutral`. Publishing is best-effort: no `gh`, no token, no network, or
#       a local-only repo with no remote all no-op with a stderr warning --
#       none of them may break the local ledger write, which is the source of
#       truth and has already happened by the time this runs.
#
#       OFF BY DEFAULT: nothing is published unless PRLAUNCH_PUBLISH_CR_STATUS=1
#       is set (see hooks/review-gate-status.sh). Unset, the ledger write is
#       the whole effect -- no network call, no commit status on your repo.
#   path [--scenarios|--repo]
#       Print the resolved ledger path (or the scenarios sidecar path, or the
#       repo key). Callers must use this instead of rebuilding the path.
#   publish-cr-cli
#       Republish the ALREADY-RECORDED cr_cli attestation at the current HEAD.
#       /PRlaunch records gates in phases 1-4 on the local branch
#       and pushes in phase 5, but the Statuses API 422s on a sha GitHub has
#       never seen -- so `record cr_cli`'s inline publish no-ops in the standard
#       flow and the attestation never reaches the PR. Phase 5 calls this right
#       after `git push`. It republishes and never re-records, so it cannot
#       revalidate a gate the re-gate rule staled; if the recorded sha is not
#       HEAD it refuses instead of attesting bytes the review never saw.
#       Same opt-in as above: without PRLAUNCH_PUBLISH_CR_STATUS=1 it validates
#       the entry, says publishing is off, and exits 0.
#   check
#       Exit 0 iff all four gate entries exist (skipped/na entries pass on
#       reason presence) AND every recorded gate sha == current HEAD. On failure
#       prints exactly which gate is missing/stale + the prescriptive fix, and
#       exits 1. On success also prints a one-line history summary (gate=count
#       per gate), when the ledger has any `history` entries -- ledgers written
#       before this change (or a legacy plain-sha marker) have none, so the line
#       is omitted rather than printed empty.

set -uo pipefail

# The `review-gate/cr-cli` publisher lives in its own hooks/ file so any other
# caller (e.g. a PR babysitter) can call the SAME function instead of forking a
# second one.
# Resolved relative to this script's REAL location: the test harness symlinks
# hooks into a throwaway HOME rather than copying the tree, and
# `readlink -f` is not portable to every macOS in the fleet.
_pg_self="${BASH_SOURCE[0]}"
while [[ -L "$_pg_self" ]]; do
  _pg_link=$(readlink "$_pg_self")
  case "$_pg_link" in
    /*) _pg_self="$_pg_link" ;;
    *)  _pg_self="$(dirname "$_pg_self")/$_pg_link" ;;
  esac
done
_pg_hooks_dir="$(cd "$(dirname "$_pg_self")" && pwd)"
_pg_lib="$_pg_hooks_dir/review-gate-status.sh"
if [[ -r "$_pg_lib" ]]; then
  # shellcheck source=review-gate-status.sh
  source "$_pg_lib"
else
  # Degrade explicitly. This script has no `set -e`, so a missing lib does not
  # actually break it -- measured: `record cr_cli` still exits 0 and `check`
  # still passes, they just emit two bare `rg_repo_slug: command not found`
  # lines. That is the problem: a silent-ish failure that reads like noise.
  # One honest skip line instead, and the behaviour stays pinned if anyone
  # ever adds `set -e` here (a `set -e` caller of the same lib dies outright
  # on the missing file).
  rg_repo_slug() { :; }
  rg_publish_cr_cli_status() {
    printf 'prlaunch-gate: review-gate/cr-cli publish skipped -- %s not readable\n' "$_pg_lib" >&2
    return 0
  }
fi

GATES="deep_review cr_cli outcome_eval tests"

die() { printf 'prlaunch-gate: %s\n' "$1" >&2; exit 1; }

phase_of() {
  case "$1" in
    deep_review)  printf '1' ;;
    cr_cli)       printf '2' ;;
    outcome_eval) printf '3' ;;
    tests)        printf '4' ;;
    *)            printf '?' ;;
  esac
}

is_gate() {
  case " $GATES " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

# ---- pull --repo-dir out of the args (it may appear anywhere) --------------
repo_dir=""
rest=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir)
      [[ $# -ge 2 ]] || die "--repo-dir requires a directory"
      repo_dir="$2"; shift 2 ;;
    *) rest+=("$1"); shift ;;
  esac
done
if [[ ${#rest[@]} -gt 0 ]]; then set -- "${rest[@]}"; else set --; fi

subcmd="${1:-}"
[[ -n "$subcmd" ]] || die "usage: prlaunch-gate.sh record <gate> [...] | publish-cr-cli | check | path [--scenarios|--repo]  (see header)"

# ---- resolve repo IDENTITY / branch / HEAD --------------------------------
# The ledger key must identify the REPOSITORY. It used to be
# `basename "$(git rev-parse --show-toplevel)"`, but inside a worktree
# --show-toplevel returns the WORKTREE directory -- so two different repos
# whose worktrees were both named after the ticket collapsed onto ONE ledger:
#   .../api-worktrees/eng-8804  -> key "eng-8804"
#   .../web-worktrees/eng-8804  -> key "eng-8804"
# One repo's `record` then overwrote the other's gate evidence, and the
# `scenarios` precondition -- the one gate check that compares no shas -- could
# be satisfied by the other repository's registration entirely (we hit this
# twice before keying on identity).
#
# Resolve it in order of decreasing authority:
#   1. the origin remote's basename       -- stable across worktrees AND clone paths
#   2. the MAIN clone's dirname (--git-common-dir) -- for remote-less repos
#   3. the worktree dirname               -- last resort (the old behaviour)
sanitize_key() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '-'; }

resolve_repo_key() {
  local url common name
  url=$(git -C "$repo_dir" config --get remote.origin.url 2>/dev/null)
  if [[ -n "$url" ]]; then
    name="${url%/}"      # trailing slash
    name="${name##*:}"   # scp-style host: / scheme:
    name="${name##*/}"
    name="${name%.git}"
    if [[ -n "$name" ]]; then sanitize_key "$name"; return; fi
  fi
  common=$(git -C "$repo_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
  if [[ -n "$common" ]]; then
    common="${common%/}"
    common="${common%/.git}"
    name=$(basename "$common")
    if [[ -n "$name" && "$name" != "." && "$name" != "/" ]]; then
      sanitize_key "$name"; return
    fi
  fi
  sanitize_key "$(basename "$toplevel")"
}

[[ -n "$repo_dir" ]] || repo_dir="$(pwd)"
repo_dir="${repo_dir/#\~/$HOME}"
toplevel=$(git -C "$repo_dir" rev-parse --show-toplevel 2>/dev/null) \
  || die "not a git repo: $repo_dir (run inside the repo, or pass --repo-dir <dir>)"
repo=$(resolve_repo_key)
branch=$(git -C "$repo_dir" branch --show-current 2>/dev/null)
head=$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null) \
  || die "no commits in $repo_dir"
[[ -n "$branch" ]] || die "detached HEAD in $repo_dir -- checkout a branch first"
slug="${branch//\//-}"
okdir="$HOME/.claude/prlaunch-ok"
ledger="$okdir/${repo}--${slug}.json"
scenarios_sidecar="$okdir/${repo}--${slug}.scenarios.md"
# Where a pre-identity-key run would have written. NOT read as evidence -- its
# provenance is exactly what was ambiguous -- but named in `check` failures so
# a suddenly-missing gate is explainable instead of mysterious.
legacy_ledger="$okdir/$(sanitize_key "$(basename "$toplevel")")--${slug}.json"
short="${head:0:8}"

# A ledger that already names a DIFFERENT repo/branch is not ours to overwrite.
# Defence in depth: with the key above a collision needs a second key-derivation
# bug, but "the ledger lies quietly" is the failure this file exists to prevent.
# (Branch is checked too: the slug collapses '/' to '-', so `me/eng-1-x` and
# `me-eng-1-x` still share a filename.)
assert_ledger_identity() {
  [[ -f "$ledger" ]] || return 0
  local lrepo lbranch
  lrepo=$(jq -r '.repo // ""' "$ledger" 2>/dev/null)
  lbranch=$(jq -r '.branch // ""' "$ledger" 2>/dev/null)
  [[ -z "$lrepo" || "$lrepo" == "$repo" ]] \
    || die "ledger identity mismatch -- $ledger records repo '$lrepo' but this call resolved '$repo'. Refusing to overwrite another repo's gate evidence. If '$lrepo' is stale, delete that file and re-run the gates."
  [[ -z "$lbranch" || "$lbranch" == "$branch" ]] \
    || die "ledger identity mismatch -- $ledger records branch '$lbranch' but this call resolved '$branch'. Refusing to overwrite another branch's gate evidence."
}

ledger_read() { if [[ -f "$ledger" ]]; then cat "$ledger"; else printf '{}'; fi; }

atomic_write() {
  mkdir -p "$okdir"
  local tmp
  tmp=$(mktemp "${ledger}.XXXXXX") || die "mktemp failed in $okdir"
  if printf '%s\n' "$1" >"$tmp" && mv -f "$tmp" "$ledger"; then :; else
    rm -f "$tmp"; die "failed writing ledger $ledger"
  fi
}

file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---- review-gate/cr-cli commit-status publish ------------------------------
# The publisher itself lives in hooks/review-gate-status.sh so other callers
# can use the same function. This script's only remaining job is to
# hand it the repo slug for the checkout it is already sitting in.
publish_review_gate_status() {
  rg_publish_cr_cli_status "$1" "$2" "$3" "$(rg_repo_slug "$toplevel")"
}

# cr_cli_status_from <skipped> <seat> <run_id> <findings-json> <has_skipped> <has_findings>
# Sets $rg_state / $rg_desc. The ONE place the cr_cli attestation's wording is
# decided, so `record` (pre-push) and `publish-cr-cli` (post-push) cannot
# drift into describing the same review two different ways.
#
# Cap seat/run to short identifiers so the FIXED part of the description
# (verdict/seat/run/findings) is always bounded. Only `reason` -- the one
# free-form, caller-supplied field -- is allowed to be long, and it goes LAST so
# it is the only thing that can get truncated (seen live in an outcome eval: a
# verbose skip reason ate the whole 140-char budget and silently dropped
# seat/run/findings when reason was built first).
cr_cli_status_from() {
  local _skipped="$1" _seat="$2" _run="$3" _findings="$4"
  local _has_skipped="$5" _has_findings="$6"
  local seat_disp="n/a" run_disp="n/a" findings_disp="n/a"
  [[ -n "$_seat" ]] && seat_disp="${_seat:0:40}"
  [[ -n "$_run"  ]] && run_disp="${_run:0:40}"
  if [[ "$_has_findings" == "1" && -n "$_findings" ]]; then
    findings_disp=$(printf '%s' "$_findings" | jq -r 'length')
  fi
  if [[ "$_has_skipped" == "1" ]]; then
    # An authorised skip (incl. cr-review.sh exit 75) is not a review --
    # `pending` honestly means "no attestation earned" and correctly leaves the
    # required review-gate context yellow. Never `success`.
    rg_state="pending"
    rg_desc="verdict=skipped seat=${seat_disp} run=${run_disp} findings=${findings_disp} reason=${_skipped}"
  else
    rg_state="success"
    rg_desc="verdict=reviewed findings=${findings_disp} seat=${seat_disp} run=${run_disp}"
  fi
}

# ---------------------------------------------------------------------------
case "$subcmd" in
  record)
    assert_ledger_identity
    gate="${2:-}"
    [[ -n "$gate" ]] || die "usage: prlaunch-gate.sh record <scenarios|deep_review|cr_cli|outcome_eval|tests> [...]"

    if [[ "$gate" == "scenarios" ]]; then
      path="${3:-}"
      [[ -n "$path" ]] || die "usage: prlaunch-gate.sh record scenarios <path>"
      path="${path/#\~/$HOME}"
      [[ -f "$path" ]] || die "scenarios file not found: $path"
      sha=$(file_sha256 "$path")
      ts=$(now)
      updated=$(ledger_read | jq \
        --arg repo "$repo" --arg branch "$branch" \
        --arg path "$path" --arg sha "$sha" --arg ts "$ts" \
        '.repo=$repo | .branch=$branch | .scenarios={path:$path, sha256:$sha, ts:$ts}') \
        || die "jq failed building the scenarios entry"
      atomic_write "$updated"
      printf 'prlaunch-gate: registered scenarios %s for %s/%s\n' "$path" "$repo" "$branch"
      exit 0
    fi

    is_gate "$gate" \
      || die "unknown gate '$gate' (scenarios|deep_review|cr_cli|outcome_eval|tests)"

    # ---- parse the gate flags ---------------------------------------------
    skipped="" ; na="" ; cmd="" ; findings="" ; scen_sha="" ; seat="" ; run_id=""
    have_skipped=0 ; have_na=0 ; have_findings=0 ; have_seat=0 ; have_run_id=0
    shift 2
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --skipped)
          [[ $# -ge 2 ]] || die "--skipped requires a reason"
          have_skipped=1; skipped="$2"; shift 2 ;;
        --na)
          [[ $# -ge 2 ]] || die "--na requires a reason"
          have_na=1; na="$2"; shift 2 ;;
        --cmd)
          [[ $# -ge 2 ]] || die "--cmd requires a value"
          cmd="$2"; shift 2 ;;
        --findings)
          [[ $# -ge 2 ]] || die "--findings requires a JSON array (or @path)"
          have_findings=1; findings="$2"; shift 2 ;;
        --seat)
          [[ $# -ge 2 ]] || die "--seat requires a value"
          have_seat=1; seat="$2"; shift 2 ;;
        --run-id)
          [[ $# -ge 2 ]] || die "--run-id requires a value"
          have_run_id=1; run_id="$2"; shift 2 ;;
        *) die "unknown flag '$1' for 'record $gate'" ;;
      esac
    done

    # ---- --findings: the review gates' actual output ----------------------
    # Before this, a gate recorded only {sha, ts} and the ledger kept a single
    # CRITICAL+HIGH integer -- so every MEDIUM/LOW finding (including all
    # structural-lens findings, which default to MEDIUM) was thrown away and the
    # gate could not be graded at all. Validation is strict and fails BEFORE any
    # write: a silently-truncated finding set is exactly the bug being fixed.
    by_severity="{}"
    if [[ $have_findings -eq 1 ]]; then
      case "$gate" in
        deep_review|cr_cli) ;;
        *) die "--findings is only valid for deep_review and cr_cli (got '$gate') -- tests and outcome_eval do not produce a finding set" ;;
      esac
      if [[ "$findings" == @* ]]; then
        fpath="${findings#@}"; fpath="${fpath/#\~/$HOME}"
        [[ -f "$fpath" ]] || die "--findings file not found: $fpath"
        findings=$(cat "$fpath") || die "--findings could not read $fpath"
      fi
      printf '%s' "$findings" | jq -e 'type == "array"' >/dev/null 2>&1 \
        || die "--findings must be a JSON array of finding objects"
      printf '%s' "$findings" | jq -e 'all(.[]; type == "object" and has("severity"))' >/dev/null 2>&1 \
        || die "--findings: every element must be an object with a 'severity' (a finding without a severity cannot be graded)"
      by_severity=$(printf '%s' "$findings" \
        | jq -c '[.[].severity | ascii_upcase] | group_by(.) | map({key: .[0], value: length}) | from_entries') \
        || die "--findings: failed deriving the severity histogram"
    fi

    [[ $have_skipped -eq 0 || "$gate" == "cr_cli" ]] \
      || die "--skipped is only valid for cr_cli (got '$gate')"
    [[ $have_skipped -eq 0 || -n "$skipped" ]] \
      || die "--skipped requires a reason"
    [[ $have_na -eq 0 || "$gate" == "outcome_eval" ]] \
      || die "--na is only valid for outcome_eval (got '$gate')"
    [[ $have_na -eq 0 || -n "$na" ]] \
      || die "--na requires a reason"
    [[ $have_seat -eq 0 || "$gate" == "cr_cli" ]] \
      || die "--seat is only valid for cr_cli (got '$gate')"
    [[ $have_run_id -eq 0 || "$gate" == "cr_cli" ]] \
      || die "--run-id is only valid for cr_cli (got '$gate')"

    # Pre-registering scenarios only means anything if the file is still the one
    # that was registered: the point of the gate is that scenarios were written
    # BEFORE the eval ran. Re-hash here and refuse on drift, otherwise the
    # recorded sha256 is decorative and the eval can be graded against scenarios
    # rewritten to match whatever shipped.
    if [[ "$gate" == "outcome_eval" && $have_na -eq 0 ]]; then
      scen_path=$(ledger_read | jq -r '.scenarios.path // ""')
      [[ -n "$scen_path" ]] \
        || die "outcome_eval refused -- no scenarios registered. Run 'prlaunch-gate.sh record scenarios <path>' BEFORE the eval, or 'record outcome_eval --na \"<reason>\"' if there is no user-facing surface."
      [[ -f "$scen_path" ]] \
        || die "outcome_eval refused -- the registered scenarios file no longer exists: $scen_path. Re-register the scenarios you actually evaluated ('record scenarios <path>'), then re-run the eval."
      want_sha=$(ledger_read | jq -r '.scenarios.sha256 // ""')
      [[ -n "$want_sha" ]] \
        || die "outcome_eval refused -- the scenarios entry carries no sha256 (ledger written by an older prlaunch-gate). Re-register with 'record scenarios $scen_path'."
      scen_sha=$(file_sha256 "$scen_path")
      [[ -n "$scen_sha" ]] \
        || die "outcome_eval refused -- could not hash the registered scenarios file: $scen_path. Check that it is readable, then re-run the eval."
      [[ "$scen_sha" == "$want_sha" ]] \
        || die "outcome_eval refused -- scenarios DRIFTED since they were registered ($scen_path: registered ${want_sha:0:12}, now ${scen_sha:0:12}). Scenarios must be written BEFORE the eval runs. Either re-run the eval against the scenarios as registered, or 'record scenarios $scen_path' again and re-run the eval on the new ones."
    fi

    ts=$(now)
    updated=$(ledger_read | jq \
      --arg repo "$repo" --arg branch "$branch" --arg gate "$gate" \
      --arg sha "$head" --arg ts "$ts" \
      --arg skipped "$skipped" --arg na "$na" --arg cmd "$cmd" \
      --arg hs "$have_skipped" --arg hn "$have_na" --arg scen "$scen_sha" \
      --arg hf "$have_findings" \
      --arg seat "$seat" --arg run_id "$run_id" \
      --arg hseat "$have_seat" --arg hrun "$have_run_id" \
      --argjson fnd "${findings:-[]}" --argjson bysev "$by_severity" \
      '
      .repo = $repo
      | .branch = $branch
      | .gates = (.gates // {})
      | .gates[$gate] = (
          {sha: $sha, ts: $ts}
          + (if $hs == "1" then {skipped: $skipped} else {} end)
          + (if $hn == "1" then {na: $na} else {} end)
          + (if $cmd != "" then {cmd: $cmd} else {} end)
          + (if $scen != "" then {scenarios_sha256: $scen} else {} end)
          + (if $hf == "1" then
               {findings: $fnd, findings_count: ($fnd|length), by_severity: $bysev}
             else {} end)
          + (if $hseat == "1" then {seat: $seat} else {} end)
          + (if $hrun == "1" then {run_id: $run_id} else {} end)
        )
      | .history = ((.history // []) + [(
          {gate: $gate, sha: $sha, ts: $ts}
          + (if $hs == "1" then {skipped: $skipped} else {} end)
          + (if $hn == "1" then {na: $na} else {} end)
          + (if $cmd != "" then {cmd: $cmd} else {} end)
          + (if $hf == "1" then
               {findings: $fnd, findings_count: ($fnd|length), by_severity: $bysev}
             else {} end)
        )])
      ') || die "jq failed building the '$gate' entry"
    atomic_write "$updated"

    # review-gate/cr-cli: publish the ADVISORY commit status inline, in this
    # same invocation, against $head directly. Never the required
    # `review-gate` context -- see publish_review_gate_status's header comment.
    if [[ "$gate" == "cr_cli" ]]; then
      cr_cli_status_from \
        "$([[ $have_skipped  -eq 1 ]] && printf '%s' "$skipped"  || printf '')" \
        "$([[ $have_seat     -eq 1 ]] && printf '%s' "$seat"     || printf '')" \
        "$([[ $have_run_id   -eq 1 ]] && printf '%s' "$run_id"   || printf '')" \
        "$([[ $have_findings -eq 1 ]] && printf '%s' "$findings" || printf '')" \
        "$have_skipped" "$have_findings"
      publish_review_gate_status "$rg_state" "$rg_desc" "$head"
    fi

    # Mirror the finding set into the automation ledger so a scorecard or an
    # escape audit can query it without walking every per-branch gate file.
    if [[ $have_findings -eq 1 ]]; then
      event=$(jq -cn \
        --arg repo "$repo" --arg branch "$branch" --arg gate "$gate" \
        --arg sha "$head" --argjson fnd "$findings" --argjson bysev "$by_severity" \
        '{event:"gate_findings", skill:"prlaunch", repo:$repo, branch:$branch,
          gate:$gate, sha:$sha, findings_count:($fnd|length),
          by_severity:$bysev, findings:$fnd}') \
        || die "jq failed building the gate_findings event"
      # The gate file is the source of truth and is already written; the ledger
      # row is the queryable mirror. Warn LOUDLY but do not fail the gate -- a
      # telemetry write must never be able to block a PR.
      if ! "$HOME/.claude/hooks/ledger-append.sh" "$event" 2>/dev/null; then
        printf 'prlaunch-gate: WARNING -- findings recorded in the gate file but the automation-ledger mirror FAILED (%s findings for %s). Ledger readers will not see this unit.\n' \
          "$(printf '%s' "$findings" | jq -r 'length')" "$gate" >&2
      fi
    fi

    note=""
    [[ $have_skipped -eq 1 ]] && note=" (skipped: $skipped)"
    [[ $have_na -eq 1 ]] && note=" (n/a: $na)"
    [[ $have_findings -eq 1 ]] && note="$note ($(printf '%s' "$findings" | jq -r 'length') findings)"
    printf 'prlaunch-gate: recorded %s @ %s for %s/%s%s\n' "$gate" "$short" "$repo" "$branch" "$note"
    exit 0
    ;;

  publish-cr-cli)
    # Post-push republish of the recorded cr_cli attestation.
    #
    # WHY THIS EXISTS: /PRlaunch records every gate in phases 1-4 on the LOCAL
    # branch and pushes in phase 5. GitHub's Statuses API 422s on a sha it has
    # never seen ("No commit found for SHA"), so `record cr_cli`'s inline
    # publish no-ops in the standard flow and the attestation essentially never
    # reaches the PR -- which looks identical, from the Checks tab, to nobody
    # having run cr_cli at all. Phase 5 calls this straight after `git push`.
    #
    # It REPUBLISHES, never re-records: the ledger entry is not touched, so a
    # late publish can never revalidate a gate the re-gate rule has staled. If
    # the recorded sha is not HEAD it refuses outright rather than attesting a
    # review that never saw the shipping bytes.
    entry=$(ledger_read | jq -c '.gates.cr_cli // empty') \
      || die "jq failed reading the ledger at $ledger"
    [[ -n "$entry" ]] \
      || die "no cr_cli gate recorded for $repo/$branch -- run Phase 2 first (prlaunch-gate.sh record cr_cli ...)"

    entry_sha=$(printf '%s' "$entry" | jq -r '.sha // ""')
    [[ "$entry_sha" == "$head" ]] \
      || die "cr_cli was recorded at ${entry_sha:0:8} but HEAD is ${short} -- a commit landed after the gate ran; re-run Phase 2 and record it again rather than republishing a stale attestation"

    e_skipped=$(printf '%s' "$entry" | jq -r '.skipped // ""')
    e_seat=$(printf '%s'    "$entry" | jq -r '.seat // ""')
    e_run=$(printf '%s'     "$entry" | jq -r '.run_id // ""')
    e_findings=$(printf '%s' "$entry" | jq -c '.findings // empty')
    e_has_skipped=$(printf '%s' "$entry" | jq -r 'if has("skipped") then "1" else "0" end')
    e_has_findings=$(printf '%s' "$entry" | jq -r 'if has("findings") then "1" else "0" end')

    if [[ "${PRLAUNCH_PUBLISH_CR_STATUS:-0}" != "1" ]]; then
      printf 'prlaunch-gate: review-gate/cr-cli status publishing is off (set PRLAUNCH_PUBLISH_CR_STATUS=1 to enable) -- the ledger entry at %s is the record\n' "$short" >&2
      exit 0
    fi
    cr_cli_status_from "$e_skipped" "$e_seat" "$e_run" "$e_findings" \
      "$e_has_skipped" "$e_has_findings"
    publish_review_gate_status "$rg_state" "$rg_desc" "$head"
    exit 0 ;;

  check)
    data=$(ledger_read)
    jq -e . >/dev/null 2>&1 <<<"$data" \
      || die "ledger missing or corrupt for $repo/$branch -- re-run /PRlaunch to record the gates."
    problems=0
    for gate in $GATES; do
      phase=$(phase_of "$gate")
      info=$(jq -r --arg g "$gate" '
        (.gates[$g]) as $e
        | if $e == null then "missing"
          elif (($e|has("skipped")) and (($e.skipped // "") == "")) then "noreason:skipped"
          elif (($e|has("na")) and (($e.na // "") == "")) then "noreason:na"
          else "sha:" + ($e.sha // "")
          end' <<<"$data")
      case "$info" in
        missing)
          printf 'MISSING gate: %s -- re-run Phase %s on HEAD %s, then prlaunch-gate.sh record %s\n' \
            "$gate" "$phase" "$short" "$gate" >&2
          problems=1
          ;;
        noreason:skipped)
          printf 'INVALID gate: %s -- marked skipped with no reason. re-run Phase %s on HEAD %s, then prlaunch-gate.sh record %s --skipped "<reason>"\n' \
            "$gate" "$phase" "$short" "$gate" >&2
          problems=1
          ;;
        noreason:na)
          printf 'INVALID gate: %s -- marked n/a with no reason. re-run Phase %s on HEAD %s, then prlaunch-gate.sh record %s --na "<reason>"\n' \
            "$gate" "$phase" "$short" "$gate" >&2
          problems=1
          ;;
        sha:*)
          gsha="${info#sha:}"
          if [[ "$gsha" != "$head" ]]; then
            printf 'STALE gate: %s (recorded %s, HEAD %s) -- re-run Phase %s on HEAD %s, then prlaunch-gate.sh record %s\n' \
              "$gate" "${gsha:0:8}" "$short" "$phase" "$short" "$gate" >&2
            problems=1
          fi
          ;;
      esac
    done
    if [[ $problems -ne 0 && -f "$legacy_ledger" && "$legacy_ledger" != "$ledger" ]]; then
      printf 'NOTE: a ledger exists under the old worktree-dirname key:\n  %s\nThat key was shared by every repo whose worktree had this directory name, so its gates are not trusted as evidence for %s. Re-run the gates; this repo now keys on its own identity.\n' \
        "$legacy_ledger" "$repo" >&2
    fi
    [[ $problems -eq 0 ]] || exit 1
    printf 'prlaunch-gate: OK -- all 4 gates recorded at HEAD %s for %s/%s\n' "$short" "$repo" "$branch"
    # Surface the append-only history so `check` output visibly shows
    # this is a ledger of every stamp ever recorded, not just a latch holding the
    # latest one.
    hist_summary=$(jq -r '
      (.history // []) | group_by(.gate)
      | map("\(.[0].gate)=\(length)") | join(" ")' <<<"$data")
    if [[ -n "$hist_summary" ]]; then
      printf 'prlaunch-gate: history -- %s\n' "$hist_summary"
    fi
    exit 0
    ;;

  path)
    # Expose the resolved paths so callers (pr-gate.sh, the PRlaunch skill)
    # stop RECONSTRUCTING what this script owns -- the shared bug class behind
    # the worktree-key collision and the wrapup ledger misread. A reconstruction
    # drifts; a call cannot.
    case "${2:-}" in
      "")           printf '%s\n' "$ledger" ;;
      --scenarios)  printf '%s\n' "$scenarios_sidecar" ;;
      --repo)       printf '%s\n' "$repo" ;;
      *)            die "usage: prlaunch-gate.sh path [--scenarios|--repo]" ;;
    esac
    exit 0
    ;;

  *)
    die "unknown subcommand '$subcmd' (record|publish-cr-cli|check|path)"
    ;;
esac
