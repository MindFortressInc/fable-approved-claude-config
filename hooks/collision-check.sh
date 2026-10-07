#!/usr/bin/env bash
# collision-check.sh <TICKET> [--repo <name>] -- is another agent already
# building this ticket?
#
# A fleet of agents (/execute, /orchestrate, unattended queue drainers, cloud
# agents) can share one tracker board, one set of clones, and one branch-name
# convention derived from the ticket id. Nothing arbitrates between them. The
# failure this exists for: an agent's worktree was deleted and its branch reset
# by another agent that derived the same branch name from the same ticket; the
# losing commit survived only as an unreferenced object. This is the detection
# half of the fix -- run it before `git worktree add`.
#
# Five probes, cheapest first. Every collision found is printed; the script is
# READ-ONLY by construction -- it never removes a worktree, never deletes a
# branch, never writes to any repo.
#
#   A  Linear state   -- In Review, or a started/completed state with a linked
#                        PR or an assignee. One API call, catches it earliest.
#   B  Live worktrees -- git worktree list across every primary clone. THE
#                        PRIMARY PROBE: it is the only one that sees a branch
#                        that has been committed but never pushed.
#   C  Local branches -- an unpushed branch with no worktree holding it.
#   D  Remote branches-- work pushed from another machine.
#   E  Open PRs       -- gh pr list.
#
# Probes B/C/D match the ticket token (`dev-42` or `dev42`, any case, prefix
# from LINEAR_BRANCH_PREFIX) in the PATH **or** the BRANCH, at any prefix. Both
# matter: `~/code/app.dev123` carries it only in the path, `/tmp/app-911-cli`
# only in the branch, and real branches use `me/`, `feat/` and bare prefixes
# alike.
#
# `--repo <name>` scopes probes B and C to ONE primary clone: a worktree or
# local-branch hit in a DIFFERENT clone prints as an informational ℹ line
# instead of a collision. That is the paired-PR shape -- one ticket, one branch
# name, two repos -- where the second repo's worker cannot tell its own sibling
# from a competitor, because nothing in the hit says which it is. Probes A, D
# and E are untouched (a remote branch or an open PR is still exit 1
# anywhere), and NO flag means fail-closed. A --repo that matches no scanned
# clone does NOT quietly disarm the probe -- it drops the scope and DEGRADES,
# because a flag that silently turns the primary probe off is the fail-open
# this script exists to prevent.
#
# Exit: 0 clear · 1 collision (details on stdout) · 2 usage/config error ·
#       3 DEGRADED -- no collision found, but a probe could not run, so "clear"
#         was never actually established. The caller cannot otherwise tell
#         "checked and clear" from "could not check". Callers treat 3 as
#         UNKNOWN, never as clear.
#
# UNCONFIGURED IS DEGRADED. A probe with no config (no clone roots, no Linear
# key or team, no own-org list, no gh) did not run, and a probe that did not run
# cannot vouch for anything -- so it degrades the verdict to exit 3 exactly like
# a probe that ran and failed. The ONLY way to turn a probe off and still get
# exit 0 is an explicit COLLISION_CHECK_SKIP_* opt-out, which prints a `·`
# line saying so. A fresh install therefore answers 3 until it is configured;
# that is the point.
#
# SELF IS NOT A COLLISION. The caller is usually the agent building the ticket,
# and by the time it re-checks (the pre-worktree re-check, or a mid-build one)
# its OWN harness may already have flipped the tracker state (see
# linear-startwork.sh), created its worktree, pushed its branch and opened its
# PR. So the checker first resolves WHO IS ASKING: the git worktree containing
# $PWD (or $COLLISION_CHECK_SELF_DIR), and only when that workspace itself
# carries the ticket token. Hits that resolve to that caller print as `· self`
# and do not count. Nothing else is suppressed -- a second worktree, a
# differently-named branch, a PR on any other branch, or a remote tip that is
# NOT an ancestor of the caller's own branch all still exit 1.
#
# The probes are also weighted, because they are not equally strong evidence:
#   strong  B/C/D/E -- a workspace, a branch, or a PR that is not the caller's
#   weak    A       -- the Linear state, which the caller's own startwork sets
# A weak hit ALONE is a collision only when the caller has NOT identified itself
# as this ticket's builder. Otherwise an obedient builder halts on its own
# startwork transition, and every worker learns to read exit 1 as advisory.
#
# Config (environment; nothing has an org-specific default). Put these in the
# "env" block of ~/.claude/settings.json so every Bash call sees them:
#   COLLISION_CHECK_ROOTS        colon-separated dirs to scan. Each entry is a
#                                primary clone itself, or a directory whose
#                                immediate subdirectories are primary clones.
#                                (no default -- unset DEGRADES probes B-E)
#   LINEAR_BRANCH_PREFIX         ticket token prefix (default: dev). Accepts the
#                                ticket as PREFIX-123, prefix123, or 123.
#   LINEAR_API_KEY               Linear personal API key, OR
#   LINEAR_KEY_FILE              path to a JSON file holding .env.LINEAR_API_KEY
#   LINEAR_DEV_TEAM_ID           UUID of the team whose issues probe A queries
#                                (key + team unset DEGRADES probe A)
#   LINEAR_API_URL               tracker GraphQL endpoint (default: Linear's)
#   COLLISION_CHECK_OWN_ORGS     colon-separated GitHub owners (orgs or users)
#                                whose repos your branches can land in. Probe E
#                                skips other owners with an info line.
#                                (no default -- unset DEGRADES probe E)
#   COLLISION_CHECK_SELF_DIR     the caller's own workspace (default: $PWD). It
#                                counts as `self` only if that worktree's path or
#                                branch carries the ticket token, so running the
#                                check from an unrelated directory can never
#                                suppress anything.
#   COLLISION_CHECK_SKIP_LINEAR  non-empty: explicitly skip probe A
#   COLLISION_CHECK_SKIP_REMOTE  non-empty: explicitly skip probe D (offline)
#   COLLISION_CHECK_SKIP_PRS     non-empty: explicitly skip probe E
#   COLLISION_CHECK_RETIRED_REMOTES  colon-separated owner/name list whose 404
#                                is expected (deleted repo, dead clone). A 404
#                                from anything NOT listed here DEGRADES, because
#                                GitHub also 404s a private repo you cannot see.
#   COLLISION_CHECK_PR_CACHE_DIR private shared cache directory (default: OS
#                                temp/collision-pr-cache-<uid>). Empty disables
#                                caching. Complete listings live for 60s from
#                                fetch start; per-repo locks coalesce builders.
#                                Auth changes get a separate cache. Failures or
#                                capped lists are never cached as complete.
set -uo pipefail

