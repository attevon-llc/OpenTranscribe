#!/bin/bash
# The four release scenarios: D the model downloader from a cold cache, then A fresh
# install, B upgrade from the previous release, and C the lite (CPU-only, cloud-ASR)
# deployment. D runs first because A seeds its model cache from D's output.
#
# This is the stage that proves the release, and the only one that needs the live
# stack STOPPED — the scenarios run under the installer's stock container names
# and ports 5173-5180 by design, so they exercise what a real user gets.
#
# Taking someone's deployment down stays a HUMAN call — but it is no longer a manual
# two-step. Given approval it stops the stack, rehearses, and restarts it to the state it
# found. Approval is `--yes`, `RELEASE_AUTO_STOP_STACK=true`, or an interactive prompt;
# with none of those and no tty it still refuses and prints the command, so an unattended
# caller can never silently stop a stack. The restart runs from an EXIT/INT/TERM trap, so
# a failed scenario or a Ctrl-C still puts the host back rather than leaving it down.
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
# Scenario D (test-cold-model-download.sh) needs no stopped stack and binds nothing; it is
# here because a fresh install is the moment a real user runs the downloader, and every
# other scenario skips it (unattended install + seeded cache). See that script's header.
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

# `--cold-cache-only` runs Scenario D alone (R7): release.sh starts it right after `build`,
# beside `scan`, and a later full `rehearse` reuses the evidence when it is still valid. It
# stops no stack, records no criteria and exits 0 only when D's own run exited 0.
COLD_ONLY=false
if [[ "${1:-}" == "--cold-cache-only" ]]; then COLD_ONLY=true; shift; fi

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
# shellcheck source=scripts/release/live-stack-lib.sh
source "$SCRIPT_DIR/live-stack-lib.sh"

# Filter by compose project label, not a name prefix -- see 10-preflight.sh's
# live-stack check for why a naive prefix match false-positives on unrelated
# containers (e.g. "opentranscribe-homepage").
#
# ⚠️ BOTH project labels, not just one (issue #783) -- reusing the SAME
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
live_stack_names="$(lsl_live_stack_names)"
# Set when THIS run stopped the stack, so the trap below puts it back exactly as found.
# Never set when the stack was already down: restoring a stack the operator had
# deliberately stopped is its own surprise.
STACK_STOPPED_BY_US=false

#: Tear down whatever scenario stack was mid-flight. Only reached on an INTERRUPTED run —
#: each scenario cleans up after itself on its own normal and failing paths.
#:
#: Without this, a Ctrl-C (or any TERM) leaves the scenario's containers AND its stock-named
#: volumes behind, and the next rehearsal does not merely inherit clutter -- it FAILS, twice
#: over. The containers hold ports 5173-5180, so it dies in gr_check_ports_free with a port
#: message rather than the real cause; and `opentranscribe_*_data` volumes make the guardrail
#: refuse outright, correctly, because "a fresh-install test against these is NOT a fresh
#: install: the database still holds the previous run's credentials". Observed 2026-09-13:
#: one interrupted run cost the NEXT run its entire fresh-install scenario.
#:
#: Scoped BY LABEL, never by name grep -- a bare name grep destroyed an unrelated container
#: on this host once (issue #693). `docker stop`, never kill: same reason the stack stop uses
#: opentr.sh.
#:
#: Volumes are deliberately NOT removed here. The scenarios' own guardrails re-check each one
#: for the `.opentranscribe-live-data` marker before deleting, and that check is not worth
#: reimplementing in a trap handler running under interrupt. Naming them is enough -- the
#: next run's guardrail already refuses and prints the exact remedy.
teardown_scenario_stack_on_interrupt() {
    local stragglers
    stragglers="$(docker ps -q --filter "label=com.opentranscribe.release-test" 2>/dev/null || true)"
    [[ -n "$stragglers" ]] || return 0

    echo -e "${YELLOW}Interrupted mid-scenario — stopping its containers so the next run is not blocked${NC}" >&2
    # xargs rather than an unquoted expansion: the id lists are newline-separated, and
    # splatting a command substitution is fragile (SC2046). `-r` makes an empty list a
    # no-op instead of invoking docker with no arguments.
    printf '%s\n' "$stragglers" | xargs -r docker stop >/dev/null 2>&1 || true
    docker ps -aq --filter "label=com.opentranscribe.release-test" 2>/dev/null \
        | xargs -r docker rm >/dev/null 2>&1 || true

    local leftover_vols
    leftover_vols="$(docker volume ls -q --filter "name=^opentranscribe_" 2>/dev/null || true)"
    if [[ -n "$leftover_vols" ]]; then
        echo -e "${YELLOW}Scenario volumes remain; the next fresh-install run will refuse until they go:${NC}" >&2
        echo "$leftover_vols" | sed 's/^/           /' >&2
        echo -e "${YELLOW}  re-run with OT_RELEASE_TEST_RESET_VOLUMES=1 (it re-checks each for the live-data marker)${NC}" >&2
    fi
}

