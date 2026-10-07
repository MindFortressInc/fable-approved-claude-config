#!/usr/bin/env bash
# Exercises collision-check.sh against REAL throwaway git repos. No network:
# Linear (probe A) and gh (probe E) are stubbed, and the fake clones have no
# reachable remote. Fixture tickets use the non-default `eng` prefix (fake ids),
# which also proves LINEAR_BRANCH_PREFIX reaches every probe.
set -uo pipefail
# Nothing from the invoking shell's config may leak into a verdict: every test
# below states the config it relies on.
unset LINEAR_API_KEY LINEAR_KEY_FILE LINEAR_DEV_TEAM_ID LINEAR_API_URL \
      COLLISION_CHECK_SKIP_PRS COLLISION_CHECK_RETIRED_REMOTES
export LINEAR_BRANCH_PREFIX=eng
SCRATCH="${TMPDIR:-/tmp}/collision-check-test.$$"
rm -rf "$SCRATCH"; mkdir -p "$SCRATCH/bin" "$SCRATCH/clones"
trap 'rm -rf "$SCRATCH"' EXIT
SCRIPT="$(cd "$(dirname "$0")" && pwd)/collision-check.sh"

# Probe A (Linear) and probe E (gh) are stubbed silent -- these tests are about
# the local git probes. Their own behaviour is covered by T8/T9/T10.
# Probe D (ls-remote) is skipped outright: these fixtures' remotes are fake, and
# the suite must not touch the network.
export COLLISION_CHECK_SKIP_LINEAR=1
export COLLISION_CHECK_SKIP_REMOTE=1
# Fixture owners are explicitly in scope. This legacy suite changes its gh
# stub between calls; shared-cache behavior has its own integration suite.
export COLLISION_CHECK_OWN_ORGS=x:acme:example-org:example-user
export COLLISION_CHECK_PR_CACHE_DIR=''
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"
export PATH="$SCRATCH/bin:$PATH"
export COLLISION_CHECK_ROOTS="$SCRATCH/clones"
# This suite is routinely run from inside a `<repo>.<prefix>-NNN` worktree, and the
# caller-identity resolution keys on exactly that. Pin the caller to
# a non-repo directory so no test's verdict depends on where it was invoked;
# the self tests at the bottom set it explicitly.
export COLLISION_CHECK_SELF_DIR="$SCRATCH"