TEAM="${LINEAR_DEV_TEAM_ID:-}"
API="${LINEAR_API_URL:-https://api.linear.app/graphql}"
PREFIX="${LINEAR_BRANCH_PREFIX:-dev}"

usage() {
  cat >&2 <<'USAGE'
usage: collision-check.sh <PREFIX-NNN|NNN> [--repo <name>]
       # is another agent building this ticket?
       # PREFIX is LINEAR_BRANCH_PREFIX (default: dev)

  --repo <name>  the repo THIS unit builds in -- its primary-clone directory
                 name, or its GitHub repo name. A live-worktree or local-branch
                 hit in a DIFFERENT primary clone is then downgraded from a
                 collision to an informational line: the expected shape of a
                 two-repo (paired-PR) ticket. Hits in the named repo, remote
                 branches, open PRs and the Linear state keep exit-1 semantics.
                 Omit it for the fail-closed default.
USAGE
  exit 2
}

bounded() { # bounded <secs> <cmd...>
  if command -v timeout  >/dev/null 2>&1; then timeout  "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$@"
  else shift; "$@"; fi
}

own_org() {
  local owner owners
  owner=$(printf '%s' "${1%%/*}" | tr '[:upper:]' '[:lower:]')
  owners=$(printf '%s' "${COLLISION_CHECK_OWN_ORGS:-}" | tr '[:upper:]' '[:lower:]')
  [ -n "$owners" ] || return 1
  case ":$owners:" in *":$owner:"*) return 0 ;; *) return 1 ;; esac
}

# A worker that cannot reach its repo is a separate process behind xargs, so it
# cannot call degrade() itself. It prints this tag plus the clone path / repo slug
# it failed on, and the parent folds those lines into the degrade message -- an
# anonymous "a repo could not be reached" costs a fresh manual diagnosis on
# every occurrence. `:` is illegal in a git ref AND in a GitHub owner/name, so
# no real finding can impersonate the tag.
CC_FAIL_TAG='::cc-fail'