# D's cache is several GB, written as uid 1000. Removed (evidence kept) once Scenario A has
# seeded from it, and from the EXIT trap on every other path.
purge_cold_cache() {
    [[ -n "$COLD_CACHE_ROOT" && -d "$COLD_CACHE_ROOT/cache" ]] || return 0
    TEST_ROOT="$COLD_CACHE_ROOT" ./scripts/release-tests/test-cold-model-download.sh --purge-cache --yes >/dev/null 2>&1 \
        || echo -e "${YELLOW}could not remove $COLD_CACHE_ROOT/cache — remove it by hand${NC}" >&2
}

restore_live_stack() {
    purge_cold_cache
    # Runs before the dev-stack restart on purpose: the scenario stack binds the SAME stock
    # ports the dev stack wants, so starting dev first would race it.
    teardown_scenario_stack_on_interrupt

    # Deferred restores (RELEASE_DEFER_STACK_RESTORE=1) leave the stack down for `smoke`; see
    # live-stack-lib.sh. A stack the operator stopped by hand is never started here.
    local stopped_by_us="$STACK_STOPPED_BY_US"
    STACK_STOPPED_BY_US=false   # idempotent: EXIT fires after an explicit call
    lsl_restore_after_stage "$VERSION" "$stopped_by_us"
}
# On EXIT so an aborted or failed scenario still puts the host back. INT/TERM too: a
# Ctrl-C mid-rehearsal used to leave the operator's stack down with no hint why.
# INT/TERM must END the run: a handler that returns hands control back to the interrupted
# rehearsal, which would carry on against the stack just torn down. EXIT does the restoring.
trap restore_live_stack EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Scenario D (cold-cache model download). Its criteria are read from the evidence it writes
# under COLD_CACHE_ROOT, via the same oracle the scenario uses, so this stage and the
# scenario's own REPORT.md cannot disagree. An unset COLD_CACHE_ROOT (a --patch waiver) leaves
# every D criterion not-measured, never a pass. A caller-supplied root is honoured but must be
# EMPTY: the scenario refuses a reused root, because a warm cache is not a cold one.
cold_rc=0
COLD_CACHE_ROOT="${COLD_CACHE_ROOT:-}"
COLD_CACHE_ORACLE="$REPO_ROOT/scripts/release-tests/lib/cold-model-download.py"

COLD_STATE_DIR="$REPO_ROOT/.release/${VERSION:-unknown}"
COLD_ROOT_FILE="$COLD_STATE_DIR/cold-cache-root"
COLD_IMAGE_FILE="$COLD_STATE_DIR/cold-cache-image-id"
BACKEND_IMAGE="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend:${VERSION}"

# The image ID Scenario D ran (or would run) against. Empty when docker cannot say, and an
# empty ID is never a match: reuse requires positive proof it is the same bytes.
backend_image_id() { docker image inspect --format '{{.Id}}' "$BACKEND_IMAGE" 2>/dev/null || true; }

