#!/bin/bash
# The three release scenarios: fresh install, upgrade from the previous release,
# and the lite (CPU-only, cloud-ASR) deployment.
#
# This is the stage that proves the release, and the only one that needs the live
# stack STOPPED — the scenarios run under the installer's stock container names
# and ports 5173-5180 by design, so they exercise what a real user gets.
#
# It never stops the stack for you. Taking someone's deployment down is their
# call, so this refuses and prints the command.
#
# Scenario C exists because `docker-build-push.sh all` BUILDS the lite image and a
# release would PUBLISH it, while nothing under scripts/release/ ever ran it — so
# lite shipped with zero release-time functional evidence (issue #667). That matters
# more than it sounds: the full/CUDA image publishes no arm64 manifest, so
# arm64_deployment_preflight() defaults every arm64 host to DEPLOYMENT_MODE=lite.
# Lite is the ONLY deployment those users can install, and it was the one shape the
# rehearsal never exercised.
#
# It publishes nothing: test-lite-mode.sh builds the lite image locally and pins
# pull_policy: never, so it needs no Docker Hub artifact and creates none.
#
# Exit: 0 all scenarios passed · 1 a scenario failed · 3 live stack still running ·
#       4 operator declined a scenario's confirmation prompt (nothing ran —
#       scripts/release.sh records this as ledger status=aborted, never
#       =failed, and --force-rehearse does not apply to it: re-run and answer
#       the prompt, or pass --yes)

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
# Set by release.sh's patch_prepare() (scripts/release/patch-lib.sh) when
# --patch resolved a waivable patch delta. Empty on every other invocation, so
# the normal path below is completely unaffected.
PATCH_SKIP_REASON="${OT_PATCH_SKIP_REASON:-}"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

# Severities come from release-criteria.yaml; the pass/fail outcome comes from here. See
# criteria-lib.sh — the contract is bidirectional, so an id recorded but not declared, or
# declared but not recorded, is exit 2 (misuse of the pipeline's own wiring, never a
# statement about the release). The 0/1/3/4 verdict contract below is unchanged.
# Exported: the consumer of this variable now lives in criteria-lib.sh, across a file
# boundary, so a bare assignment would be invisible to both shellcheck and a reader.
export STAGE_ID=rehearse
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

# Filter by compose project label, not a name prefix -- see 10-preflight.sh's
# live-stack check for why a naive prefix match false-positives on unrelated
# containers (e.g. "opentranscribe-homepage").
#
# ⚠️ BOTH project labels, not just one (issue #783 finding N1) -- reusing the SAME
# ${OPENTR_STOP_PROJECT_LABEL:-opentranscribe} / ${OPENTR_STOP_PROJECT_LABEL_ALT:-transcribe-app}
# mechanism 10-preflight.sh's live-stack check already uses, not a second definition of
# "what are the two legitimate project names" to drift out of sync. A repo clone's
# compose project defaults to the DIRECTORY name, so `./opentr.sh start dev` from this
# checkout runs under `transcribe-app` while a curl/one-liner install runs under
# `opentranscribe`. Checking only the latter recorded `live-stack-stopped` PASS with the
# dev stack fully up (measured 2026-09-06: 0 matches for `opentranscribe`, 20 for
# `transcribe-app`), and this stage then died several minutes later in
# gr_check_ports_free with a message about ports instead of the real cause.
#
# Captured rather than piped into `grep -q`: under `set -o pipefail`, grep -q closes the
# pipe on its first match and `docker ps` can die with SIGPIPE, turning "the stack IS up"
# into a reported all-clear.
live_stack_names="$( {
    docker ps --filter "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL:-opentranscribe}" --format '{{.Names}}'
    docker ps --filter "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL_ALT:-transcribe-app}" --format '{{.Names}}'
} | sort -u)"
if [[ -n "$live_stack_names" ]]; then
    record live-stack-stopped fail "the live stack is running" "./opentr.sh stop"
    echo -e "${RED}The live stack is running; the scenarios cannot run beside it.${NC}" >&2
    echo -e "${YELLOW}  ./opentr.sh stop     # preserves all data${NC}" >&2
    echo -e "${YELLOW}  (restart afterwards with ./opentr.sh start dev)${NC}" >&2
    # Exit 3 (precondition unmet) as before. Deliberately BEFORE
    # criteria_assert_all_checked: nothing ran, so the scenario criteria are genuinely
    # unchecked, and reporting that as wiring drift would bury the real reason.
    exit 3