# Probes D and E are network-bound and can run across dozens of clones, so they
# fan out via xargs -P onto these internal modes. They print raw findings; the
# parent turns each line into a report. $CC_TOKEN_RE is inherited from the parent.
case "${1:-}" in
  --_remote)  # <clone-path> -> matching remote branches
    : "${CC_TOKEN_RE:?--_remote is internal; run collision-check.sh <TICKET>}"
    # Retry, for the same reason probe E does: this probe fans ls-remote across
    # every clone, so with no retry a transient blip on ANY ONE of them voids the
    # whole verdict. 3 tries / 1s / 2s backoff, same as probe E.
    #
    # Fail-closed is preserved and MUST stay that way: a remote that fails all
    # three attempts still exits 1 and still degrades the run. This buys back
    # the flakes, never the genuinely unreachable. There is deliberately no
    # allowlist here -- an operator-declared "retired" remote would ALSO
    # silence a repo that is merely down.
    _refs=""; _ok=0
    for _try in 1 2 3; do
      if _refs=$(GIT_TERMINAL_PROMPT=0 bounded 15 git -C "$2" ls-remote --heads origin 2>/dev/null); then
        _ok=1; break
      fi
      [ "$_try" -lt 3 ] && sleep "$_try"   # 1s, then 2s
    done
    if [ "$_ok" -ne 1 ]; then
      printf '%s\t%s\n' "$CC_FAIL_TAG" "$2"   # name the clone
      exit 1   # timeout / auth failure / dead remote -- NOT "no match"
    fi
    # <ref>\t<clone-path>\t<sha>. The sha is what lets the parent tell the
    # caller's OWN pushed branch from another agent's commit sitting on the same
    # branch name, and the full clone path -- not its basename -- is what lets
    # the parent resolve that clone's origin URL.
    # ONE grep over the whole stream, not one fork per ref: a clone with
    # thousands of heads would otherwise pay thousands of forks per run. The
    # match is scoped to the REF column: the prefix is configurable and may be
    # spelled entirely in hex letters (`abc`, `fe`), so an unscoped match could
    # find the token inside the sha.
    _tab=$'\t'
    printf '%s\n' "$_refs" | grep -E "^[^${_tab}]*${_tab}[^${_tab}]*${CC_TOKEN_RE}" \
      | while IFS=$'\t' read -r _sha _ref; do
          case "$_ref" in refs/heads/*) _ref="${_ref#refs/heads/}" ;; *) continue ;; esac
          printf '%s\t%s\t%s\n' "$_ref" "$2" "$_sha"
        done
    exit 0 ;;
  --_pr)      # <owner/name> -> matching open PRs (cache shared across tickets)
    : "${CC_NUM:?--_pr is internal; run collision-check.sh <TICKET>}"
    _script="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    _prs=$(python3 "$(dirname "$_script")/collision-pr-cache.py" "$_script" "$2")
    _rc=$?
    # Partial listings can prove a collision but can never prove absence.
    if [ "$_rc" -ne 0 ] && { [ "$_rc" -ne 4 ] || ! grep -qE "$CC_TOKEN_RE" <<<"$_prs"; }; then
      printf '%s\t%s\n' "$CC_FAIL_TAG" "$2"
      exit 1
    fi
    printf '%s\n' "$_prs" | grep -E "$CC_TOKEN_RE" \
      | while IFS= read -r p; do
          printf '%s\t%s\t%s\n' "$2" "${p##*$'\t'}" "$(printf '%s' "$p" | tr '\t' ' ')"
        done
    exit 0 ;;
  --_pr-list) # <owner/name> -> complete unfiltered rows; exit 4 if capped
    : "${CC_NUM:?--_pr is internal; run collision-check.sh <TICKET>}"
    # Deliberately NOT `--search dev-NNN`: that is hyphen-sensitive (it misses a
    # `dev123` PR -- the exact bug class this whole check exists to kill) and it
    # does not reach head branch names without a `head:` qualifier. List the
    # open PRs and apply the same token regex every other probe uses, over the
    # number, the title, AND the branch.
    # --limit is gh's pagination bound, not a page size: a repo with more open
    # PRs than the bound is SILENTLY truncated, and a truncated probe that says
    # "no PR" is the failure mode this whole script exists to prevent. It is set
    # far above any normal repo, and hitting it marks the listing incomplete.
    #
    # Do not "optimize" this to `gh api repos/X/pulls`: REST cannot select
    # fields, so it returns FULL PR objects and --paginate moves megabytes. On a
    # very large public repo REST times out at 30s where this 3-field GraphQL
    # call returns in seconds, and timeouts are a DEGRADED storm.
    #
    # Three outcomes, which must stay distinct:
    #   flaked   -> retry; a blip on 1 of N repos must not void the whole run
    #   retired  -> 404 from a DECLARED-retired remote: expected, stay silent
    #   failed   -> exit 1; could not look (incl. any undeclared 404) -> DEGRADES
    # Collapsing "flaked" into "failed" makes clean tickets report UNKNOWN often
    # enough that the gate gets trained away.
    #
    # mktemp (O_EXCL, unpredictable name), NOT a "$$-$slug" path: that is
    # predictable, so a local attacker can pre-plant a symlink there and the
    # 2>"$_err" redirect below would truncate whatever it points at.
    # If mktemp itself fails (full /tmp), fall back to /dev/null: that only
    # costs the 404 short-circuit, so 404s get retried and then DEGRADE --
    # i.e. it degrades fail-CLOSED, never fail-open.
    _err=$(mktemp "${TMPDIR:-/tmp}/cc-pr-XXXXXX" 2>/dev/null) || _err=/dev/null
    # Cleanup is a trap, not a line after the loop: the undeclared-404 branch
    # below exits from INSIDE the loop, and a trailing `rm` would leak one temp
    # file per gate run there. EXIT only: trapping INT/TERM would move the exit
    # codes this probe's callers switch on. The /dev/null fallback is never
    # removed.
    trap '[ "$_err" = /dev/null ] || rm -f "$_err"' EXIT
    _prs=""; _ok=0; _complete=1
    for _try in 1 2 3; do
      if _prs=$(bounded 30 gh pr list --repo "$2" --state open --limit 1000 \
            --json number,title,headRefName \
            --jq '.[] | "\(.number)\t\(.title)\t\(.headRefName)"' 2>"$_err"); then
        _ok=1
        [ "$(printf '%s\n' "$_prs" | grep -c '^[0-9]')" -ge 1000 ] && _complete=0
        break
      fi
      # A 404 is AMBIGUOUS: the repo may be deleted, or it may be private and
      # this token simply cannot see it. Those need opposite verdicts, and
      # nothing in the response distinguishes them -- so the operator declares
      # which remotes are retired, and everything else fails CLOSED. Retrying a
      # 404 is pointless either way, so both branches settle now.
      if [ "$_err" != /dev/null ] && grep -qE 'HTTP 404|Not Found' "$_err" 2>/dev/null; then
        case ":${COLLISION_CHECK_RETIRED_REMOTES:-}:" in
          *":$2:"*) _ok=1; _prs=""; break ;;   # declared retired -- expected, not failed
        esac
        printf '%s\t%s\n' "$CC_FAIL_TAG" "$2"  # name the repo
        exit 1                                  # undeclared 404 -- DEGRADE
      fi
      # GitHub's SECONDARY rate limit is a distinct failure from quota
      # exhaustion: `gh api rate_limit` can show GraphQL headroom while
      # `gh pr list` still fails with this string, because it throttles request
      # RATE, not COUNT. Retrying into it burns the 3-try budget on a limiter
      # that does not clear in seconds. For own-org repos (the only ones whose
      # coverage matters here) fall back to bounded REST on the separate core
      # quota; anyone else keeps retry-then-degrade.
      if [ "$_err" != /dev/null ] && grep -qiE 'rate limit already exceeded|secondary rate limit' "$_err" 2>/dev/null; then
        if own_org "$2"; then
            # One-shot: whatever this REST attempt yields, do NOT fall through
            # to the sleep+retry below and re-hit the same GraphQL limiter.
            # Paged manually and capped at 3 pages -- a full 100-row page means
            # "maybe more", a short page means "done". A repo that fills all 3
            # pages is marked incomplete (see below) instead of silently
            # under-covering past the cap.
            _prs=""; _ok=0; _rok=1; _page=1
            while [ "$_rok" -eq 1 ] && [ "$_page" -le 3 ]; do
              if _pg_out=$(bounded 30 gh api \
                    "repos/$2/pulls?state=open&per_page=100&page=$_page" \
                    --jq '.[] | "\(.number)\t\(.title)\t\(.head.ref)"' 2>"$_err"); then
                [ -n "$_pg_out" ] && _prs="${_prs}${_prs:+$'\n'}${_pg_out}"
                _n=$(printf '%s\n' "$_pg_out" | grep -c '^[0-9]')
                [ "$_n" -lt 100 ] && break   # short page -- no more to fetch
                _page=$((_page+1))
              else
                _rok=0   # bounded REST call itself failed -- fall through to DEGRADE
              fi
            done
            # A capped listing is useful for positive matches only. The
            # cache must NEVER reuse it as a complete list for another ticket.
            [ "$_page" -gt 3 ] && _complete=0
            [ "$_rok" -eq 1 ] && _ok=1
            break   # exit the GraphQL retry loop -- fallback was attempted
        fi
      fi
      [ "$_try" -lt 3 ] && sleep "$_try"   # 1s, then 2s
    done
    if [ "$_ok" -ne 1 ]; then
      printf '%s\t%s\n' "$CC_FAIL_TAG" "$2"   # name the repo
      exit 1   # attempted and genuinely failed -- DEGRADE
    fi
    printf '%s\n' "$_prs"
    [ "$_complete" -eq 1 ] || exit 4
    exit 0 ;;
esac

[ $# -ge 1 ] || usage
RAW=""; SCOPE_REPO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)   [ $# -ge 2 ] && [ -n "$2" ] || usage; SCOPE_REPO="$2"; shift 2 ;;
    --repo=*) SCOPE_REPO="${1#--repo=}"; [ -n "$SCOPE_REPO" ] || usage; shift ;;
    -*)       usage ;;
    *)        [ -z "$RAW" ] || usage; RAW="$1"; shift ;;
  esac
done
[ -n "$RAW" ] || usage

# The prefix is interpolated into regexes below, so it must be plain letters
# and digits. A bad value is a config error, not something to guess around.
if ! printf '%s' "$PREFIX" | grep -qE '^[A-Za-z][A-Za-z0-9]*$'; then
  echo "collision-check: LINEAR_BRANCH_PREFIX='$PREFIX' must be letters/digits, starting with a letter" >&2
  exit 2
fi
PREFIX_LC=$(printf '%s' "$PREFIX" | tr '[:upper:]' '[:lower:]')
PREFIX_UC=$(printf '%s' "$PREFIX" | tr '[:lower:]' '[:upper:]')
NUM=$(printf '%s' "$RAW" | tr '[:upper:]' '[:lower:]')
NUM="${NUM#"$PREFIX_LC"}"; NUM="${NUM#-}"
case "$NUM" in
  ''|*[!0-9]*) usage ;;
esac
TICKET="${PREFIX_UC}-${NUM}"

# The token as it appears in a path or a branch: `dev42` or `dev-42` in any
# case, and NOT a longer number that merely starts with it (`dev-42` must not
# match 4).
CI_PREFIX=""
for ((_i = 0; _i < ${#PREFIX_LC}; _i++)); do
  _c="${PREFIX_LC:_i:1}"
  case "$_c" in
    [a-z]) CI_PREFIX="${CI_PREFIX}[$(printf '%s' "$_c" | tr '[:lower:]' '[:upper:]')${_c}]" ;;
    *)     CI_PREFIX="${CI_PREFIX}${_c}" ;;
  esac
done
TOKEN_RE="${CI_PREFIX}-?${NUM}([^0-9]|\$)"

# STRONG (a workspace/branch/PR that is not the caller's) vs WEAK (the Linear
# state, which the caller's own startwork sets) -- see the header. Only FOUND
# is a collision on its own; WEAK needs the caller to be a stranger to count.
FOUND=0
WEAK=0
INFO=0
DEGRADED=""
report()      { FOUND=1; printf '  ✗ %s\n' "$1"; }
report_weak() { WEAK=1;  printf '  ✗ %s\n' "$1"; }
self_note()   { printf '  · self: %s\n' "$1"; }
skipped()     { printf '  · %s: skipped (%s set)\n' "$1" "$2"; }
degrade() { DEGRADED="${DEGRADED}${1}"$'\n'; printf '  ? %s — probe could not run\n' "$1"; }
# Turn the CC_FAIL_TAG lines a fanned-out probe emitted into one short list for
# its degrade message. Capped: an offline machine fails EVERY clone at once,
# and a 60-name message buries the verdict it is attached to.
fail_names() { # fail_names <base|slug> <worker-stdout> -> "name, name, +N more"
  local n=0 shown=0 out="" tag id name
  while IFS=$'\t' read -r tag id; do
    [ "$tag" = "$CC_FAIL_TAG" ] || continue
    n=$((n+1))
    [ "$shown" -lt 5 ] || continue
    if [ "$1" = base ]; then name=$(basename "$id"); else name="$id"; fi
    out="${out:+$out, }$name"; shown=$((shown+1))
  done <<<"$2"
  [ "$n" -gt "$shown" ] && out="$out, +$((n-shown)) more"
  printf '%s' "${out:-repo not identified}"
}
# A hit that --repo downgraded. It is still printed, and still says out loud
# that it is unverified: the risk this flag introduces is not a wrong exit
# code, it is an orchestrator that learns to skim past "the other repo's hit"
# and stops reading the probe lines at all.
info()    { INFO=1; printf '  ℹ %s — sibling-repo hit; expected for a paired-PR ticket, but VERIFY it is your own worker\n' "$1"; }

# gh_slug <origin-url> -> owner/name, or non-zero if it is not a GitHub remote
# this tool can query. Shared by probe E and the caller-identity resolution so
# the two can never disagree about which repo a clone belongs to.
gh_slug() {
  case "$1" in
    git@github.com:*|https://github.com/*|ssh://git@github.com/*) ;;
    *) return 1 ;;
  esac
  local _s
  _s=$(printf '%s' "$1" | sed -E 's|^ssh://||; s|^git@github\.com:||; s|^https://github\.com/||; s|/$||; s|\.git$||')
  case "$_s" in ''|*' '*|/*|*/*/*) return 1 ;; */*) ;; *) return 1 ;; esac
  printf '%s' "$_s"
}

