#!/bin/bash
# review-gate-status.sh -- the ONE `review-gate/cr-cli` publisher.
#
# Sourced, never executed. Callers share this one function:
#   * hooks/prlaunch-gate.sh   -- `record cr_cli` / `publish-cr-cli`
#   * any other tool that records a CLI review out of band (e.g. a PR
#     babysitter that is not cd-ed into the repo)
#
# It publishes ADVISORY evidence that a CodeRabbit CLI review ran, at the exact
# sha it ran against. The context is deliberately SEPARATE from a required
# `review-gate` check: this function must never be called with, or write, that
# context, or the thing being gated would certify itself as reviewed ("two
# contexts, one authority"). If you run a review-gate workflow, it is the sole
# writer of `review-gate`; it only ever READS this one.
#
# OFF BY DEFAULT. Nothing is published -- no network call, no commit status on
# your repo -- unless PRLAUNCH_PUBLISH_CR_STATUS=1 is set in the environment.
# The callers' own ledger/store write is the source of truth either way; this
# is optional telemetry for teams whose CI reads the status.
#
# The repo is an EXPLICIT `owner/repo` slug parameter rather than gh's
# `{owner}/{repo}` placeholder resolved from the caller's cwd, because a caller
# sweeping many repos at once is not cd-ed into any of them. `rg_repo_slug`
# derives that slug for callers that do have a checkout.
#
# SHELL-OPTION SAFETY: prlaunch-gate.sh runs under `set -uo pipefail`; other
# callers may run under `set -euo pipefail`. Every path here therefore
# avoids bare `cond && action` statement chains (which return 1 when the
# condition is false and would kill a `set -e` caller) and never lets a failing
# command substitution escape into an assignment. The function always returns 0:
# publishing is best-effort telemetry layered on top of a ledger/store write
# that has ALREADY happened and is the real source of truth.

RG_STATUS_CONTEXT="review-gate/cr-cli"
# GitHub's Statuses API hard-rejects a description longer than 140 chars.
RG_MAX_DESCRIPTION=140

# rg_repo_slug <dir> -> prints `owner/name`, or nothing if it can't be resolved.
#
# Handles the three remote shapes git hands out: https://host/owner/name[.git],
# scp-style git@host:owner/name[.git], and ssh://git@host[:port]/owner/name.
# Prints nothing (rather than guessing) when there is no origin, or when the URL
# has no owner segment -- a bogus slug would post a status to a repo that isn't
# ours, which is worse than not posting one.
rg_repo_slug() {
  local dir="${1:-}" url name rest owner
  [ -n "$dir" ] || return 0
  if ! url=$(git -C "$dir" remote get-url origin 2>/dev/null); then
    return 0
  fi
  [ -n "$url" ] || return 0

  # A filesystem path is a legitimate git remote and has no owner/repo slug at
  # all -- `/srv/git/bare-repo.git` would otherwise parse to `git/bare-repo`
  # and send a status to a stranger's repo. Only URL-shaped remotes qualify.
  case "$url" in
    /*|./*|../*|~*|file://*) return 0 ;;
  esac

  url="${url%.git}"
  url="${url%/}"
  # Drop any scheme/host/port prefix: everything up to the last colon goes.
  # https://github.com/O/N -> //github.com/O/N ; git@github.com:O/N -> O/N
  url="${url##*:}"

  name="${url##*/}"
  rest="${url%/*}"
  # No slash at all means there was no owner segment to take.
  [ "$rest" != "$url" ] || return 0
  owner="${rest##*/}"
  [ -n "$owner" ] || return 0
  [ -n "$name" ] || return 0
  printf '%s/%s' "$owner" "$name"
}

# rg_publish_cr_cli_status <state> <description> <sha> <owner/repo>
#
# Always returns 0. Every failure mode -- no slug, no `gh`, no token, no
# network, an API rejection -- warns on stderr and returns success, because the
# caller's own durable record is what actually gates anything.
rg_publish_cr_cli_status() {
  local state="${1:-}" desc="${2:-}" sha="${3:-}" slug="${4:-}"
  local runner rc out_err resolved

  # Opt-in: silent no-op unless the operator asked for statuses.
  if [ "${PRLAUNCH_PUBLISH_CR_STATUS:-0}" != "1" ]; then
    return 0
  fi

  if [ ${#desc} -gt "$RG_MAX_DESCRIPTION" ]; then
    desc="${desc:0:$((RG_MAX_DESCRIPTION - 3))}..."
  fi

  if [ -z "$slug" ]; then
    printf 'review-gate-status: %s publish skipped -- no owner/repo slug (local-only repo, or no origin remote)\n' \
      "$RG_STATUS_CONTEXT" >&2
    return 0
  fi
  if [ -z "$sha" ]; then
    printf 'review-gate-status: %s publish skipped -- no target sha\n' "$RG_STATUS_CONTEXT" >&2
    return 0
  fi
  if ! command -v gh >/dev/null 2>&1; then
    printf 'review-gate-status: %s publish skipped -- gh not on PATH\n' "$RG_STATUS_CONTEXT" >&2
    return 0
  fi

  runner=(gh)
  if command -v timeout >/dev/null 2>&1; then
    runner=(timeout 15 gh)
  fi

  # GitHub's Statuses API hard-requires a full 40-char hex object ID. A caller
  # that only kept `git rev-parse --short` output (7-9 chars) would be rejected
  # outright. Resolve here, at the publish boundary, so every caller is
  # covered.
  if [[ ${#sha} -ne 40 || ! "$sha" =~ ^[0-9a-fA-F]+$ ]]; then
    resolved=$("${runner[@]}" api "repos/$slug/commits/$sha" -q .sha 2>/dev/null) || resolved=""
    if [[ -n "$resolved" && "$resolved" =~ ^[0-9a-fA-F]{40}$ ]]; then
      sha="$resolved"
    else
      printf 'review-gate-status: %s publish skipped -- sha %s could not be resolved to a full OID\n' \
        "$RG_STATUS_CONTEXT" "$sha" >&2
      return 0
    fi
  fi

  if out_err=$("${runner[@]}" api "repos/$slug/statuses/$sha" \
      -f state="$state" \
      -f context="$RG_STATUS_CONTEXT" \
      -f description="$desc" \
      2>&1 >/dev/null); then
    rc=0
  else
    rc=$?
  fi

  if [ "$rc" -eq 0 ]; then
    printf 'review-gate-status: published %s=%s @ %s (%s)\n' \
      "$RG_STATUS_CONTEXT" "$state" "${sha:0:8}" "$slug" >&2
  else
    printf 'review-gate-status: WARNING -- failed to publish %s status (rc=%s) -- the local record is still the source of truth\n' \
      "$RG_STATUS_CONTEXT" "$rc" >&2
    if [ -n "$out_err" ]; then
      printf '%s\n' "$out_err" >&2
    fi
  fi
  return 0
}