fi
record live-stack-stopped pass

fresh_rc=0; upgrade_rc=0; lite_rc=0

# Names still held by a previous scenario's stack, as a single string ("" = none).
#
# Deliberately NOT `docker ps ... | grep -q .`: this script runs under `set -o pipefail`,
# and `grep -q` closes the pipe on its first match, so `docker ps` can die with SIGPIPE and
# turn a stack that IS still up into a reported "all clear". Capturing the output has no
# such failure mode.
stock_containers() {
    docker ps -a --filter 'label=com.docker.compose.project=opentranscribe' --format '{{.Names}}'
}

# Every scenario ends with its stack UP, deliberately: run standalone, you want to poke at
# the thing you just installed. But all three bind the same stock container names and ports
# 5173-5180, so the next scenario's guardrails refuse to start while the previous stack
# exists — meaning this stage could never get past the first one.
#
# So the ORCHESTRATOR tears each one down before the next. That is different from stopping
# the operator's live deployment, which this stage still refuses to do: these are containers
# this stage created moments ago.
#
# Returns 3 (precondition unmet) when the names do not release, so the caller can skip the
# next scenario rather than watch it fail for a reason that is not about the release.
teardown_scenario() {
    local label="$1"; shift
    echo -e "${BLUE}Tearing down ${label}'s stack so the next scenario can bind its ports${NC}" >&2
    "$@" --cleanup --yes || {
        echo -e "${YELLOW}${label} cleanup reported a problem; continuing${NC}" >&2
    }

    # Cleanup is asynchronous at the docker level — a container in 'Removing' still holds its
    # port bindings, and the next preflight would see a stale name. Wait for the names to
    # actually disappear rather than racing them.
    local _
    for _ in $(seq 1 30); do
        [[ -z "$(stock_containers)" ]] && break
        sleep 2
    done
    if [[ -n "$(stock_containers)" ]]; then
        echo -e "${RED}${label}'s containers did not go away; the next scenario cannot start${NC}" >&2
        docker ps -a --filter 'label=com.docker.compose.project=opentranscribe' --format '  {{.Names}}\t{{.Status}}' >&2
        return 3
    fi
    return 0
}

if [[ -n "$PATCH_SKIP_REASON" ]]; then
    # --patch waived the rehearsal (scripts/release/patch-lib.sh decided the
    # diff touches none of PATCH_REHEARSAL_TRIGGERS). fresh_rc/upgrade_rc/
    # lite_rc stay 0 from their init above, so the aggregation below is
    # completely unaffected — this is a WAIVER, not three scenarios that
    # happened to pass.
    echo -e "${YELLOW}--patch: rehearsal scenarios WAIVED — ${PATCH_SKIP_REASON}${NC}" >&2
