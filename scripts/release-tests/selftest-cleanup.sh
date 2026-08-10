#!/bin/bash
# Self-test for the release harness's own cleanup logic (issue #408).
#
# WHY THIS EXISTS
#
# gr_cleanup_owned_stock_resources deletes Docker volumes whose names are
# indistinguishable from a real deployment's (`opentranscribe_postgres_data`).
# The only thing separating "release-test residue" from "the user's database"
# is the ownership stamp and the live-data marker. Code with that much
# consequence and that little margin needs a test that actually runs it, not a
# reading of it.
#
# Everything here operates on the throwaway project name `otselftest`, so no
# real volume is ever in scope even if the logic is wrong.
#
# Usage: ./scripts/release-tests/selftest-cleanup.sh
# Exit:  0 all cases behaved · 1 a case failed

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# guardrails.sh requires these before it will source.
export TEST_SCENARIO="selftest"
export TEST_PROJECT_NAME="ot-reltest-selftest"
export TEST_ROOT="/tmp/ot-reltest-selftest"
export TEST_LABEL="com.opentranscribe.release-test=selftest"
export TEST_PORTS=""

# The two knobs that keep this away from anything real.
export GR_STOCK_PROJECT="otselftest"
export GR_OWNED_STAMP="/tmp/ot-selftest-owned-stamp"

mkdir -p "$TEST_ROOT"

# shellcheck source=lib/guardrails.sh
source "$SCRIPT_DIR/lib/guardrails.sh"
# guardrails installs an EXIT trap for the repo .env check; we test functions
# directly here and do not want it firing on our behalf.
trap - EXIT

PASS=0
FAIL=0
ok()   { echo -e "  \033[0;32mPASS\033[0m  $*"; PASS=$((PASS + 1)); }
bad()  { echo -e "  \033[0;31mFAIL\033[0m  $*"; FAIL=$((FAIL + 1)); }

vol_exists() { docker volume inspect "$1" >/dev/null 2>&1; }

cleanup_all() {
    docker volume rm "${GR_STOCK_PROJECT}_postgres_data" >/dev/null 2>&1
    docker volume rm "${GR_STOCK_PROJECT}_minio_data"    >/dev/null 2>&1
    rm -f "$GR_OWNED_STAMP"
}
trap cleanup_all EXIT

echo "Release-harness cleanup self-test (project: $GR_STOCK_PROJECT)"
echo

# ── Case 1: no ownership stamp => must not remove anything ─────────────────
# This is the case that protects a user who runs --cleanup on a machine whose
# live deployment happens to use named volumes.
echo "1. no ownership stamp"
rm -f "$GR_OWNED_STAMP"
docker volume create "${GR_STOCK_PROJECT}_postgres_data" >/dev/null
gr_cleanup_owned_stock_resources >/dev/null 2>&1
if vol_exists "${GR_STOCK_PROJECT}_postgres_data"; then
    ok "volume survived (no stamp = no authority to delete)"
else
    bad "volume was removed without an ownership stamp"
fi

# ── Case 2: stamped as test-owned => removed ───────────────────────────────
echo "2. stamped as test-owned"
printf 'volume=%s_postgres_data\n' "$GR_STOCK_PROJECT" > "$GR_OWNED_STAMP"
gr_cleanup_owned_stock_resources >/dev/null 2>&1
if vol_exists "${GR_STOCK_PROJECT}_postgres_data"; then
    bad "test-owned volume was left behind (issue #408 regression)"
else
    ok "test-owned volume removed"
fi

# ── Case 3: stamped BUT carries the live-data marker => refused ────────────
# Defense in depth. Even if the stamp is wrong or stale, a volume holding real
# data must survive.
echo "3. stamped but carries .opentranscribe-live-data"
docker volume create "${GR_STOCK_PROJECT}_minio_data" >/dev/null
docker run --rm -v "${GR_STOCK_PROJECT}_minio_data:/v" alpine \
    touch /v/.opentranscribe-live-data >/dev/null 2>&1
printf 'volume=%s_minio_data\n' "$GR_STOCK_PROJECT" > "$GR_OWNED_STAMP"
out="$(gr_cleanup_owned_stock_resources 2>&1)"
if vol_exists "${GR_STOCK_PROJECT}_minio_data"; then
    ok "live-data volume survived despite being stamped"
else
    bad "LIVE-DATA VOLUME WAS DELETED — the marker check is not working"
fi
if grep -q 'live-data marker' <<<"$out"; then
    ok "refusal was reported, not silent"
else
    bad "no refusal message (a silent skip reads as success)"