# --- who is asking? ----------------------------------------------------------
# Resolved BEFORE any probe runs, from the caller's own worktree. The token
# guard is the whole safety story: a caller whose workspace has nothing to do
# with this ticket gets no identity at all, so it can suppress nothing.
CALLER_TOP=""; CALLER_BRANCH=""; CALLER_CLONE=""; CALLER_URL=""; CALLER_SLUG=""
CALLER_REMOTE_FOREIGN=0
# Compare paths the same way on both sides: `git worktree list` and $PWD can
# disagree on symlinked prefixes (/var vs /private/var on macOS) for what is
# physically the same directory.
norm_path() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
_cdir="${COLLISION_CHECK_SELF_DIR:-$PWD}"
if command -v git >/dev/null 2>&1 && [ -d "$_cdir" ]; then
  if _ctop=$(git -C "$_cdir" rev-parse --show-toplevel 2>/dev/null) && [ -n "$_ctop" ]; then
    _cbr=$(git -C "$_ctop" symbolic-ref --quiet --short HEAD 2>/dev/null) || _cbr=""
    if printf '%s' "$_ctop" | grep -qE "$TOKEN_RE" || printf '%s' "$_cbr" | grep -qE "$TOKEN_RE"; then
      CALLER_TOP=$(norm_path "$_ctop")
      CALLER_BRANCH="$_cbr"
      CALLER_URL=$(git -C "$_ctop" remote get-url origin 2>/dev/null) || CALLER_URL=""
      [ -n "$CALLER_URL" ] && CALLER_SLUG=$(gh_slug "$CALLER_URL")
      # the PRIMARY clone this worktree is linked to -- probe C keys on it
      if _cgc=$(git -C "$_ctop" rev-parse --git-common-dir 2>/dev/null) && [ -n "$_cgc" ]; then
        case "$_cgc" in /*) ;; *) _cgc="$_ctop/$_cgc" ;; esac
        CALLER_CLONE=$(norm_path "$(dirname "$_cgc")")
      fi
    fi
  fi
fi

caller_holds_worktree() { # <worktree-path>
  [ -n "$CALLER_TOP" ] || return 1
  [ "$(norm_path "$1")" = "$CALLER_TOP" ]
}

# Same repo as the caller's? Compare by SLUG when both sides resolve to one:
# probe D dedupes remotes across clones, so the clone that actually got probed
# may be a different checkout of the same repo carrying the OTHER URL form
# (git@github.com: vs https://). A literal URL compare would then fail to
# recognise the caller's own push. Fall back to an exact URL match when either
# side is not a GitHub remote.
caller_same_repo() { # <clone>
  local _u _s
  _u=$(git -C "$1" remote get-url origin 2>/dev/null) || return 1
  _s=$(gh_slug "$_u") || _s=""
  if [ -n "$_s" ] && [ -n "$CALLER_SLUG" ]; then
    [ "$_s" = "$CALLER_SLUG" ]
  else
    [ -n "$CALLER_URL" ] && [ "$_u" = "$CALLER_URL" ]
  fi
}

caller_holds_remote() { # <clone> <ref> <sha>
  [ -n "$CALLER_TOP" ] && [ -n "$CALLER_BRANCH" ] || return 1
  [ "$2" = "$CALLER_BRANCH" ] || return 1
  caller_same_repo "$1" || return 1
  # Our own tip, or an ancestor of it (we pushed, then committed more). A remote
  # tip we cannot reach from our own branch is SOMEONE ELSE'S commit -- on the
  # very branch name we would force-push over.
  git -C "$CALLER_TOP" merge-base --is-ancestor "$3" "$CALLER_BRANCH" 2>/dev/null
}

collision_verdict() {
  cat <<EOF

COLLISION — ${TICKET} is already in flight. HALT and report; do not build.
Never clear the way by force: do NOT 'git worktree remove', do NOT 'git branch -D'.
A branch with no PR yet is UNPUSHED work — the most destructible state there is,
not a safe one. Report what is in flight and let the human decide.
EOF
  exit 1
}

echo "collision-check ${TICKET}"
[ -n "$CALLER_TOP" ] && printf '  · caller: %s%s — hits resolving here are not collisions\n' \
  "$CALLER_TOP" "${CALLER_BRANCH:+  [${CALLER_BRANCH}]}"

# git absent would silently void the two probes that actually catch the common
# case (live worktree, unpushed local branch).
command -v git >/dev/null 2>&1 || degrade "worktree + local-branch probes (git not found)"

# --- probe A: Linear state -------------------------------------------------
# The cheapest signal and the earliest: a ticket in In Review has a PR against it
# by definition, and needs no naming convention to hold.
if [ -n "${COLLISION_CHECK_SKIP_LINEAR:-}" ]; then
  skipped "Linear state probe" COLLISION_CHECK_SKIP_LINEAR
elif ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
  degrade "Linear state (curl and jq are required)"
else
  # Same key resolution as linear-startwork.sh: env var first, else a JSON file.
  key="${LINEAR_API_KEY:-}"
  if [ -z "$key" ] && [ -n "${LINEAR_KEY_FILE:-}" ]; then
    key=$(jq -r '.env.LINEAR_API_KEY // ""' "$LINEAR_KEY_FILE" 2>/dev/null)
  fi
  if [ -z "$key" ] || [ -z "$TEAM" ]; then
    degrade "Linear state (unconfigured: set LINEAR_API_KEY or LINEAR_KEY_FILE, and LINEAR_DEV_TEAM_ID — or COLLISION_CHECK_SKIP_LINEAR=1)"
  else
    q="query { issues(filter: { number: { eq: ${NUM} }, team: { id: { eq: \"${TEAM}\" } } }) { nodes { identifier state { name type } assignee { displayName } attachments { nodes { url } } } } }"
    # The key goes in via --config, NOT -H: argv is world-readable, so
    # `-H "Authorization: $key"` publishes the token to any local user running
    # `ps`. printf is a bash builtin and the process substitution is a fork, so
    # the key never reaches another process's argv either.
    resp=$(curl -s --max-time 8 -X POST "$API" \
      --config <(printf 'header = "Authorization: %s"\n' "$key") \
      -H "Content-Type: application/json" \
      -d "$(jq -n --arg q "$q" '{query:$q}')" 2>/dev/null)
    node=$(jq -c '.data.issues.nodes[0] // empty' <<<"$resp" 2>/dev/null)
    if [ -z "$node" ]; then
      # This probe was ATTEMPTED. Empty means the API errored, timed out, or
      # returned something unparseable -- degraded, not clear. (A real ticket
      # always returns a node.)
      degrade "Linear state (API error, timeout, or unknown ticket)"
    else
      st=$(jq -r '.state.name // ""'      <<<"$node")
      ty=$(jq -r '.state.type // ""'      <<<"$node")
      who=$(jq -r '.assignee.displayName // ""' <<<"$node")
      prs=$(jq -r '[.attachments.nodes[]?.url | select(test("/pull/"))] | length' <<<"$node" 2>/dev/null)
      [ -z "$prs" ] && prs=0
      if [ "$st" = "In Review" ]; then
        report_weak "Linear state is '$st' — a PR already exists against this ticket"
      elif { [ "$ty" = "started" ] || [ "$ty" = "completed" ]; } && [ "$prs" -gt 0 ]; then
        report_weak "Linear state is '$st' with $prs linked PR(s)"
      elif { [ "$ty" = "started" ] || [ "$ty" = "completed" ]; } && [ -n "$who" ]; then
        report_weak "Linear state is '$st', assigned to $who — someone else's lane"
      elif [ -n "$who" ]; then
        printf '  · note: assigned to %s (state %s) — not a collision, but not unowned either\n' "$who" "$st"
      fi
    fi
  fi
fi

# --- collect primary clones ------------------------------------------------
# `git worktree list` in a primary clone reports EVERY worktree it owns, wherever
# it lives on disk -- including /tmp and scratch dirs. Scanning the clones is
# therefore complete in a way that globbing a directory never is.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

CLONES=()
if [ -z "${COLLISION_CHECK_ROOTS:-}" ]; then
  degrade "worktree + local-branch + remote + PR probes (unconfigured: set COLLISION_CHECK_ROOTS to the dir(s) holding your clones)"
else
  IFS=':' read -r -a roots <<<"$COLLISION_CHECK_ROOTS"
  for r in "${roots[@]}"; do
    [ -n "$r" ] || continue
    r="${r%/}"
    if [ -e "$r/.git" ]; then
      CLONES+=("$r")   # the root is itself a primary clone
      continue
    fi
    for d in "$r"/*/; do
      [ -d "${d}.git" ] && CLONES+=("${d%/}")
    done
  done
  # Scanning zero clones finds zero collisions -- which is indistinguishable
  # from a clean machine unless we say so. A wrong COLLISION_CHECK_ROOTS would
  # otherwise green-light every ticket forever.
  [ "${#CLONES[@]}" -gt 0 ] || degrade "worktree + local-branch probes (no primary clones found to scan)"