pass=0; fail=0
ok()   { echo "  PASS: $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL: $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/        /'; fail=$((fail+1)); }
check(){ # check <label> <expected-substring> <actual>
  if grep -qF -- "$2" <<<"$3"; then ok "$1"; else bad "$1 -- expected to contain: $2" "$3"; fi
}
absent(){ # absent <label> <unexpected-substring> <actual>
  if grep -qF -- "$2" <<<"$3"; then bad "$1 -- should NOT contain: $2" "$3"; else ok "$1"; fi
}

# mkclone <name> -- a primary clone with one commit on main
mkclone() {
  local p="$SCRATCH/clones/$1"
  git init -q -b main "$p"
  git -C "$p" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  echo "$p"
}
# mkwt <clone> <worktree-path> <branch> -- a linked worktree holding <branch>
mkwt() {
  git -C "$1" worktree add -q -b "$3" "$2" main 2>/dev/null
}

echo "T1: acceptance -- the probe finds a worktree named '.eng7988' (NO hyphen)"
c=$(mkclone repo-a); mkwt "$c" "$SCRATCH/repo-a.eng7988" wip/no-ticket-token
o=$("$SCRIPT" ENG-7988 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "names the worktree" "repo-a.eng7988" "$o"

echo "T2: acceptance -- the probe finds a worktree named '.eng-7988' (hyphen)"
c=$(mkclone repo-b); mkwt "$c" "$SCRATCH/repo-b.eng-7988" wip/b
o=$("$SCRIPT" ENG-7988 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "names the worktree" "repo-b.eng-7988" "$o"

echo "T3: acceptance -- the probe finds '<repo>.dev-NNNN-slug'"
c=$(mkclone repo-c); mkwt "$c" "$SCRATCH/repo-c.eng-7988-some-slug" wip/c
o=$("$SCRIPT" ENG-7988 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "names the worktree" "repo-c.eng-7988-some-slug" "$o"

echo "T4: acceptance -- an unpushed local branch on the target ticket is detected"
c=$(mkclone repo-d)
git -C "$c" branch me/eng-6501-unpushed-work
o=$("$SCRIPT" ENG-6501 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "names the branch" "me/eng-6501-unpushed-work" "$o"
check "labels it local" "local branch" "$o"

echo "T5: blind spot 4b -- a non-'me/' prefix is still detected"
c=$(mkclone repo-e)
git -C "$c" branch feat/eng-6502-fe-router-p1
o=$("$SCRIPT" ENG-6502 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "finds feat/-prefixed branch" "feat/eng-6502-fe-router-p1" "$o"

echo "T6: blind spot 3 -- PR-numbered path, token only in the BRANCH"
c=$(mkclone repo-f); mkwt "$c" "$SCRATCH/repo-f-911-cli" feat/eng-6503-fe-router
o=$("$SCRIPT" ENG-6503 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "names the worktree by its branch" "repo-f-911-cli" "$o"

echo "T7: a genuinely clear ticket exits 0"
o=$("$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "exit 0 (clear)" || bad "expected exit 0, got $rc" "$o"
check "says clear" "CLEAR" "$o"

echo "T8: probe A -- 'In Review' is a collision"
cat > "$SCRATCH/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo '{"data":{"issues":{"nodes":[{"identifier":"ENG-9998","state":{"name":"In Review","type":"started"},"assignee":null,"attachments":{"nodes":[]}}]}}}'
STUB
chmod +x "$SCRATCH/bin/curl"
o=$(COLLISION_CHECK_SKIP_LINEAR='' LINEAR_API_KEY=stub LINEAR_DEV_TEAM_ID=team-fixture-id "$SCRIPT" ENG-9998 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "names the Linear state" "In Review" "$o"

echo "T9: probe A that was ATTEMPTED and FAILED reports DEGRADED, never CLEAR"
cat > "$SCRATCH/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo '{"errors":[{"message":"boom"}]}'
STUB
chmod +x "$SCRATCH/bin/curl"
o=$(COLLISION_CHECK_SKIP_LINEAR='' LINEAR_API_KEY=stub LINEAR_DEV_TEAM_ID=team-fixture-id "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "exit 3 (degraded)" || bad "expected exit 3 on API error, got $rc" "$o"
check  "says DEGRADED" "DEGRADED" "$o"
absent "does NOT claim CLEAR" "CLEAR —" "$o"

echo "T9b: UNCONFIGURED Linear is DEGRADED, never CLEAR -- no key, no team, no skip"
CAPFILE="$SCRATCH/curl-capture.log"
: > "$CAPFILE"
cat > "$SCRATCH/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$CURL_CAPTURE_FILE"
echo '{"data":{"issues":{"nodes":[]}}}'
STUB
chmod +x "$SCRATCH/bin/curl"
o=$(CURL_CAPTURE_FILE="$CAPFILE" COLLISION_CHECK_SKIP_LINEAR='' "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "exit 3 (unconfigured is degraded)" || bad "expected exit 3, got $rc" "$o"
check  "says DEGRADED" "DEGRADED" "$o"
check  "names the missing config" "LINEAR_API_KEY" "$o"
absent "does NOT claim CLEAR" "CLEAR —" "$o"
[[ ! -s "$CAPFILE" ]] && ok "no tracker call without config" || bad "curl was called: $(cat "$CAPFILE")"

echo "T9c: a key with NO team id is still unconfigured -> DEGRADED"
o=$(CURL_CAPTURE_FILE="$CAPFILE" COLLISION_CHECK_SKIP_LINEAR='' LINEAR_API_KEY=stub "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "exit 3 (no team id)" || bad "expected exit 3, got $rc" "$o"
check "names the team setting" "LINEAR_DEV_TEAM_ID" "$o"
[[ ! -s "$CAPFILE" ]] && ok "no tracker call without a team" || bad "curl was called: $(cat "$CAPFILE")"

echo "T8c: LINEAR_DEV_TEAM_ID and LINEAR_API_URL reach probe A's query; the key never reaches argv"
: > "$CAPFILE"
CURL_CAPTURE_FILE="$CAPFILE" COLLISION_CHECK_SKIP_LINEAR='' LINEAR_API_KEY=lin-key-fixture \
  LINEAR_DEV_TEAM_ID="team-fixture-id" LINEAR_API_URL="https://tracker.example.test/graphql" \
  "$SCRIPT" ENG-9999 >/dev/null 2>&1
captured="$(cat "$CAPFILE")"
check  "LINEAR_DEV_TEAM_ID reaches probe A's query" "team-fixture-id" "$captured"
check  "LINEAR_API_URL is the actual endpoint invoked" "https://tracker.example.test/graphql" "$captured"
absent "default endpoint is not invoked when overridden" "api.linear.app" "$captured"
absent "the API key is not on curl's argv" "lin-key-fixture" "$captured"

echo "T8d: with LINEAR_API_URL unset, the public Linear endpoint is used"
: > "$CAPFILE"
CURL_CAPTURE_FILE="$CAPFILE" COLLISION_CHECK_SKIP_LINEAR='' LINEAR_API_KEY=stub LINEAR_DEV_TEAM_ID=team-fixture-id \
  "$SCRIPT" ENG-9999 >/dev/null 2>&1
check "default endpoint is Linear's" "https://api.linear.app/graphql" "$(cat "$CAPFILE")"

echo "T8e: LINEAR_KEY_FILE (JSON .env.LINEAR_API_KEY) is the fallback key source"
printf '{"env":{"LINEAR_API_KEY":"file-key-fixture"}}\n' > "$SCRATCH/linear-key.json"
cat > "$SCRATCH/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo '{"data":{"issues":{"nodes":[{"identifier":"ENG-9998","state":{"name":"In Review","type":"started"},"assignee":null,"attachments":{"nodes":[]}}]}}}'
STUB
chmod +x "$SCRATCH/bin/curl"
o=$(COLLISION_CHECK_SKIP_LINEAR='' LINEAR_KEY_FILE="$SCRATCH/linear-key.json" LINEAR_DEV_TEAM_ID=team-fixture-id \
    "$SCRIPT" ENG-9998 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "key file resolved -> probe A ran (exit 1 on In Review)" || bad "expected exit 1, got $rc" "$o"
check "names the Linear state" "In Review" "$o"
rm -f "$SCRATCH/bin/curl" "$CAPFILE" "$SCRATCH/linear-key.json"

echo "T10: probe E -- an open PR referencing the ticket is a collision"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "913	fix: /partners allow-list (ENG-6504)	me/eng-6504-partners	OPEN"
STUB
chmod +x "$SCRATCH/bin/gh"
c=$(mkclone repo-g); git -C "$c" remote add origin https://github.com/x/repo-g.git
o=$("$SCRIPT" ENG-6504 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "names the PR" "913" "$o"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"

echo "T11: the script MUTATES NOTHING -- no worktree removed, no branch deleted"
# snapshot EVERY fixture repo, not just one -- a probe that removed some other
# clone's worktree would otherwise pass this test.
snap_all() { for r in "$SCRATCH"/clones/*/; do
    echo "== $r"; git -C "$r" worktree list 2>/dev/null | sort; git -C "$r" branch -a 2>/dev/null | sort
  done; }
before_all=$(snap_all)
before_dirs=$(find "$SCRATCH" -maxdepth 1 | sort)
"$SCRIPT" ENG-7988 >/dev/null 2>&1
[[ "$before_all" == "$(snap_all)" ]] && ok "worktrees + branches unchanged across ALL fixtures" || bad "a fixture repo changed"
[[ "$before_dirs" == "$(find "$SCRATCH" -maxdepth 1 | sort)" ]] && ok "no directory removed" || bad "a directory disappeared"

echo "T12: a bare number and a lowercase id are accepted, a junk arg exits 2"
o=$("$SCRIPT" 7988 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "bare number exits 1" || bad "expected exit 1, got $rc" "$o"
check "bare number works" "repo-a.eng7988" "$o"
o=$("$SCRIPT" eng-7988 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "lowercase exits 1" || bad "expected exit 1, got $rc" "$o"
check "lowercase works" "repo-a.eng7988" "$o"
o=$("$SCRIPT" nonsense 2>&1); rc=$?
[[ $rc -eq 2 ]] && ok "exit 2 (usage)" || bad "expected exit 2 on junk arg, got $rc" "$o"

echo "T13: a substring number does not false-positive (7988 must not match 79881)"
c=$(mkclone repo-h); mkwt "$c" "$SCRATCH/repo-h.eng-79881" wip/h
o=$("$SCRIPT" ENG-79881 2>&1)
check  "ENG-79881 DOES report its own worktree"  "repo-h.eng-79881" "$o"
absent "ENG-79881 does not report repo-a.eng7988" "repo-a.eng7988" "$o"
o=$("$SCRIPT" ENG-7988 2>&1)
check  "ENG-7988 DOES report its own worktree"   "repo-a.eng7988"  "$o"
absent "ENG-7988 does not report repo-h.eng-79881" "repo-h.eng-79881" "$o"

echo "T14: probe D -- a clone path containing a SPACE is still probed (xargs -0)"
# A GitHub URL rewritten to a local bare repo keeps this offline while testing
# a real remote's foreign branch. The configured URL must remain non-local.
# Its OWN root dir: probe D is live for this test, and T10's fixture carries a
# real github.com URL that must never be contacted.
mkdir -p "$SCRATCH/d-clones"
BARE="$SCRATCH/origin-a.git"
git init -q --bare "$BARE"
seed="$SCRATCH/d-seed"
git init -q -b main "$seed"
git -C "$seed" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$seed" branch me/eng-6505-pushed-elsewhere
git -C "$seed" push -q "$BARE" me/eng-6505-pushed-elsewhere
spaced="$SCRATCH/d-clones/repo with space"
git init -q -b main "$spaced"
git -C "$spaced" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$spaced" remote add origin https://github.com/x/repo-spaced.git
git -C "$spaced" config "url.$BARE.insteadOf" https://github.com/x/repo-spaced.git
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/d-clones" COLLISION_CHECK_SKIP_REMOTE='' "$SCRIPT" ENG-6505 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "probe D finds the remote branch" "me/eng-6505-pushed-elsewhere" "$o"
check "names the space-containing clone" "repo with space" "$o"

echo "T14b: a path-remote clone must not echo the caller's local branch as foreign"
mkdir -p "$SCRATCH/path-clones"
path_origin="$SCRATCH/path-origin"
git init -q -b main "$path_origin"
git -C "$path_origin" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
path_wt="$SCRATCH/path-origin.eng-6507"
git -C "$path_origin" worktree add -q -b me/eng-6507-own "$path_wt" main
path_clone="$SCRATCH/path-clones/repo-path"
git init -q -b main "$path_clone"
git -C "$path_clone" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$path_clone" remote add origin "$path_origin"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/path-clones" COLLISION_CHECK_SELF_DIR="$path_wt" \
    COLLISION_CHECK_SKIP_REMOTE='' "$SCRIPT" ENG-6507 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "local path origin -> exit 0" || bad "expected exit 0, got $rc" "$o"
absent "own branch is not called a foreign remote" "pushed from elsewhere" "$o"
git -C "$path_clone" remote set-url origin "file://$path_origin"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/path-clones" COLLISION_CHECK_SELF_DIR="$path_wt" \
    COLLISION_CHECK_SKIP_REMOTE='' "$SCRIPT" ENG-6507 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "file:// origin -> exit 0" || bad "expected exit 0, got $rc" "$o"
absent "file:// also hides the own branch" "pushed from elsewhere" "$o"
git clone -q --bare "$path_origin" "$path_clone/rel-origin.git"
git -C "$path_clone" remote set-url origin rel-origin.git
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/path-clones" COLLISION_CHECK_SELF_DIR="$path_wt" \
    COLLISION_CHECK_SKIP_REMOTE='' "$SCRIPT" ENG-6507 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "bare relative origin -> exit 0" || bad "expected exit 0, got $rc" "$o"
absent "bare relative origin hides the own branch" "pushed from elsewhere" "$o"

echo "T14c: multi-valued remote.origin.url, network first / local second -- probe D must still classify (and probe) the FIRST url"
# `git config --get` returns the LAST value on a multi-valued key.
# Put the network url first and a local path second so the buggy last-value
# read misclassifies this clone as local-origin and skips it, silently
# swallowing a real foreign push. Reuses T14's insteadOf trick to keep this
# offline: the "network" url is rewritten to a local bare repo, but the string
# `git config` sees is still `https://...`.
mkdir -p "$SCRATCH/multi-clones"
BARE_C="$SCRATCH/origin-c.git"
git init -q --bare "$BARE_C"
seed_c="$SCRATCH/d-seed-c"
git init -q -b main "$seed_c"
git -C "$seed_c" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$seed_c" branch me/eng-6511-pushed-elsewhere
git -C "$seed_c" push -q "$BARE_C" me/eng-6511-pushed-elsewhere
multi1="$SCRATCH/multi-clones/repo-multi1"
git init -q -b main "$multi1"
git -C "$multi1" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$multi1" remote add origin https://github.com/x/repo-multi1.git
git -C "$multi1" config "url.$BARE_C.insteadOf" https://github.com/x/repo-multi1.git
git -C "$multi1" config --add remote.origin.url "$SCRATCH/some-local-path-6511"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/multi-clones" COLLISION_CHECK_SKIP_REMOTE='' "$SCRIPT" ENG-6511 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "network-first/local-later -> exit 1 (collision still found)" || bad "expected exit 1, got $rc" "$o"
check "probe D finds the multi-valued remote's foreign branch" "me/eng-6511-pushed-elsewhere" "$o"

echo "T14d: multi-valued remote.origin.url, local first / network second -- probe D must classify (and skip) the FIRST url"
# Reverse of T14c: local path first, an unreachable network url second. The
# buggy last-value read sees the network url, decides this clone IS eligible
# for probe D, then probes using the FIRST url (a local path) -- exactly the
# path-remote case T14b exists to exclude. That surfaces a branch that merely
# lives in a local sibling clone as a false "pushed from elsewhere".
mkdir -p "$SCRATCH/multi-clones2"
local_origin_d="$SCRATCH/local-origin-d"
git init -q -b main "$local_origin_d"
git -C "$local_origin_d" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$local_origin_d" branch me/eng-6512-in-local-origin
multi2="$SCRATCH/multi-clones2/repo-multi2"
git init -q -b main "$multi2"
git -C "$multi2" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$multi2" remote add origin "$local_origin_d"
git -C "$multi2" config --add remote.origin.url "https://github.com/x/never-reached-6512.git"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/multi-clones2" COLLISION_CHECK_SKIP_REMOTE='' "$SCRIPT" ENG-6512 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "local-first/network-later -> exit 0 (local url correctly excluded)" || bad "expected exit 0, got $rc" "$o"
absent "local sibling branch not reported as pushed from elsewhere" "pushed from elsewhere" "$o"

echo "T15: the same branch name in TWO clones is reported for both, not deduped away"
c1=$(mkclone repo-i); c2=$(mkclone repo-j)
mkwt "$c1" "$SCRATCH/repo-i.eng-6506" me/eng-6506-shared   # held by a worktree
git -C "$c2" branch me/eng-6506-shared                     # loose in a different clone
o=$("$SCRIPT" ENG-6506 2>&1)
check "worktree in repo-i reported" "repo-i.eng-6506" "$o"
check "loose branch in repo-j reported too" "in repo-j" "$o"

echo "T16: probe E finds a PR whose token has NO hyphen (eng6507), not just eng-6507"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '914\tfix: harden the thing (eng6507)\twip/no-token-in-branch\n'
printf '915\tunrelated cleanup\tchore/tidy\n'
STUB
chmod +x "$SCRATCH/bin/gh"
mkdir -p "$SCRATCH/e-clones/repo-k"
git init -q -b main "$SCRATCH/e-clones/repo-k"
git -C "$SCRATCH/e-clones/repo-k" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$SCRATCH/e-clones/repo-k" remote add origin https://github.com/x/repo-k.git
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/e-clones" "$SCRIPT" ENG-6507 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check  "finds the hyphen-less PR" "914" "$o"
absent "does not report the unrelated PR" "915" "$o"

echo "T17: scanning ZERO clones is DEGRADED, not CLEAR"
mkdir -p "$SCRATCH/empty-root"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/empty-root" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "exit 3 (degraded)" || bad "expected exit 3, got $rc" "$o"
check  "names the empty scan" "no primary clones found" "$o"
absent "does NOT claim CLEAR" "CLEAR —" "$o"

echo "T18: a clone whose git call FAILS is DEGRADED, not silently skipped"
mkdir -p "$SCRATCH/broken-root/notarepo/.git"   # looks like a clone, git will refuse it
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/broken-root" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "exit 3 (degraded)" || bad "expected exit 3, got $rc" "$o"
check  "names the unreadable clone" "notarepo" "$o"
absent "does NOT claim CLEAR" "CLEAR —" "$o"

echo "T19: a probe-E worker that FAILS degrades the verdict (vs. finding nothing)"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 1          # auth failure / API error -- NOT "no open PRs"
STUB
chmod +x "$SCRATCH/bin/gh"
mkdir -p "$SCRATCH/f-clones/repo-l"
git init -q -b main "$SCRATCH/f-clones/repo-l"
git -C "$SCRATCH/f-clones/repo-l" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$SCRATCH/f-clones/repo-l" remote add origin https://github.com/x/repo-l.git
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/f-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "exit 3 (degraded)" || bad "expected exit 3, got $rc" "$o"
check  "names the failed PR probe" "open-PR probe" "$o"
check  "names the repo it could not query" "x/repo-l" "$o"
absent "the un-queryable repo is not itself reported as a hit" "open PR in" "$o"
absent "does NOT claim CLEAR" "CLEAR —" "$o"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"

echo "T20: a healthy gh returning NO matching PRs is still CLEAR, not degraded"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/f-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "exit 0 (clear)" || bad "expected exit 0, got $rc" "$o"
absent "no degraded noise" "DEGRADED" "$o"

echo "T21: a non-GitHub origin is skipped silently, NOT degraded"
# Regression: the slug guard used to pass a local-path origin through to
# `gh pr list --repo /path/to/repo`, which failed on every real run and made the
# whole verdict DEGRADED — the cry-wolf failure that trains the signal away.
mkdir -p "$SCRATCH/g-clones/repo-m"
git init -q -b main "$SCRATCH/g-clones/repo-m"
git -C "$SCRATCH/g-clones/repo-m" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$SCRATCH/g-clones/repo-m" remote add origin /some/local/path/repo-m
export GH_CALL_LOG="$SCRATCH/gh-calls.log"; : > "$GH_CALL_LOG"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
exit 1          # would fail if probe E were handed a local path
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/g-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "exit 0 (clear)" || bad "expected exit 0, got $rc" "$o"
[[ ! -s "$GH_CALL_LOG" ]] && ok "gh was never invoked for a local-path origin" \
  || bad "gh WAS invoked: $(cat "$GH_CALL_LOG")"
absent "no degraded noise from a local-path remote" "DEGRADED" "$o"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"

# --- probe E reliability ----------------------------------------------------
# A gate that reports UNKNOWN on ~17% of clean runs gets trained away within a
# day (the script's own header predicts this). These four pin
# the distinction the old code could not draw: a probe that FLAKED, a remote
# that is GONE, and a probe that genuinely FAILED are three different things.
mkdir -p "$SCRATCH/r-clones/repo-x"
git init -q -b main "$SCRATCH/r-clones/repo-x"
git -C "$SCRATCH/r-clones/repo-x" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$SCRATCH/r-clones/repo-x" remote add origin https://github.com/acme/repo-x.git

echo
echo "T22: a TRANSIENT probe-E failure is retried, not degraded"
export GH_ATTEMPT_FILE="$SCRATCH/gh-attempts"; : > "$GH_ATTEMPT_FILE"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo x >> "$GH_ATTEMPT_FILE"
if [ "$(wc -l < "$GH_ATTEMPT_FILE" | tr -d ' ')" -lt 2 ]; then
  echo "error connecting to api.github.com: connection reset by peer" >&2; exit 1
fi
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "transient failure retried -> exit 0" || bad "expected exit 0, got $rc" "$o"
absent "no DEGRADED after a successful retry" "DEGRADED" "$o"
[[ $(wc -l < "$GH_ATTEMPT_FILE" | tr -d ' ') -ge 2 ]] \
  && ok "probe E actually retried (>=2 attempts)" \
  || bad "no retry happened: $(wc -l < "$GH_ATTEMPT_FILE") attempt(s)"

echo
echo "T23: an UNDECLARED 404 DEGRADES — GitHub 404s a private repo you cannot see,"
echo "     so 'gone' and 'no access' are indistinguishable and must fail closed"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh: Not Found (HTTP 404)" >&2
exit 1
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "undeclared 404 -> exit 3 DEGRADED (no silent skip)" \
  || bad "expected exit 3, got $rc — a 404 was treated as clear" "$o"
check "the degrade message names the escape hatch" "COLLISION_CHECK_RETIRED_REMOTES" "$o"
check "and names the 404ing repo, so the hatch is actionable" "acme/repo-x" "$o"

echo
echo "T23b: a DECLARED-retired remote's 404 is unconfigured, not degraded"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" \
    COLLISION_CHECK_RETIRED_REMOTES="acme/repo-x" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "declared-retired 404 -> exit 0" || bad "expected exit 0, got $rc" "$o"
absent "no DEGRADED from a declared-retired repo" "DEGRADED" "$o"

echo
echo "T23c: the undeclared-404 exit does not leak its mktemp'd stderr sink"
# That branch exits from INSIDE the retry loop, so the cleanup has to be a trap
# set at the mktemp, not a line after the loop -- otherwise a machine with one
# undeclared-404 remote drops a cc-pr-XXXXXX file into $TMPDIR on every single
# gate run, and this gate runs before every build.
mkdir -p "$SCRATCH/leak-tmp"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" TMPDIR="$SCRATCH/leak-tmp" \
    "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "undeclared 404 still DEGRADES with the trap in place" \
  || bad "expected exit 3, got $rc" "$o"
_leak=""
for _f in "$SCRATCH/leak-tmp"/cc-pr-*; do [[ -e $_f ]] && _leak="$_leak ${_f##*/}"; done
[[ -z ${_leak// /} ]] && ok "no cc-pr-* file survived in \$TMPDIR" \
  || bad "leaked stderr sink(s):$_leak"

echo
echo "T24: a PERSISTENT probe-E failure still DEGRADES (fail-closed preserved)"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "error connecting to api.github.com: connection reset by peer" >&2
exit 1
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "persistent failure -> exit 3 DEGRADED" || bad "expected exit 3, got $rc" "$o"
check "names the probe that failed" "open-PR probe" "$o"
check "names the repo that failed" "acme/repo-x" "$o"

echo
echo "T25: a 404 costs exactly ONE call — a dead remote must not pay the retry tax"
export GH_ATTEMPT_FILE="$SCRATCH/gh-404-attempts"; : > "$GH_ATTEMPT_FILE"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo x >> "$GH_ATTEMPT_FILE"
echo "gh: Not Found (HTTP 404)" >&2
exit 1
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" \
    COLLISION_CHECK_RETIRED_REMOTES="acme/repo-x" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $(wc -l < "$GH_ATTEMPT_FILE" | tr -d ' ') -eq 1 ]] \
  && ok "404 short-circuits after 1 attempt" \
  || bad "404 retried $(wc -l < "$GH_ATTEMPT_FILE" | tr -d ' ') time(s) — wasted quota"

echo
echo "T26: probe E stays field-selected — guards the measured REST regression"
# Not style policing. REST (`gh api repos/X/pulls`) cannot select fields, so it
# returns full PR objects; on a very large public repo that TIMES OUT at 30s
# where this 3-field GraphQL call returns in seconds. A future "move off the GraphQL quota" refactor
# that swaps this for REST reintroduces the DEGRADED storm via
# timeouts — which is the exact bug T22-T24 exist to prevent.
export GH_CALL_LOG="$SCRATCH/gh-calls-e.log"; : > "$GH_CALL_LOG"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
grep -Fq -- '--json number,title,headRefName' "$GH_CALL_LOG" \
  && ok "probe E selects exactly the 3 fields the token filter needs" \
  || bad "probe E is not field-selected: $(cat "$GH_CALL_LOG")"
grep -q -- '--paginate' "$GH_CALL_LOG" \
  && bad "unbounded --paginate reintroduced: $(cat "$GH_CALL_LOG")" \
  || ok "no unbounded --paginate walk"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"

# --- GraphQL SECONDARY rate limit -> bounded REST fallback -------
# GraphQL's secondary limiter is a distinct failure from quota exhaustion --
# `gh api rate_limit` can show headroom while `gh pr list` still fails with
# this exact string (measured live, ticket comments 2026-09-02/03). Retrying
# into it wastes the budget; degrading trains the fleet to ignore exit 3. The
# fallback is scoped to own-org repos ONLY (COLLISION_CHECK_OWN_ORGS) -- REST
# stays untouched for everyone else (T24 unmodified, above).
mkdir -p "$SCRATCH/own-org-clones/repo-own"
git init -q -b main "$SCRATCH/own-org-clones/repo-own"
git -C "$SCRATCH/own-org-clones/repo-own" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$SCRATCH/own-org-clones/repo-own" remote add origin https://github.com/example-org/repo-own.git

echo
echo "T26b: own-org secondary-limit hit falls back to bounded REST, not DEGRADE"
export GH_CALL_LOG="$SCRATCH/gh-calls-26b.log"; : > "$GH_CALL_LOG"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
case "$*" in
  *"pr list"*)
    echo "GraphQL: API rate limit already exceeded for user ID 12345" >&2
    exit 1 ;;
  *"api "*)
    printf '777\tfallback pr\tme/eng-9999-rest-fallback\n'
    exit 0 ;;
esac
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/own-org-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "own-org secondary-limit hit -> exit 1 (collision via REST)" \
  || bad "expected exit 1, got $rc" "$o"
check "reports the PR the REST fallback found" "777" "$o"
grep -Fq -- 'api repos/example-org/repo-own/pulls' "$GH_CALL_LOG" \
  && ok "REST fallback call was made" \
  || bad "no REST fallback call in log: $(cat "$GH_CALL_LOG")"

echo
echo "T26c: third-party remotes are skipped before any GitHub call"
export GH_CALL_LOG="$SCRATCH/gh-calls-26c.log"; : > "$GH_CALL_LOG"
o=$(COLLISION_CHECK_OWN_ORGS=example-org:example-user COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "third-party remote -> exit 0 (unconfigured)" \
  || bad "expected exit 0, got $rc" "$o"
check "names the skipped remote" "acme/repo-x: skipped (not an own-org remote)" "$o"
[[ ! -s "$GH_CALL_LOG" ]] && ok "no GitHub call for a non-own-org repo" \
  || bad "unexpected GitHub call: $(cat "$GH_CALL_LOG")"

echo
echo "T26d: own-org repo where BOTH GraphQL and the REST fallback fail -> fail-closed"
export GH_CALL_LOG="$SCRATCH/gh-calls-26d.log"; : > "$GH_CALL_LOG"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
case "$*" in
  *"pr list"*)
    echo "GraphQL: API rate limit already exceeded for user ID 12345" >&2
    exit 1 ;;
  *"api "*)
    echo "gh: some transient REST error" >&2
    exit 1 ;;
esac
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/own-org-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "both GraphQL and REST failing -> exit 3 DEGRADED, not clear" \
  || bad "expected exit 3 (fail-closed), got $rc" "$o"
absent "must NOT report clear when both probes failed" "CLEAR —" "$o"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"

# --- A full
# capped 3rd REST page was being accepted as a COMPLETE result. 3 full 100-row
# pages hit the cap without ever seeing the "short page -- done" signal, so a
# matching PR sitting on an unfetched 4th page was silently invisible and the
# run reported CLEAR. Must DEGRADE instead -- "could not check" must never
# read as "checked and clear" (this script's own header invariant). ---------
echo
echo "T26e: own-org REST hits the 3-page cap with every page FULL -> DEGRADED, not a false clear"
export GH_CALL_LOG="$SCRATCH/gh-calls-26e.log"; : > "$GH_CALL_LOG"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
case "$*" in
  *"pr list"*)
    echo "GraphQL: API rate limit already exceeded for user ID 12345" >&2
    exit 1 ;;
  *"&page=1 "*)
    for i in $(seq 1 100); do printf '%d\tpr %d\tchore/branch-%d\n' "$i" "$i" "$i"; done
    exit 0 ;;
  *"&page=2 "*)
    for i in $(seq 101 200); do printf '%d\tpr %d\tchore/branch-%d\n' "$i" "$i" "$i"; done
    exit 0 ;;
  *"&page=3 "*)
    for i in $(seq 201 300); do printf '%d\tpr %d\tchore/branch-%d\n' "$i" "$i" "$i"; done
    exit 0 ;;
esac
STUB
chmod +x "$SCRATCH/bin/gh"
# None of the 300 rows above carry the ENG-9999 token -- a hypothetical match
# sits on a 4th page this probe is bounded to never fetch.
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/own-org-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "3 full pages at the cap -> exit 3 DEGRADED (not a false clear)" \
  || bad "expected exit 3, got $rc" "$o"
check "the degrade message names the probe" "open-PR probe" "$o"
absent "must NOT report clear when the cap was hit full" "CLEAR —" "$o"
grep -Fq -- 'page=3' "$GH_CALL_LOG" \
  && ok "the probe actually paged out to the cap (3 pages fetched)" \
  || bad "expected 3 pages of REST calls in the log: $(cat "$GH_CALL_LOG")"
absent "never fetches past the 3-page cap (no quota-storm regression)" 'page=4' "$GH_CALL_LOG"

echo
echo "T26e2: a match WITHIN the first 3 pages is still found -- the DEGRADE fix doesn't cost coverage"
export GH_CALL_LOG="$SCRATCH/gh-calls-26e2.log"; : > "$GH_CALL_LOG"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
case "$*" in
  *"pr list"*)
    echo "GraphQL: API rate limit already exceeded for user ID 12345" >&2
    exit 1 ;;
  *"&page=1 "*)
    for i in $(seq 1 100); do printf '%d\tpr %d\tchore/branch-%d\n' "$i" "$i" "$i"; done
    exit 0 ;;
  *"&page=2 "*)
    # 150th open PR is the caller's own ticket -- token lands mid-cap, not on a short page.
    for i in $(seq 101 200); do
      if [ "$i" -eq 150 ]; then
        printf '150\town-org fix (eng-9999)\tme/eng-9999-mid-cap-match\n'
      else
        printf '%d\tpr %d\tchore/branch-%d\n' "$i" "$i" "$i"
      fi
    done
    exit 0 ;;
  *"&page=3 "*)
    for i in $(seq 201 300); do printf '%d\tpr %d\tchore/branch-%d\n' "$i" "$i" "$i"; done
    exit 0 ;;
esac
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/own-org-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "match on page 2 of 3 (cap still hit) -> exit 1 (found, coverage unchanged)" \
  || bad "expected exit 1, got $rc" "$o"
check "reports the PR found within the capped pages" "150" "$o"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"

# --- probe D reliability ----------------------------------------------------
# Probe E got a retry because a 1-in-N network blip was voiding
# whole runs. Probe D fans ls-remote across every clone with NO retry, so it
# has the identical defect at a wider blast radius: ANY one flaky remote
# voids the verdict for a ticket nobody is building.
#
# A custom remote helper (git-remote-<transport>) is the probe-D analogue of
# the gh stub: `origin = flaky::x` makes git exec `git-remote-flaky`, which we
# control. No network.
mk_flaky_helper() { # mk_flaky_helper <fail-this-many-attempts-first>
  cat > "$SCRATCH/bin/git-remote-flaky" <<STUB
#!/usr/bin/env bash
echo x >> "\$FLAKY_ATTEMPT_FILE"
if [ "\$(wc -l < "\$FLAKY_ATTEMPT_FILE" | tr -d ' ')" -le $1 ]; then
  echo "fatal: unable to access remote: connection reset by peer" >&2
  exit 1
fi
while IFS= read -r line; do
  case "\$line" in
    capabilities) printf 'fetch\n\n' ;;
    list)         printf '0000000000000000000000000000000000000000 refs/heads/main\n\n' ;;
    *)            exit 0 ;;
  esac
done
STUB
  chmod +x "$SCRATCH/bin/git-remote-flaky"
}
mkdir -p "$SCRATCH/d-clones/repo-y"
git init -q -b main "$SCRATCH/d-clones/repo-y"
git -C "$SCRATCH/d-clones/repo-y" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$SCRATCH/d-clones/repo-y" remote add origin "flaky::x"

echo
echo "T27: a TRANSIENT probe-D failure is retried, not degraded"
export FLAKY_ATTEMPT_FILE="$SCRATCH/flaky-attempts"; : > "$FLAKY_ATTEMPT_FILE"
mk_flaky_helper 1
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/d-clones" COLLISION_CHECK_SKIP_REMOTE= \
    "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "transient failure retried -> exit 0" || bad "expected exit 0, got $rc" "$o"
absent "no DEGRADED after a successful retry" "DEGRADED" "$o"
[[ $(wc -l < "$FLAKY_ATTEMPT_FILE" | tr -d ' ') -ge 2 ]] \
  && ok "probe D actually retried (>=2 attempts)" \
  || bad "no retry happened: $(wc -l < "$FLAKY_ATTEMPT_FILE") attempt(s)"

echo
echo "T28: a PERSISTENT probe-D failure still DEGRADES (fail-closed preserved)"
: > "$FLAKY_ATTEMPT_FILE"
mk_flaky_helper 99
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/d-clones" COLLISION_CHECK_SKIP_REMOTE= \
    "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "persistent failure -> exit 3 DEGRADED" || bad "expected exit 3, got $rc" "$o"
check "names the remote-branch probe" "remote-branch probe" "$o"
check "names the clone it could not reach" "repo-y" "$o"
absent "the unreachable clone is not itself reported as a branch hit" "remote branch" "$o"
rm -f "$SCRATCH/bin/git-remote-flaky"

echo
# --- --repo scope for paired-PR (two-repo) tickets ----------------
# One ticket, one gitBranchName, two repos: the second repo's worker sees the
# FIRST repo's worktree and cannot tell its own sibling from a competitor.
# --repo says "the other clone's hit is not mine to halt on" -- and nothing
# else about the gate may move, which is what (b) and (c) pin.
mkdir -p "$SCRATCH/p-clones"
psvc="$SCRATCH/p-clones/repo-svc"; prmt="$SCRATCH/p-clones/repo-rmt"
for p in "$psvc" "$prmt"; do
  git init -q -b main "$p"
  git -C "$p" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
done
# the sibling worker's worktree: OTHER repo, same ticket, same branch name
mkwt "$prmt" "$SCRATCH/repo-rmt.eng-6508" me/eng-6508-paired
PSCOPE=(env COLLISION_CHECK_ROOTS="$SCRATCH/p-clones")

echo
echo "T29a: --repo -- a SIBLING-repo worktree is downgraded to informational, exit 0"
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6508 --repo repo-svc 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "exit 0 (not a collision for this repo)" || bad "expected exit 0, got $rc" "$o"
check  "prints the informational marker"   "ℹ" "$o"
check  "still names the sibling worktree"  "repo-rmt.eng-6508" "$o"
check  "labels it a sibling-repo hit"      "sibling-repo hit" "$o"
check  "still demands the caller verify it" "VERIFY it is your own worker" "$o"
absent "does NOT declare a collision"      "COLLISION —" "$o"

echo
echo "T29b: --repo -- a SAME-repo worktree is still a collision, exit 1"
mkwt "$psvc" "$SCRATCH/repo-svc.eng-6509" me/eng-6509-own
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6509 --repo repo-svc 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (collision inside the named repo)" || bad "expected exit 1, got $rc" "$o"
check  "names the colliding worktree" "repo-svc.eng-6509" "$o"
check  "still says COLLISION"         "COLLISION —" "$o"
absent "not downgraded"               "sibling-repo hit" "$o"

echo
echo "T29c: regression pin -- with NO --repo, the cross-repo hit is exit 1 as before"
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6508 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (fail-closed default unchanged)" || bad "expected exit 1, got $rc" "$o"
check  "reports the cross-repo worktree as a collision" "repo-rmt.eng-6508" "$o"
absent "no informational downgrade without the flag"    "ℹ" "$o"

echo
echo "T29d: --repo scopes probe C (loose local branches) the same way"
git -C "$prmt" branch me/eng-6510-loose
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6510 --repo repo-svc 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "sibling-repo loose branch -> exit 0" || bad "expected exit 0, got $rc" "$o"
check "names the sibling branch" "me/eng-6510-loose" "$o"
check "downgraded, not fatal"    "ℹ" "$o"
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6510 --repo repo-rmt 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "same-repo loose branch -> exit 1" || bad "expected exit 1, got $rc" "$o"

echo
echo "T29e: a --repo that names NO scanned clone must not silently disarm probes B/C"
# The dangerous failure: a typo'd scope makes every clone 'a different repo',
# so every worktree hit downgrades and the gate reports clear. It must instead
# drop the scope (hits stay fatal) AND degrade.
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6510 --repo repo-svc-typo 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "unresolvable scope still reports the hit (exit 1)" || bad "expected exit 1, got $rc" "$o"
check  "names the unresolvable scope" "no scanned clone has that name" "$o"
absent "the hit was not downgraded"   "sibling-repo hit" "$o"
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-9999 --repo repo-svc-typo 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "unresolvable scope alone -> exit 3 DEGRADED" || bad "expected exit 3, got $rc" "$o"
absent "does NOT claim CLEAR" "CLEAR —" "$o"

echo
echo "T29f: --repo=<name> is accepted; a bare --repo or an unknown flag exits 2"
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6508 --repo=repo-svc 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "--repo=<name> form works" || bad "expected exit 0, got $rc" "$o"
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6508 --repo 2>&1); rc=$?
[[ $rc -eq 2 ]] && ok "--repo with no value -> exit 2" || bad "expected exit 2, got $rc" "$o"
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6508 --bogus 2>&1); rc=$?
[[ $rc -eq 2 ]] && ok "unknown flag -> exit 2" || bad "expected exit 2, got $rc" "$o"
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6508 ENG-6509 2>&1); rc=$?
[[ $rc -eq 2 ]] && ok "two ticket ids -> exit 2" || bad "expected exit 2, got $rc" "$o"
check "usage names the flag" "--repo <name>" "$o"