else
    # ---------------------------------------------------------------- branch under test
    #
    # ⚠️ INSIDE the non-waived branch on purpose. A `--patch` waiver runs no scenario and
    # therefore installs nothing, so demanding a pushed branch there would refuse a
    # rehearsal that was never going to download anything — which is exactly what it did
    # when this block sat above the `if` (3 tests in test_rehearse_patch_waiver.py).
    #
    # Scenarios A and C install through `setup-opentranscribe.sh`, which DOWNLOADS every
    # release-manifest.txt file (docker-compose.yml, docker-compose.lite.yml,
    # opentranscribe.sh, ...) from $OPENTRANSCRIBE_BRANCH. Both scenario scripts default
    # TO_BRANCH to `master`, and this orchestrator used to leave that alone — so rehearsing an
    # unreleased version from a feature branch installed MASTER's deployment files, and every
    # compose or installer change in the release went unrehearsed. The stage that exists to
    # prove the release proved the previous one.
    #
    # Measured 2026-09-08: a /ml-models mount added for celery-cpu-worker, committed AND pushed,
    # was absent from the staged install tree (7 occurrences in the working tree, 4 on
    # origin/master, 2 staged) and `docker exec ...-celery-cpu-worker ls /ml-models` reported
    # "No such file or directory" after the fix. Worse, a standalone lite run passed 17/17 and
    # was credited to that fix; it had never been installed.
    #
    # ⚠️ This does NOT change where the INSTALLER script comes from, and must not. Root
    # CLAUDE.md's install-path section is explicit that setup-opentranscribe.sh legitimately
    # comes from the default branch while artifacts come from the resolved release ref -- a real
    # `curl | bash` user has exactly those two sources, which is why verify-install-paths.sh
    # exists. Only the manifest files were wrong.
    #
    # ⚠️ An unpushed branch REFUSES rather than falling back. The installer downloads, so a
    # branch the remote has never seen cannot be installed; quietly substituting master would
    # produce a green rehearsal for code that was never exercised -- the same defect wearing a
    # different hat.
    if [[ -z "${TO_BRANCH:-}" ]]; then
        TO_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
        if [[ -z "$TO_BRANCH" || "$TO_BRANCH" == "HEAD" ]]; then
            echo -e "${RED}✗ cannot determine the branch under test (detached HEAD?).${NC}" >&2
            echo -e "${RED}  Set TO_BRANCH=<branch> explicitly; rehearsing an unknown tree is${NC}" >&2
            echo -e "${RED}  worse than not rehearsing.${NC}" >&2
            exit 3
        fi
    fi
    # ⚠️ Only ask when there is something to ask. A tree with no `origin` (a tarball, or the
    # synthetic repo test_rehearse_patch_waiver.py builds) cannot be "accidentally rehearsing
    # master instead of the branch" -- that notion needs a remote to be meaningful -- and the
    # installer fails loudly by itself if the ref does not resolve. Refusing there would make
    # the stage unrunnable outside a clone while protecting against nothing. A real release
    # checkout always has origin, so the refusal below still fires where the hazard exists.
    if ! git -C "$REPO_ROOT" remote get-url origin >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠ no 'origin' remote: cannot verify that '${TO_BRANCH}' is${NC}" >&2
        echo -e "${YELLOW}  pushed. The installer will fail loudly if it does not resolve.${NC}" >&2
    elif ! git -C "$REPO_ROOT" ls-remote --exit-code --heads origin "$TO_BRANCH" >/dev/null 2>&1; then
        echo -e "${RED}✗ branch '${TO_BRANCH}' is not on origin.${NC}" >&2
        echo -e "${RED}  Scenarios A and C install by DOWNLOADING the manifest files from it, so${NC}" >&2
        echo -e "${RED}  an unpushed branch cannot be rehearsed. Push it first:${NC}" >&2
        echo -e "${RED}      git push -u origin ${TO_BRANCH}${NC}" >&2
        exit 3
    fi
    export TO_BRANCH
    echo -e "${BLUE}Rehearsing the deployment files from branch: ${TO_BRANCH}${NC}" >&2

    echo -e "${BLUE}Scenario A — fresh install${NC}" >&2
    ./scripts/release-tests/test-fresh-install.sh --yes || fresh_rc=$?

    teardown_scenario "Scenario A" ./scripts/release-tests/test-fresh-install.sh || upgrade_rc=$?

    if [[ $upgrade_rc -eq 0 ]]; then
        echo -e "${BLUE}Scenario B — upgrade from the previous published release${NC}" >&2
        REQUIRE_PREVIOUS=1 ./scripts/release-tests/test-upgrade.sh --yes || upgrade_rc=$?
    fi

    # Scenario C runs regardless of B's assertion verdict but not through a broken teardown:
    # a failed upgrade is a finding about the upgrade, and lite is an independent deployment
    # shape whose evidence is worth having either way. A stack that would not release its
    # ports, though, means C cannot bind and would fail for an unrelated reason.
    if [[ $upgrade_rc -eq 3 ]]; then
        lite_rc=3
    else
        teardown_scenario "Scenario B" ./scripts/release-tests/test-upgrade.sh || lite_rc=$?
    fi

    if [[ $lite_rc -eq 0 ]]; then
        echo -e "${BLUE}Scenario C — lite (CPU-only) deployment, mocked cloud ASR + mocked LLM${NC}" >&2
        ./scripts/release-tests/test-lite-mode.sh --yes || lite_rc=$?
    fi

    # Scenario C must be torn down too, even though it is the last one.
    #
    # Each scenario script ends with "Stack left running for inspection", which is right when
    # a human runs one by hand and wrong here: the scenarios deliberately bind the standard
    # ports under the stock `opentranscribe-*` names, and `lib/guardrails.sh` REFUSES to start
    # when any such container exists — running or merely stopped. So C's surviving stack does
    # not just leak resources, it makes the NEXT `rehearse` fail its preconditions with exit 3,
    # a "did not run" that reads like an unmet precondition of the release rather than residue
    # from the previous run. A and B were already torn down; only the tail was missing.
    #
    # ⚠️ A teardown problem must NOT rewrite C's verdict. The scenario's own outcome is a fact
    # about the release; failing to clean up afterwards is a fact about this host, and
    # collapsing the two would report a passing lite deployment as a failed one (or vice
    # versa). It is therefore captured separately and warned about, loudly, with the manual
    # command — the same "cleanup reported a problem; continuing" shape `teardown_scenario`
    # already uses for A and B.
    lite_teardown_rc=0
    teardown_scenario "Scenario C" ./scripts/release-tests/test-lite-mode.sh || lite_teardown_rc=$?
    if [[ $lite_teardown_rc -ne 0 ]]; then
        echo -e "${YELLOW}Scenario C's stack did not tear down cleanly (rc=$lite_teardown_rc).${NC}" >&2
        echo -e "${YELLOW}The NEXT rehearsal will refuse to start until it is gone. Clean up with:${NC}" >&2
        echo -e "${YELLOW}    ./scripts/release-tests/test-lite-mode.sh --cleanup --yes${NC}" >&2
    fi