fi

# ── Case 4: the stamp is consumed, so a second run has no authority ────────
# Prevents a stale stamp from authorising deletions on a later, unrelated run.
echo "4. stamp is consumed after use"
printf 'volume=%s_postgres_data\n' "$GR_STOCK_PROJECT" > "$GR_OWNED_STAMP"
docker volume create "${GR_STOCK_PROJECT}_postgres_data" >/dev/null
gr_cleanup_owned_stock_resources >/dev/null 2>&1
if [[ -f "$GR_OWNED_STAMP" ]]; then
    bad "stamp still present after cleanup (would authorise a later run)"
else
    ok "stamp consumed"
fi

# ── Case 5: the repo .env fingerprint actually detects a change ────────────
# The sentinel is only worth having if it fails when it should.
echo "5. repo .env sentinel"
probe="/tmp/ot-selftest-env-probe"
printf 'A=1\n' > "$probe"
export GR_REPO_ENV="$probe"   # read by the sourced guardrails functions
gr_fingerprint_repo_env >/dev/null 2>&1
trap - EXIT; trap cleanup_all EXIT   # drop the sentinel's own EXIT trap
# Both calls run in a SUBSHELL. gr_assert_repo_env_untouched signals failure
# via gr_die, which calls `exit` — in `if cmd; then` that terminates this
# script rather than taking the else branch, so the negative case silently
# killed the run before the summary printed.
if ( gr_assert_repo_env_untouched ) >/dev/null 2>&1; then
    ok "unchanged .env passes"
else
    bad "unchanged .env reported as modified"
fi
printf 'A=2\n' > "$probe"
if ( gr_assert_repo_env_untouched ) >/dev/null 2>&1; then
    bad "MODIFIED .env passed — the sentinel does not work"
else
    ok "modified .env is caught"
fi
rm -f "$probe"

# ── Case 6: the NAS dataset can never be a cleanup target ─────────────────
# 484 GB of irreplaceable media lives at /mnt/nas/opentranscribe-minio as a
# BIND mount. Two independent reasons it is out of reach, both asserted here
# because "it's a bind mount so we're fine" is the kind of reasoning that stops
# being true after a refactor.
echo "6. protected paths are unreachable by cleanup"
for p in /mnt/nas/opentranscribe-minio /mnt/nvm/opentranscribe /mnt/nas/opentranscribe; do
    if gr_path_inside "$p/some/child" "$p"; then
        ok "cleanup would refuse a TEST_ROOT under $p"
    else
        bad "$p is NOT recognised as protected — cleanup could delete it"
    fi
done

# The allowlist is the second gate: even a path that dodged the protected-path
# check has to be inside a sanctioned test area to be removed.
for bad_root in /mnt/nas/opentranscribe-minio /home /var/lib; do
    case "$(gr_realpath "$bad_root")" in
        /mnt/nvm/opentranscribe-test-runs/*|/tmp/ot-reltest-*)
            bad "$bad_root matches the cleanup allowlist" ;;
        *)
            ok "$bad_root is outside the cleanup allowlist" ;;
    esac
done

# ── Case 7: ownership is DERIVED by prefix, not a hardcoded name list ─────
# The stamp records what existed BEFORE the run. Anything else under the
# project prefix was created by the test. This is what stops volumes like
# pipeline_scratch and transcription-temp accumulating forever just because
# nobody added them to a list.
echo "7. derived ownership (preexisting vs test-created)"
docker volume create "${GR_STOCK_PROJECT}_preexisting_data" >/dev/null
docker volume create "${GR_STOCK_PROJECT}_scratch_made_by_test" >/dev/null
{
    echo "project=${GR_STOCK_PROJECT}"
    echo "preexisting=${GR_STOCK_PROJECT}_preexisting_data"
} > "$GR_OWNED_STAMP"
gr_cleanup_owned_stock_resources >/dev/null 2>&1

if vol_exists "${GR_STOCK_PROJECT}_preexisting_data"; then
    ok "a volume that existed before the run was left alone"
else
    bad "removed a PRE-EXISTING volume — that could be someone's data"
fi
if vol_exists "${GR_STOCK_PROJECT}_scratch_made_by_test"; then
    bad "test-created volume left behind (it was never in any name list)"
else
    ok "test-created volume removed without being named anywhere"
fi
docker volume rm "${GR_STOCK_PROJECT}_preexisting_data" >/dev/null 2>&1

echo
echo "── $PASS passed, $FAIL failed ──"
[[ $FAIL -eq 0 ]]