echo
echo "T29g: --repo also resolves via the origin slug, not just the directory name"
# A clone directory name and its repo name can differ (svc vs svc-renamed), so
# a caller passing the repo name must still scope correctly.
git -C "$psvc" remote add origin https://github.com/acme/svc-renamed.git
# --- the caller is not a collision with itself --------------------
# A builder that ran linear-startwork.sh (Linear now In Progress, assigned, with
# its own PR attached) and holds the only worktree/branch/PR must get exit 0.
# Everything that is NOT the caller's must still be exit 1.
mkdir -p "$SCRATCH/s-clones"
SC="$SCRATCH/s-clones/repo-self"
git init -q -b main "$SC"
git -C "$SC" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$SC" remote add origin https://github.com/x/repo-self.git
SELFWT="$SCRATCH/repo-self.eng-6600"
git -C "$SC" worktree add -q -b me/eng-6600-thing "$SELFWT" main
# probe A: exactly what startwork leaves behind -- started, assigned, PR linked
cat > "$SCRATCH/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo '{"data":{"issues":{"nodes":[{"identifier":"ENG-6600","state":{"name":"In Progress","type":"started"},"assignee":{"displayName":"Test User"},"attachments":{"nodes":[{"url":"https://github.com/x/repo-self/pull/42"}]}}]}}}'
STUB
chmod +x "$SCRATCH/bin/curl"
# probe E: the only open PR is the caller's own branch
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '42\tfix: the thing (ENG-6600)\tme/eng-6600-thing\n'
STUB
chmod +x "$SCRATCH/bin/gh"
selfrun() { COLLISION_CHECK_ROOTS="$SCRATCH/s-clones" COLLISION_CHECK_SELF_DIR="$SELFWT" \
            COLLISION_CHECK_SKIP_LINEAR='' LINEAR_API_KEY=stub LINEAR_DEV_TEAM_ID=team-fixture-id "$SCRIPT" ENG-6600 2>&1; }