# --- BEGIN cold-cache-scenario ---
# Runs Scenario D into COLD_CACHE_ROOT and seeds A only from a cache that passed EVERY D
# criterion: a run whose downloads exited 0 but left weights missing, or whose nltk tree is
# suspect, would hand A the gap. Sets cold_rc and, on all-pass, OT_COLD_CACHE_SEED.
scenario_d_run() {
    echo -e "${BLUE}Scenario D — cold-cache model download (CPU, empty cache, the image under test)${NC}" >&2
    COLD_CACHE_ROOT="${COLD_CACHE_ROOT:-/mnt/nvm/opentranscribe-test-runs/ot-reltest-coldcache-$(date +%Y%m%d-%H%M%S)}"
    TEST_ROOT="$COLD_CACHE_ROOT" ./scripts/release-tests/test-cold-model-download.sh --yes || cold_rc=$?
    local cold_all_pass=true cold_criterion cold_seen
    for cold_criterion in download weights nltk-hardlink; do
        cold_seen="$(python3 "$COLD_CACHE_ORACLE" outcome --root "$COLD_CACHE_ROOT" --criterion "$cold_criterion" || true)"
        [[ "${cold_seen%%$'\t'*}" == "pass" ]] || cold_all_pass=false
    done
    if [[ "$cold_all_pass" == "true" ]]; then
        export OT_COLD_CACHE_SEED="$COLD_CACHE_ROOT/cache"
        echo -e "${GREEN}Scenario A will seed its model cache from Scenario D's cold download${NC}" >&2
    fi
}
# --- END cold-cache-scenario ---

# R7: a `--cold-cache-only` run (release.sh starts it right after `build`) left its root and the
# image ID it ran against under .release/<v>/. The full rehearse reuses that evidence ONLY when
# (1) the recorded image ID equals the current image's, and (2) the oracle passes all three
# criteria. Otherwise it runs D itself, exactly as before. The criteria are still recorded by
# this stage, from the same oracle, so nothing is waived.
cold_evidence_reusable() {
    [[ -z "${COLD_CACHE_ROOT:-}" && -f "$COLD_ROOT_FILE" && -f "$COLD_IMAGE_FILE" ]] || return 1
    local root recorded current criterion seen
    root="$(<"$COLD_ROOT_FILE")"; recorded="$(<"$COLD_IMAGE_FILE")"; current="$(backend_image_id)"
    [[ -n "$recorded" && "$recorded" == "$current" && -d "$root" ]] || return 1
    for criterion in download weights nltk-hardlink; do
        seen="$(python3 "$COLD_CACHE_ORACLE" outcome --root "$root" --criterion "$criterion" || true)"
        [[ "${seen%%$'\t'*}" == "pass" ]] || return 1
    done
    COLD_CACHE_ROOT="$root"
    return 0
}

if [[ "$COLD_ONLY" == "true" ]]; then
    # No stack is touched, so none of the EXIT/INT machinery above may fire: its purge would
    # delete the cache this run exists to leave behind.
    trap - EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if [[ -n "$PATCH_SKIP_REASON" ]]; then
        echo -e "${YELLOW}--patch: Scenario D waived; nothing to run early${NC}" >&2
        exit 0
    fi
    mkdir -p "$COLD_STATE_DIR"
    rm -f "$COLD_ROOT_FILE" "$COLD_IMAGE_FILE"   # never leave a stale pair beside a new run
    cold_image_before="$(backend_image_id)"
    scenario_d_run
    if [[ $cold_rc -eq 0 && -n "$cold_image_before" ]]; then
        printf '%s\n' "$COLD_CACHE_ROOT" > "$COLD_ROOT_FILE"
        printf '%s\n' "$cold_image_before" > "$COLD_IMAGE_FILE"
    fi
    exit $cold_rc
fi