fi

# --- resolve --repo against the clones we actually scan ---------------------
# `--repo` must name one of them. A typo, a repo not cloned on this machine, or
# a directory name that differs from the repo name would otherwise make EVERY
# worktree/branch hit "a different clone", i.e. the flag would silently switch
# off the primary probe. So resolve it up front: no match means drop the scope
# (every hit stays exit-1) AND degrade, because the question the caller asked
# was never actually answered.
SCOPED_CLONES=""
if [ -n "$SCOPE_REPO" ]; then
  for c in "${CLONES[@]:-}"; do
    [ -n "$c" ] || continue
    n=$(basename "$c")
    if [ "$n" != "$SCOPE_REPO" ]; then
      n=$(git -C "$c" remote get-url origin 2>/dev/null \
            | sed -E 's|/$||; s|\.git$||; s|.*[/:]||')
    fi
    [ "$n" = "$SCOPE_REPO" ] && SCOPED_CLONES="${SCOPED_CLONES}${c}"$'\n'
  done
  if [ -z "$SCOPED_CLONES" ]; then
    degrade "--repo scope '$SCOPE_REPO' (no scanned clone has that name)"
    SCOPE_REPO=""
  fi
fi

# fatal_in <clone> -- does a worktree/branch hit in this clone still count as a
# collision? Always, without --repo; with it, only inside the named repo.
fatal_in() {
  [ -n "$SCOPE_REPO" ] || return 0
  grep -qxF -- "$1" <<<"$SCOPED_CLONES"
}