echo
echo "T27: self acceptance -- own startwork flip + own worktree/branch/PR is exit 0"
o=$(selfrun); rc=$?
[[ $rc -eq 0 ]] && ok "exit 0 (self, not a collision)" || bad "expected exit 0, got $rc" "$o"
check  "the Linear hit is still PRINTED, not hidden" "In Progress" "$o"
check  "the verdict names it as self"                "SELF —" "$o"
check  "the caller is identified in the output"      "repo-self.eng-6600" "$o"
check  "the caller's own PR is marked self"          "self: open PR" "$o"
absent "does NOT claim a collision"                  "COLLISION —" "$o"

echo
echo "T28: a SECOND worktree for the same ticket is STILL exit 1, self or not"
git -C "$SC" worktree add -q -b me/eng-6600-second "$SCRATCH/repo-self.eng-6600-second" main
o=$(selfrun); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (real collision survives)" || bad "expected exit 1, got $rc" "$o"
check  "names the OTHER worktree"      "repo-self.eng-6600-second" "$o"
check  "still marks the caller's own"  "self: live worktree" "$o"
git -C "$SC" worktree remove --force "$SCRATCH/repo-self.eng-6600-second"
git -C "$SC" branch -q -D me/eng-6600-second

echo
echo "T33: an open PR on a DIFFERENT branch of the same repo is STILL exit 1"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '42\tfix: the thing (ENG-6600)\tme/eng-6600-thing\n'
printf '43\tsomeone else (ENG-6600)\tfeat/eng-6600-other-agent\n'
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(selfrun); rc=$?
[[ $rc -eq 1 ]] && ok "exit 1 (foreign PR survives)" || bad "expected exit 1, got $rc" "$o"
check "names the foreign PR" "43" "$o"