if [[ -n "$live_stack_names" ]]; then
    # Automating the stop/restart dance, but NOT the decision. Taking someone's deployment
    # down stays a human call -- it is just no longer a manual two-step. Approval comes from
    # `--yes` (the pipeline's existing "I mean it" signal), an explicit
    # RELEASE_AUTO_STOP_STACK=true, or an interactive prompt. With none of those and no tty,
    # the original refusal stands, so an unattended caller can never silently stop a stack.
    # (the decision itself lives in live-stack-lib.sh, shared with 85-smoke.sh)
    auto_stop=false
    lsl_approved_to_stop "$live_stack_names" "the scenarios" && auto_stop=true

    if [[ "$auto_stop" != "true" ]]; then
        record live-stack-stopped fail "the live stack is running" "./opentr.sh stop"
        echo -e "${RED}The live stack is running; the scenarios cannot run beside it.${NC}" >&2
        echo -e "${YELLOW}  ./opentr.sh stop     # preserves all data${NC}" >&2
        echo -e "${YELLOW}  (restart afterwards with ./opentr.sh start dev)${NC}" >&2
        echo -e "${YELLOW}  or re-run with --yes / RELEASE_AUTO_STOP_STACK=true to do both automatically${NC}" >&2
        # Exit 3 (precondition unmet) as before. Deliberately BEFORE
        # criteria_assert_all_checked: nothing ran, so the scenario criteria are genuinely
        # unchecked, and reporting that as wiring drift would bury the real reason.
        exit 3
    fi
fi

# Scenario D runs HERE: after the stop was approved, before anything is stopped. It binds no
# port and uses no stock container name, so its minutes are not downtime for the live
# stack. Scenario A, later, installs from what it produced. (--cold-cache-only ran it earlier.)
if [[ -z "$PATCH_SKIP_REASON" ]]; then
    if cold_evidence_reusable; then
        echo -e "${GREEN}Scenario D: reusing the cold-cache run from ${COLD_CACHE_ROOT} (same image, every criterion passed)${NC}" >&2
        export OT_COLD_CACHE_SEED="$COLD_CACHE_ROOT/cache"
    else
        scenario_d_run
    fi
fi

if [[ -n "$live_stack_names" ]]; then
    # ./opentr.sh stop, never docker kill/pkill: see live-stack-lib.sh.
    if ! lsl_stop_stack; then
        record live-stack-stopped fail "$LSL_FAIL_REASON" "$LSL_FAIL_FIX"
        echo -e "${RED}${LSL_FAIL_REASON}; not proceeding (${LSL_FAIL_FIX}).${NC}" >&2
        exit 3
    fi
    STACK_STOPPED_BY_US=true
fi

# The label check above only knows the two stock compose projects. An `otfresh-*` fresh stack
# (what the `test` stage runs on) holds the same ports under neither label, and used to pass
# straight through to gr_check_ports_free's exit 1 — a scenario FAIL, not an unmet precondition.
# Ask who actually owns the ports. Runs AFTER the auto-stop above so that path is unaffected.
# --- BEGIN stock-port-owners-check ---
# shellcheck source=scripts/lib/stock-port-owners.sh
source "$REPO_ROOT/scripts/lib/stock-port-owners.sh"
stock_port_state=0
stock_port_check || stock_port_state=$?
if [[ "$stock_port_state" -eq 1 ]]; then
    record live-stack-stopped fail "$STOCK_PORT_SUMMARY" "$STOCK_PORT_FIX"
    echo -e "${RED}${STOCK_PORT_SUMMARY}${NC}" >&2
    echo -e "${YELLOW}  stop it with: ${STOCK_PORT_FIX}${NC}" >&2
    echo -e "${YELLOW}  (the scenarios bind 5173-5180 under the installer's stock names)${NC}" >&2
    exit 3
fi
# --- END stock-port-owners-check ---
record live-stack-stopped pass