# --- probe B: live worktrees (primary) -------------------------------------
# A clone whose git call FAILS is a clone we did not scan -- degrade, never
# treat it as "nothing there". `degrade` must run in THIS shell, so the git
# output is captured first rather than piped through a subshell.
HELD_BRANCHES=""
for c in "${CLONES[@]:-}"; do
  [ -n "$c" ] || continue
  if ! _wt_out=$(git -C "$c" worktree list --porcelain 2>/dev/null); then
    degrade "worktree probe in $(basename "$c") (git failed)"
    continue
  fi
  wt=""; br=""
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) wt="${line#worktree }"; br="" ;;
      "branch refs/heads/"*)
        br="${line#branch refs/heads/}"
        HELD_BRANCHES="${HELD_BRANCHES}${c}"$'\t'"${br}"$'\n'
        ;;
      "")  # blank line terminates a record
        if [ -n "$wt" ] && { printf '%s' "$wt" | grep -qE "$TOKEN_RE" || printf '%s' "$br" | grep -qE "$TOKEN_RE"; }; then
          if caller_holds_worktree "$wt"; then
            self_note "live worktree ${wt}${br:+  [${br}]} — the caller's own workspace"
          elif fatal_in "$c"; then
            report "live worktree ${wt}${br:+  [${br}]} — another agent's workspace"
          else
            info "live worktree ${wt}${br:+  [${br}]} in $(basename "$c")"
          fi
        fi
        wt=""; br=""
        ;;
    esac
  done <<<"${_wt_out}"$'\n'