echo
echo "T34: the SAME Linear state, from a caller that is a stranger to the ticket,"
echo "     is unchanged -- exit 1 (self-awareness must not fail open)"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"
o=$("${PSCOPE[@]}" "$SCRIPT" ENG-6509 --repo svc-renamed 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "origin-slug name scopes to the right clone (exit 1)" || bad "expected exit 1, got $rc" "$o"
absent "the same-repo hit was not downgraded" "sibling-repo hit" "$o"
absent "origin slug actually resolved the scope" "no scanned clone has that name" "$o"
git -C "$psvc" remote remove origin
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/s-clones" COLLISION_CHECK_SELF_DIR="$SCRATCH" \
    COLLISION_CHECK_SKIP_LINEAR='' LINEAR_API_KEY=stub LINEAR_DEV_TEAM_ID=team-fixture-id "$SCRIPT" ENG-6601 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "stranger caller -> exit 1" || bad "expected exit 1, got $rc" "$o"
check  "says COLLISION" "COLLISION —" "$o"
absent "no self claim"  "SELF —" "$o"
rm -f "$SCRATCH/bin/curl"

echo
echo "T35: probe D -- the caller's OWN pushed branch is self; a tip it cannot"
echo "     reach on the same branch name is not"
# insteadOf keeps this offline while still presenting a github.com origin, so
# probe D talks to a bare repo on disk and probe E still resolves a real slug.
mkdir -p "$SCRATCH/sd-clones"
SDBARE="$SCRATCH/origin-sd.git"; git init -q --bare "$SDBARE"
SD="$SCRATCH/sd-clones/repo-sd"
git init -q -b main "$SD"
git -C "$SD" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$SD" remote add origin https://github.com/x/repo-sd.git
git -C "$SD" config "url.$SDBARE.insteadOf" https://github.com/x/repo-sd.git
SDWT="$SCRATCH/repo-sd.eng-6602"
git -C "$SD" worktree add -q -b me/eng-6602-x "$SDWT" main
git -C "$SDWT" push -q origin me/eng-6602-x
sdrun() { COLLISION_CHECK_ROOTS="$SCRATCH/sd-clones" COLLISION_CHECK_SELF_DIR="$SDWT" \
          COLLISION_CHECK_SKIP_REMOTE='' "$SCRIPT" ENG-6602 2>&1; }
o=$(sdrun); rc=$?
[[ $rc -eq 0 ]] && ok "own pushed branch -> exit 0" || bad "expected exit 0, got $rc" "$o"
check "probe D marks it self" "self: remote branch" "$o"

echo "T35b: pushed, then committed more -- the remote tip is an ANCESTOR, still self"
git -C "$SDWT" -c user.email=t@t -c user.name=t commit -q --allow-empty -m more
o=$(sdrun); rc=$?
[[ $rc -eq 0 ]] && ok "local ahead of remote -> exit 0" || bad "expected exit 0, got $rc" "$o"

echo "T35c: a commit we cannot reach on OUR branch name is another agent -- exit 1"
SDSEED="$SCRATCH/sd-seed"
git init -q -b main "$SDSEED"
git -C "$SDSEED" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "foreign work"
git -C "$SDSEED" push -q -f "$SDBARE" HEAD:me/eng-6602-x
o=$(sdrun); rc=$?
[[ $rc -eq 1 ]] && ok "unreachable remote tip -> exit 1" || bad "expected exit 1, got $rc" "$o"
check  "reported as pushed from elsewhere" "pushed from elsewhere" "$o"
absent "not credited to the caller"        "self: remote branch" "$o"

echo
echo "T36: an unrelated caller directory cannot suppress anything -- the identity"
echo "     only counts when the caller's OWN workspace carries the ticket token"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/s-clones" COLLISION_CHECK_SELF_DIR="$SELFWT" "$SCRIPT" ENG-6603 2>&1); rc=$?
absent "no caller identity for a ticket the workspace does not name" "· caller:" "$o"

# --- configuration: unconfigured is DEGRADED, an explicit skip is not -------
echo
echo "T40: a FULLY unconfigured run (no COLLISION_CHECK_*, no LINEAR_*) exits 3, never 0"
o=$(cd "$SCRATCH" && env -i PATH="$PATH" HOME="$SCRATCH" "$SCRIPT" 42 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "exit 3 (degraded) with nothing configured" || bad "expected exit 3, got $rc" "$o"
check  "says DEGRADED"                       "DEGRADED" "$o"
check  "names the missing clone roots"       "COLLISION_CHECK_ROOTS" "$o"
check  "names the missing Linear config"     "LINEAR_API_KEY" "$o"
check  "names the missing own-org list"      "COLLISION_CHECK_OWN_ORGS" "$o"
absent "does NOT claim CLEAR"                "CLEAR" "$o"

echo
echo "T41: COLLISION_CHECK_OWN_ORGS unset -> probe E DEGRADES without calling GitHub"
export GH_CALL_LOG="$SCRATCH/gh-calls-41.log"; : > "$GH_CALL_LOG"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$GH_CALL_LOG"
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"
o=$(env -u COLLISION_CHECK_OWN_ORGS COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 3 ]] && ok "exit 3 (own orgs unconfigured)" || bad "expected exit 3, got $rc" "$o"
check  "names the setting" "COLLISION_CHECK_OWN_ORGS" "$o"
[[ ! -s "$GH_CALL_LOG" ]] && ok "no GitHub call without an own-org list" \
  || bad "unexpected GitHub call: $(cat "$GH_CALL_LOG")"

echo
echo "T42: an EXPLICIT skip is the only opt-out that still exits 0, and it says so"
o=$(env -u COLLISION_CHECK_OWN_ORGS COLLISION_CHECK_ROOTS="$SCRATCH/r-clones" COLLISION_CHECK_SKIP_PRS=1 \
    "$SCRIPT" ENG-9999 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "explicit skip -> exit 0" || bad "expected exit 0, got $rc" "$o"
check "prints the skip line" "open-PR probe: skipped (COLLISION_CHECK_SKIP_PRS set)" "$o"
check "probe A skip is printed too" "Linear state probe: skipped (COLLISION_CHECK_SKIP_LINEAR set)" "$o"
cat > "$SCRATCH/bin/gh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$SCRATCH/bin/gh"

echo
echo "T43: the DEFAULT prefix is dev -- PREFIX-NN, prefixNN and a bare number all resolve"
mkdir -p "$SCRATCH/dp-clones"
dp=$(git init -q -b main "$SCRATCH/dp-clones/repo-dp" && echo "$SCRATCH/dp-clones/repo-dp")
git -C "$dp" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$dp" worktree add -q -b wip/x "$SCRATCH/repo-dp.dev-42" main
for arg in DEV-42 dev42 42; do
  o=$(env -u LINEAR_BRANCH_PREFIX COLLISION_CHECK_ROOTS="$SCRATCH/dp-clones" "$SCRIPT" "$arg" 2>&1); rc=$?
  [[ $rc -eq 1 ]] && ok "$arg -> exit 1 (collision)" || bad "$arg: expected exit 1, got $rc" "$o"
done
check "the verdict names the ticket with the default prefix" "DEV-42 is already in flight" "$o"
o=$(env -u LINEAR_BRANCH_PREFIX COLLISION_CHECK_ROOTS="$SCRATCH/dp-clones" "$SCRIPT" ENG-42 2>&1); rc=$?
[[ $rc -eq 2 ]] && ok "a different prefix than configured -> exit 2" || bad "expected exit 2, got $rc" "$o"

echo
echo "T44: an unusable LINEAR_BRANCH_PREFIX is a config error (exit 2), not a guess"
o=$(LINEAR_BRANCH_PREFIX='a.b' "$SCRIPT" 42 2>&1); rc=$?
[[ $rc -eq 2 ]] && ok "regex-unsafe prefix -> exit 2" || bad "expected exit 2, got $rc" "$o"
check "names the setting" "LINEAR_BRANCH_PREFIX" "$o"

echo
echo "T45: a COLLISION_CHECK_ROOTS entry that is ITSELF a clone is scanned"
o=$(COLLISION_CHECK_ROOTS="$dp" LINEAR_BRANCH_PREFIX=dev "$SCRIPT" DEV-42 2>&1); rc=$?
[[ $rc -eq 1 ]] && ok "root-is-a-clone -> exit 1 (collision)" || bad "expected exit 1, got $rc" "$o"
check "names the worktree" "repo-dp.dev-42" "$o"

echo
echo "T45b: a literal ~ or \$HOME root (as a settings.json env value arrives) is expanded"
for root in '~/dp-clones' '$HOME/dp-clones'; do
  o=$(HOME="$SCRATCH" COLLISION_CHECK_ROOTS="$root" LINEAR_BRANCH_PREFIX=dev "$SCRIPT" DEV-42 2>&1); rc=$?
  [[ $rc -eq 1 ]] && ok "$root -> exit 1 (scanned)" || bad "$root: expected exit 1, got $rc" "$o"
done

echo
echo "T46: probe D matches the token in the REF only -- a hex-letter prefix cannot match a sha"
# Prefix `abc` is spelled in hex digits, and this remote's main tip is abc42f...:
# an unscoped grep over `<sha>\t<ref>` would report `main` as a collision.
cat > "$SCRATCH/bin/git-remote-fixture" <<'STUB'
#!/usr/bin/env bash
while IFS= read -r line; do
  case "$line" in
    capabilities) printf 'fetch\n\n' ;;
    list)         printf 'abc42f0000000000000000000000000000000000 refs/heads/main\n\n' ;;
    *)            exit 0 ;;
  esac
done
STUB
chmod +x "$SCRATCH/bin/git-remote-fixture"
mkdir -p "$SCRATCH/hx-clones"
git init -q -b main "$SCRATCH/hx-clones/repo-hx"
git -C "$SCRATCH/hx-clones/repo-hx" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$SCRATCH/hx-clones/repo-hx" remote add origin "fixture::x"
o=$(COLLISION_CHECK_ROOTS="$SCRATCH/hx-clones" COLLISION_CHECK_SKIP_REMOTE= LINEAR_BRANCH_PREFIX=abc \
    "$SCRIPT" ABC-42 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok "sha containing the token is not a hit -> exit 0" || bad "expected exit 0, got $rc" "$o"
absent "main is not reported" "remote branch 'main'" "$o"
rm -f "$SCRATCH/bin/git-remote-fixture"

echo
echo "collision-check: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