fi

# Record each scenario against its declared criterion.
#
# `not-measured` is the right word for a scenario that never ran because an earlier
# teardown would not release the ports (rc 3): it did not fail, it was never attempted, and
# against a blocking criterion the library stops the stage exactly as a failure would. A
# declined confirmation (rc 4) is the operator's choice, so it is also not-measured rather
# than a failure of the release. A --patch waiver is the SAME "did not run" fact — rc stays
# 0 from the init above (nothing ran to fail), so the waiver reason must override rc, or a
# waived scenario would misreport as a pass.
scenario_outcome() {
    local rc="$1" patch_reason="${2:-}"
    if [[ -n "$patch_reason" ]]; then
        echo not-measured
        return
    fi
    case "$rc" in
        0) echo pass ;;
        3|4) echo not-measured ;;
        *) echo fail ;;
    esac
}
record fresh-install "$(scenario_outcome "$fresh_rc" "$PATCH_SKIP_REASON")" \
    "${PATCH_SKIP_REASON:-test-fresh-install.sh rc=$fresh_rc}" \
    "read the REPORT.md under its TEST_ROOT" "${PATCH_SKIP_REASON:+waived}"
record upgrade-from-previous "$(scenario_outcome "$upgrade_rc" "$PATCH_SKIP_REASON")" \
    "${PATCH_SKIP_REASON:-test-upgrade.sh rc=$upgrade_rc}" \
    "read the REPORT.md under its TEST_ROOT" "${PATCH_SKIP_REASON:+waived}"
record lite-mode "$(scenario_outcome "$lite_rc" "$PATCH_SKIP_REASON")" \
    "${PATCH_SKIP_REASON:-test-lite-mode.sh rc=$lite_rc}" \
    "read the REPORT.md under its TEST_ROOT" "${PATCH_SKIP_REASON:+waived}"

# Both halves of the contract: every declared criterion was recorded above.
criteria_assert_all_checked

# The VERDICT is still this stage's own, not the criteria library's counters.
#
# Preserve the shared exit-code contract rather than flattening everything to 1. An operator
# who declines a scenario's `I UNDERSTAND` prompt has ABORTED (4); a teardown that would not
# release its ports is an unmet PRECONDITION (3). Collapsing both into "gate failed" told
# every caller — release.sh's ledger and scripts/test-matrix.sh's leg 3 alike — that a
# rehearsal had run and failed, when none had run at all. `record` above reports; it
# deliberately does not decide the exit code, so declaring criteria did not change 0/1/3/4.
rc=0
if [[ $fresh_rc -eq 4 || $upgrade_rc -eq 4 || $lite_rc -eq 4 ]]; then
    rc=4
elif [[ $fresh_rc -eq 3 || $upgrade_rc -eq 3 || $lite_rc -eq 3 ]]; then
    rc=3
elif [[ $fresh_rc -ne 0 || $upgrade_rc -ne 0 || $lite_rc -ne 0 ]]; then
    rc=1
fi
[[ $rc -eq 0 ]] && echo -e "${GREEN}all three scenarios passed${NC}" >&2

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"rehearse","version":"%s","status":"%s","criteria":[%s],"next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" \
        "$(criteria_json)" \
        "$([[ $rc -eq 0 ]] && echo '["restart the dev stack, then tag"]' || echo '["read the REPORT.md under each TEST_ROOT"]')"
fi
exit $rc