done

# --- probe C: local branches not held by a worktree ------------------------
# An agent that has committed but not pushed is invisible to ls-remote; this and
# probe B are the only things that see it.
for c in "${CLONES[@]:-}"; do
  [ -n "$c" ] || continue
  if ! _refs=$(git -C "$c" for-each-ref --format='%(refname:short)' refs/heads 2>/dev/null); then
    degrade "local-branch probe in $(basename "$c") (git failed)"
    continue
  fi
  while IFS= read -r b; do
    [ -n "$b" ] || continue
    printf '%s' "$b" | grep -qE "$TOKEN_RE" || continue
    # keyed by clone, not by branch name alone: the same branch name in a
    # different clone is a different collision, not a duplicate report.
    grep -qxF "${c}"$'\t'"${b}" <<<"$HELD_BRANCHES" && continue
    if [ -n "$CALLER_BRANCH" ] && [ "$b" = "$CALLER_BRANCH" ] \
       && [ -n "$CALLER_CLONE" ] && [ "$(norm_path "$c")" = "$CALLER_CLONE" ]; then
      self_note "local branch '$b' in $(basename "$c") — the caller's own"
      continue
    fi
    if fatal_in "$c"; then
      report "local branch '$b' in $(basename "$c") — may hold unpushed commits"
    else
      info "local branch '$b' in $(basename "$c")"
    fi
  done <<<"$_refs"
done

# Probes D and E are network-bound; fan them out 8-wide or a machine with many
# clones waits over a minute, which is too slow for a gate that runs every time.
export CC_TOKEN_RE="$TOKEN_RE" CC_NUM="$NUM"

# --- probe D: remote branches ----------------------------------------------
# Work pushed from another machine, which nothing local can see.
if [ -n "${COLLISION_CHECK_SKIP_REMOTE:-}" ]; then
  skipped "remote-branch probe" COLLISION_CHECK_SKIP_REMOTE
else
  seen_remotes=""; targets=""
  for c in "${CLONES[@]:-}"; do
    [ -n "$c" ] || continue
    # A filesystem origin reflects another local clone's branches, including
    # the caller's own unpushed worktree branch. It cannot prove a foreign push.
    # Read the configured URL: `remote get-url` expands insteadOf rewrites and
    # can turn a genuine GitHub URL into a local test transport.
    # `config --get` returns the LAST value when remote.origin.url is
    # multi-valued, but `remote get-url origin` (and the ls-remote it feeds)
    # act on the FIRST. Read the first one here too, or a network-first/
    # local-later config skips a real remote probe, and a local-first/
    # network-later config probes a local path this classification meant to
    # exclude.
    configured_url=$(git -C "$c" config --get-all remote.origin.url 2>/dev/null | sed -n '1p')
    [ -n "$configured_url" ] || continue
    case "$configured_url" in
      file://*) continue ;;
      *://*|*:*) ;;  # network URL or SCP-style host:path
      *) continue ;; # absolute, relative, and home-relative paths
    esac
    url=$(git -C "$c" remote get-url origin 2>/dev/null) || continue
    [ -n "$url" ] || continue
    grep -qxF "$url" <<<"$seen_remotes" && continue
    seen_remotes="${seen_remotes}${url}"$'\n'
    targets="${targets}${c}"$'\n'
  done
  _out=""
  if [ -n "$targets" ]; then
    # xargs exits 123 when any worker exits non-zero -- that is a repo we failed
    # to reach, not a repo with no matching branch.
    if ! _out=$(printf '%s' "$targets" | tr '\n' '\0' | xargs -0 -P 8 -n 1 "$SELF" --_remote 2>/dev/null); then
      degrade "remote-branch probe (could not reach: $(fail_names base "$_out"))"
    fi
    # The tag lines are diagnostics, not findings -- strip them before parsing.
    _out=$(grep -v "^${CC_FAIL_TAG}"$'\t' <<<"$_out")
  fi
  while IFS=$'\t' read -r ref clone sha; do
    [ -n "$ref" ] || continue
    if caller_holds_remote "$clone" "$ref" "$sha"; then
      self_note "remote branch '${ref}' on $(basename "$clone") — the caller's own push"
      continue
    fi
    # Same branch name in OUR repo, but not reachable from ours: whatever opened
    # a PR on it is not us either, so probe E must not credit that PR to the
    # caller. Scoped to our own repo -- a same-named branch in some other repo
    # says nothing about who owns our PR.
    if [ -n "$CALLER_BRANCH" ] && [ "$ref" = "$CALLER_BRANCH" ] && caller_same_repo "$clone"; then
      CALLER_REMOTE_FOREIGN=1
    fi
    report "remote branch '${ref}' on $(basename "$clone") — pushed from elsewhere"
  done <<<"$_out"