fresh_rc=0; upgrade_rc=0; lite_rc=0
# Scenario C's teardown status. Separate from lite_rc on purpose — whether the lite
# deployment works is a fact about the RELEASE; whether this host then released its ports
# is a fact about the HOST. It still reaches the exit-code aggregation below, because
# teardown_scenario returns 3 (precondition) when the containers will not go away, and a
# stack that outlives the run blocks the NEXT rehearsal exactly as an unmet precondition
# does. Initialised here, not in the branch, so a --patch waiver (which runs no scenarios
# at all) still has it defined under `set -u`.
lite_teardown_rc=0

# Names still held by a previous scenario's stack, as a single string ("" = none).
#
# Deliberately NOT `docker ps ... | grep -q .`: this script runs under `set -o pipefail`,
# and `grep -q` closes the pipe on its first match, so `docker ps` can die with SIGPIPE and
# turn a stack that IS still up into a reported "all clear". Capturing the output has no
# such failure mode.
# $1 = the compose project the scenario ran under: `opentranscribe` for A and B, `ot-reltest-lite`
# for C. Asking about the stock project after C proved nothing about C's containers (STK plan R4).
# Prints the UTC start and duration_s of whatever it runs (LOOP_SPEED_PLAN M1: the rehearsal had
# no per-scenario times, so its ~27 min was reconstructed from file mtimes). Returns the command's rc.
rehearse_timed() {
    local label="$1" t0=$SECONDS rc=0; shift
    echo -e "${BLUE}${label} started $(date -u +%FT%TZ)${NC}" >&2
    "$@" || rc=$?
    echo -e "${BLUE}${label} done duration_s=$(( SECONDS - t0 )) rc=${rc}${NC}" >&2
    return "$rc"
}

stock_containers() {
    docker ps -a --filter "label=com.docker.compose.project=${1:?compose project}" --format '{{.Names}}'
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
    local label="$1" project="$2"; shift 2
    echo -e "${BLUE}Tearing down ${label}'s stack so the next scenario can bind its ports${NC}" >&2
    "$@" --cleanup --yes || {
        echo -e "${YELLOW}${label} cleanup reported a problem; continuing${NC}" >&2
    }

    # Cleanup is asynchronous at the docker level — a container in 'Removing' still holds its
    # port bindings, and the next preflight would see a stale name. Wait for the names to
    # actually disappear rather than racing them.
    local _
    for _ in $(seq 1 30); do
        [[ -z "$(stock_containers "$project")" ]] && break
        sleep 2
    done
    if [[ -n "$(stock_containers "$project")" ]]; then
        echo -e "${RED}${label}'s containers did not go away; the next scenario cannot start${NC}" >&2
        docker ps -a --filter "label=com.docker.compose.project=${project}" --format '  {{.Names}}\t{{.Status}}' >&2
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
    rehearse_timed "Scenario A" ./scripts/release-tests/test-fresh-install.sh --yes || fresh_rc=$?
    # A has copied (hardlinked or real-copied) what it needed; D's evidence stays, its GB go.
    purge_cold_cache

    teardown_scenario "Scenario A" opentranscribe ./scripts/release-tests/test-fresh-install.sh || upgrade_rc=$?

    if [[ $upgrade_rc -eq 0 ]]; then
        echo -e "${BLUE}Scenario B — upgrade from the previous published release${NC}" >&2
        REQUIRE_PREVIOUS=1 rehearse_timed "Scenario B" ./scripts/release-tests/test-upgrade.sh --yes || upgrade_rc=$?
    fi

    # Scenario C runs regardless of B's assertion verdict but not through a broken teardown:
    # a failed upgrade is a finding about the upgrade, and lite is an independent deployment
    # shape whose evidence is worth having either way. A stack that would not release its
    # ports, though, means C cannot bind and would fail for an unrelated reason.
    if [[ $upgrade_rc -eq 3 ]]; then
        lite_rc=3
    else
        teardown_scenario "Scenario B" opentranscribe ./scripts/release-tests/test-upgrade.sh || lite_rc=$?
    fi

    if [[ $lite_rc -eq 0 ]]; then
        echo -e "${BLUE}Scenario C — lite (CPU-only) deployment, mocked cloud ASR + mocked LLM${NC}" >&2
        rehearse_timed "Scenario C" ./scripts/release-tests/test-lite-mode.sh --yes || lite_rc=$?
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
    teardown_scenario "Scenario C" ot-reltest-lite ./scripts/release-tests/test-lite-mode.sh || lite_teardown_rc=$?
    if [[ $lite_teardown_rc -ne 0 ]]; then
        echo -e "${YELLOW}Scenario C's stack did not tear down cleanly (rc=$lite_teardown_rc).${NC}" >&2
        echo -e "${YELLOW}The NEXT rehearsal will refuse to start until it is gone. Clean up with:${NC}" >&2
        echo -e "${YELLOW}    ./scripts/release-tests/test-lite-mode.sh --cleanup --yes${NC}" >&2
    fi
fi

# Gap 2 (RELEASE_SPEED_PLAN section 6): the frontend/docs arm64 legs were scanned and
# manifest-checked but never EXECUTED before Docker Hub; `smoke` probes only lite arm64, after
# publish. Run `nginx -t` in each of them on the arm64 host (`docker --context <ctx> run`), so an
# exec-format or wrong-base defect is caught before the push. The leg is a name in the LOCAL
# daemon (built with --load), so it is streamed to the remote daemon first (small nginx
# images). Prints "<outcome><TAB><detail>"; never exits the stage by itself.
HOST_ONLY=0
if [[ "${RELEASE_HOST_ARCH_ONLY:-}" == "1" || "${RELEASE_HOST_ARCH_ONLY:-}" == "true" \
      || -f "$REPO_ROOT/.release/${VERSION}/host-arch-only" ]]; then
    HOST_ONLY=1
fi
arm64_web_legs_outcome() {
    local ctx="${REMOTE_ARM64_CONTEXT:-remote-arm64}"
    local legs="$REPO_ROOT/.release/${VERSION}/legs.tsv"
    local component platform tag checked=0
    local -a failed=()
    if [[ -n "$PATCH_SKIP_REASON" ]]; then
        printf 'not-measured\t%s\n' "$PATCH_SKIP_REASON"; return
    fi
    if (( HOST_ONLY )); then
        printf 'not-measured\thost-arch-only build: no arm64 legs to run\n'; return
    fi
    if ! docker context inspect "$ctx" >/dev/null 2>&1; then
        printf "not-measured\tno '%s' docker context\n" "$ctx"; return
    fi
    if [[ ! -f "$legs" ]]; then
        printf 'not-measured\tno %s (run the build stage first)\n' "$legs"; return
    fi
    while IFS=$'\t' read -r component _ platform tag _; do
        [[ "$platform" == "linux/arm64" ]] || continue
        [[ "$component" == "frontend" || "$component" == "docs" ]] || continue
        checked=$((checked + 1))
        if ! docker save "$tag" | docker --context "$ctx" load >/dev/null 2>&1; then
            failed+=("${tag}: could not be loaded onto ${ctx}"); continue
        fi
        if ! docker --context "$ctx" run --rm "$tag" nginx -t >/dev/null 2>&1; then
            failed+=("${tag}: nginx -t failed on arm64")
        fi
    done < "$legs"
    if (( ${#failed[@]} )); then
        printf 'fail\t%s\n' "${failed[*]}"
    elif (( checked == 0 )); then
        printf 'not-measured\tlegs.tsv lists no frontend/docs arm64 leg\n'
    else
        printf 'pass\t%d arm64 web leg(s) ran nginx -t\n' "$checked"
    fi
}

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

# Scenario D, one criterion per question so a red one names what broke. Each reads the
# scenario's evidence through the oracle; a missing file is not-measured, never a pass.
# --- BEGIN cold-cache-record ---
cold_outcome() {
    local criterion="$1" line
    if [[ -n "$PATCH_SKIP_REASON" ]]; then
        printf 'not-measured\t%s\n' "$PATCH_SKIP_REASON"
        return
    fi
    if [[ -z "$COLD_CACHE_ROOT" ]]; then
        printf 'not-measured\tScenario D did not run\n'
        return
    fi
    if ! line="$(python3 "$COLD_CACHE_ORACLE" outcome --root "$COLD_CACHE_ROOT" --criterion "$criterion")"; then
        printf 'not-measured\tthe cold-cache oracle could not read %s\n' "$COLD_CACHE_ROOT"
        return
    fi
    printf '%s\n' "$line"
}
cold_fix="read $COLD_CACHE_ROOT/REPORT.md and groups/<group>.log"
# The stage's exit code must agree with what is recorded. D's own exit status normally does,
# but the evidence is the authority: a scenario that exited 0 having written no TSV (or a
# failing one) must not leave this stage at 0 beside a blocking criterion it failed.
cold_derived=0
cold_tally() {
    case "${1%%$'\t'*}" in
        fail)         cold_derived=1 ;;
        not-measured) (( cold_derived == 1 )) || cold_derived=3 ;;
    esac
}
cold_line="$(cold_outcome download)"; cold_tally "$cold_line"
record cold-cache-model-download "${cold_line%%$'\t'*}" "${cold_line#*$'\t'}" "$cold_fix" "${PATCH_SKIP_REASON:+waived}"
cold_line="$(cold_outcome weights)"; cold_tally "$cold_line"
record cold-cache-weights-present "${cold_line%%$'\t'*}" "${cold_line#*$'\t'}" "$cold_fix" "${PATCH_SKIP_REASON:+waived}"
cold_line="$(cold_outcome nltk-hardlink)"; cold_tally "$cold_line"
record cold-cache-nltk-hardlinks-fail-loudly "${cold_line%%$'\t'*}" "${cold_line#*$'\t'}" "$cold_fix" "${PATCH_SKIP_REASON:+waived}"
# A --patch waiver is recorded waived (a warning), exactly like A-C, and moves no exit code.
if [[ -z "$PATCH_SKIP_REASON" && $cold_rc -eq 0 ]]; then
    cold_rc=$cold_derived
fi
# --- END cold-cache-record ---

# Gap 2: blocking. An unreachable arm64 host blocks; the recorded override is --force-rehearse "reason".
arm64_line="$(arm64_web_legs_outcome)"
arm64_waiver="${PATCH_SKIP_REASON:+waived}"
(( HOST_ONLY )) && arm64_waiver=waived
record arm64-web-legs-run "${arm64_line%%$'\t'*}" "${arm64_line#*$'\t'}" \
    "make the arm64 host reachable (docker context remote-arm64) and re-run rehearse, or --force-rehearse \"reason\" (recorded in the ledger)" "$arm64_waiver"

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
if [[ $fresh_rc -eq 4 || $upgrade_rc -eq 4 || $lite_rc -eq 4 || $lite_teardown_rc -eq 4 || $cold_rc -eq 4 ]]; then
    rc=4
elif [[ $fresh_rc -eq 3 || $upgrade_rc -eq 3 || $lite_rc -eq 3 || $lite_teardown_rc -eq 3 || $cold_rc -eq 3 ]]; then
    rc=3
elif [[ $fresh_rc -ne 0 || $upgrade_rc -ne 0 || $lite_rc -ne 0 || $lite_teardown_rc -ne 0 || $cold_rc -ne 0 ]]; then
    rc=1
fi
[[ $rc -eq 0 ]] && echo -e "${GREEN}all four scenarios passed${NC}" >&2

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"rehearse","version":"%s","status":"%s","criteria":[%s],"next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" \
        "$(criteria_json)" \
        "$([[ $rc -eq 0 ]] && echo '["restart the dev stack, then tag"]' || echo '["read the REPORT.md under each TEST_ROOT"]')"
fi
exit $rc