fi

# --- probe E: open PRs ------------------------------------------------------
# Every listed PR is filtered against the token (number, title AND branch) in
# the --_pr mode before it counts.
if [ -n "${COLLISION_CHECK_SKIP_PRS:-}" ]; then
  skipped "open-PR probe" COLLISION_CHECK_SKIP_PRS
elif ! command -v gh >/dev/null 2>&1; then
  degrade "open-PR probe (gh not found — install it, or set COLLISION_CHECK_SKIP_PRS=1)"
elif [ -z "${COLLISION_CHECK_OWN_ORGS:-}" ]; then
  degrade "open-PR probe (unconfigured: set COLLISION_CHECK_OWN_ORGS to the GitHub owners your branches land in, or COLLISION_CHECK_SKIP_PRS=1)"
else
  seen_repos=""; slugs=""
  for c in "${CLONES[@]:-}"; do
    [ -n "$c" ] || continue
    url=$(git -C "$c" remote get-url origin 2>/dev/null) || continue
    # Only GitHub remotes have PRs. A local-path or non-GitHub origin has
    # nothing for this probe to ask; skipping it silently keeps the DEGRADED
    # signal rare enough to still mean something.
    slug=$(gh_slug "$url") || continue
    grep -qxF "$slug" <<<"$seen_repos" && continue
    seen_repos="${seen_repos}${slug}"$'\n'
    if ! own_org "$slug"; then
      printf '  ℹ open PRs in %s: skipped (not an own-org remote)\n' "$slug"
      continue
    fi
    slugs="${slugs}${slug}"$'\n'
  done
  _out=""
  if [ -n "$slugs" ]; then
    if ! _out=$(printf '%s' "$slugs" | tr '\n' '\0' | xargs -0 -P 8 -n 1 "$SELF" --_pr 2>/dev/null); then
      degrade "open-PR probe (could not query: $(fail_names slug "$_out"); if a remote is retired, add its owner/name to COLLISION_CHECK_RETIRED_REMOTES)"
    fi
    _out=$(grep -v "^${CC_FAIL_TAG}"$'\t' <<<"$_out")
  fi
  while IFS=$'\t' read -r slug head pr; do
    [ -n "$pr" ] || continue
    if [ "$CALLER_REMOTE_FOREIGN" -eq 0 ] && [ -n "$CALLER_BRANCH" ] && [ "$head" = "$CALLER_BRANCH" ] \
       && [ -n "$CALLER_SLUG" ] && [ "$slug" = "$CALLER_SLUG" ]; then
      self_note "open PR in ${slug}: ${pr} — the caller's own branch"
      continue
    fi
    report "open PR in ${slug}: ${pr}"
  done <<<"$_out"
fi

# --- verdict ----------------------------------------------------------------
if [ "$FOUND" -eq 1 ]; then
  collision_verdict
fi
# A lone Linear-state hit from a caller that is a stranger to this ticket is a
# collision -- including when a probe also degraded.
if [ "$WEAK" -eq 1 ] && [ -z "$CALLER_TOP" ]; then
  collision_verdict
fi
if [ "$WEAK" -eq 1 ]; then
  cat <<EOF

SELF — the Linear state above is the only hit, and this caller owns the ticket's
workspace (${CALLER_TOP}${CALLER_BRANCH:+  [${CALLER_BRANCH}]}): linear-startwork.sh
flips the ticket the moment the builder creates its branch. Every strong probe —
worktree, local branch, remote branch, open PR — either found nothing or resolved
to this caller, so there is no second agent to halt for.
EOF
fi
if [ -n "$DEGRADED" ]; then
  cat <<EOF

DEGRADED — no collision found, but this is NOT a clear result: the probe(s)
above could not run. Treat as UNKNOWN. Report the degradation rather than
building on the assumption that silence means nobody is there.
EOF
  exit 3
fi
if [ "$INFO" -eq 1 ]; then
  cat <<EOF

CLEAR for ${SCOPE_REPO} — nothing is building ${TICKET} in ${SCOPE_REPO} itself.
The ℹ line(s) above are hits in OTHER repos, downgraded because --repo was
passed. That is the expected shape of a paired-PR ticket, and it is NOT proof:
confirm each one is your own sibling worker before you build. A stranger's
worktree in the other repo looks exactly the same from here.
EOF
  exit 0
fi
echo "CLEAR — no other agent appears to be building ${TICKET}."
exit 0
