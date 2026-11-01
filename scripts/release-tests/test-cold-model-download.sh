#!/bin/bash
# test-cold-model-download.sh — Scenario D: the model downloader, from an EMPTY cache, on CPU,
# in the image being released.
#
# WHY THIS EXISTS
#
# v0.6.0 shipped a scripts/download-models.py whose `pyannote` group exits 1 on every host
# from a cold cache, CPU and GPU alike (measured 2026-11-01 on the published v0.6.0 image:
# `IndexError: list index out of range` in transformers/pipelines/base.py, from whisperx's
# transcribe of synthetic sine-wave audio that VAD rejects). No release gate ran it:
#
#   * the rehearsal installs with OPENTRANSCRIBE_UNATTENDED=1, and setup-opentranscribe.sh's
#     download_ai_models() returns at its first line under that flag; and
#   * every scenario then seeds its model cache from a warm shared copy, so even the app's own
#     lazy download never runs.
#
# So the one command an interactive `curl | bash` user runs right after install was the one
# command no rehearsal had ever executed. scripts/test-model-download.sh was the only direct
# exercise, and it ran `:latest` with `--gpus all`, by hand, wired into nothing.
#
# WHAT IT DOES
#
#   1. Resolves the image under test (davidamacey/opentranscribe-backend:<VERSION>), and proves
#      it is the code under test exactly as Scenario A does (gr_assert_image_is_the_code_under_test).
#   2. Proves the container sees NO GPU (torch.cuda.is_available() is False) — a CPU-only host
#      is the shape no other leg covers, and this host's GPUs are shared.
#   3. Runs `download-models.py --only <group>` once per group, each in its own container,
#      against one empty cache. The group list is read from the script's own DOWNLOAD_GROUPS
#      (lib/cold-model-download.py groups), so a new group is rehearsed the day it lands, and a
#      failing group is NAMED rather than folded into one exit status. The mounts mirror
#      scripts/download-models.sh's docker run (unit-tested), and the script is the release
#      checkout's own copy — the exact pairing an installed user gets.
#   4. Checks the resulting cache independently of the exit codes (lib/cold-model-download.py
#      verify): the downloader treats 'partial' and 'skipped' as success.
#   5. The nltk hardlink leg: `cp -al` the produced nltk_data, run the nltk group against it,
#      and require that the downloader either fails NAMING pathsec, or exits 0 leaving a corpus
#      nltk will open. Exit 0 over a tree nltk >= 3.10 refuses is the silent shape that cost
#      a full rehearsal once (scripts/CLAUDE.md, "NEVER hardlink nltk_data").
#
# It binds no ports, uses no stock container names and needs no stack stopped, so it can run
# beside the live deployment. 65-rehearse.sh runs it first and hands its cache to Scenario A,
# which then installs from weights the shipped downloader produced instead of the warm copy.
#
# EVIDENCE (under TEST_ROOT): groups.tsv (group, rc, seconds), groups-expected.txt,
# artifacts.tsv (group, pass|fail, detail), hardlink.tsv (pass|fail, detail), provenance.txt,
# groups/<group>.log, cache/ (the cold-produced cache), REPORT.md.
#
# Exit: 0 every group and check passed · 1 a group or check failed · 2 misuse ·
#       3 precondition (no image, no token, image not the code under test, GPU visible).
#
# Usage:
#   ./scripts/release-tests/test-cold-model-download.sh [--yes] [--list-groups] [--dry-run]
#   ./scripts/release-tests/test-cold-model-download.sh --cleanup     # remove the last TEST_ROOT
#
# Env: LOCAL_IMAGE_TAG (default: VERSION), DOWNLOADER_IMAGE (overrides the image outright;
#      provenance is then recorded UNVERIFIED and the run can never report a pass),
#      DOWNLOADER_PY (a different download-models.py, e.g. an unmerged fix; also UNVERIFIED),
#      WHISPER_MODEL (default large-v3-turbo — Scenario A's install default, so A can seed from
#      this cache), OPENSEARCH_MODELS (default all-MiniLM-L6-v2, also A's), TEST_ROOT,
#      HUGGINGFACE_TOKEN (else read from OT_RELEASE_TEST_SECRETS / .env.test-secrets),
#      OT_COLD_CACHE_GROUPS (comma list, iteration only: a subset run is never a pass).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TEST_SCENARIO="cold-model-download"
TEST_PROJECT_NAME="${TEST_PROJECT_NAME:-ot-reltest-coldcache}"
TEST_ROOT="${TEST_ROOT:-/mnt/nvm/opentranscribe-test-runs/${TEST_PROJECT_NAME}-$(date +%Y%m%d-%H%M%S)}"
TEST_LABEL="com.opentranscribe.release-test=${TEST_SCENARIO}"
LOCAL_IMAGE_TAG="${LOCAL_IMAGE_TAG:-}"
WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}"
OPENSEARCH_MODELS="${OPENSEARCH_MODELS:-all-MiniLM-L6-v2}"
SCRIPT_OVERRIDDEN=0
[[ -n "${DOWNLOADER_PY:-}" ]] && SCRIPT_OVERRIDDEN=1
DOWNLOADER_PY="${DOWNLOADER_PY:-$REPO_ROOT/scripts/download-models.py}"
ORACLE="$LIB_DIR/cold-model-download.py"

DO_CLEANUP=0
DO_LIST=0
DO_DRY_RUN=0
while (( $# > 0 )); do
    case "$1" in
        --yes)          ;;   # accepted for symmetry with A-C; nothing here needs confirming
        --cleanup)      DO_CLEANUP=1 ;;
        --list-groups)  DO_LIST=1 ;;
        --dry-run)      DO_DRY_RUN=1 ;;
        --help|-h)      sed -n '2,60p' "$0"; exit 0 ;;
        *)              echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

# shellcheck source=lib/guardrails.sh
source "$LIB_DIR/guardrails.sh"
# shellcheck source=lib/versions.sh
source "$LIB_DIR/versions.sh"

groups_from_script() {
    python3 "$ORACLE" groups "$DOWNLOADER_PY"
}

if (( DO_LIST )); then
    groups_from_script
    exit $?
fi

# Removes only a TEST_ROOT this scenario recorded, under the path firewall. The cache is
# written by the container's appuser (uid 1000), so a host user with another uid cannot rm it;
# the busybox fallback removes it from inside a container instead.
remove_test_root() {
    local root="$1"
    [[ -n "$root" && -d "$root" ]] || return 0
    case "$(gr_realpath "$root")" in
        /mnt/nvm/opentranscribe-test-runs/"${TEST_PROJECT_NAME}"-*|/tmp/ot-reltest-*) ;;
        *) gr_die "refusing to remove '$root': not a ${TEST_PROJECT_NAME} test root" ;;
    esac
    rm -rf "$root" 2>/dev/null || docker run --rm -v "$root:/r" busybox rm -rf /r/cache /r/hardlink
    rm -rf "$root"
    gr_ok "removed $root"
}

if (( DO_CLEANUP )); then
    last="$(gr_last_test_root || true)"
    remove_test_root "${last:-}"
    exit 0
fi

if [[ -z "$LOCAL_IMAGE_TAG" ]]; then
    LOCAL_IMAGE_TAG="$(ver_to_version)"
fi
LOCAL_IMAGE_TAG="$(ver_normalize "$LOCAL_IMAGE_TAG")"
IMAGE_OVERRIDDEN=0
[[ -n "${DOWNLOADER_IMAGE:-}" ]] && IMAGE_OVERRIDDEN=1
DOWNLOADER_IMAGE="${DOWNLOADER_IMAGE:-davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG}}"

all_groups="$(groups_from_script)" || { echo "cannot read DOWNLOAD_GROUPS from $DOWNLOADER_PY" >&2; exit 3; }
if [[ -n "${OT_COLD_CACHE_GROUPS:-}" ]]; then
    selected="${OT_COLD_CACHE_GROUPS//,/$'\n'}"
else
    selected="$all_groups"
fi
mapfile -t GROUPS_TO_RUN < <(printf '%s\n' "$selected" | sed '/^$/d')

if (( DO_DRY_RUN )); then
    echo "image:          $DOWNLOADER_IMAGE"
    echo "downloader:     $DOWNLOADER_PY"
    echo "WHISPER_MODEL:  $WHISPER_MODEL"
    echo "TEST_ROOT:      $TEST_ROOT"
    echo "groups:         ${GROUPS_TO_RUN[*]}"
    exit 0
fi

gr_check_test_root
mkdir -p "$TEST_ROOT/groups" "$TEST_ROOT/cache"
printf '%s\n' "$all_groups" > "$TEST_ROOT/groups-expected.txt"
CACHE="$TEST_ROOT/cache"
SUBDIRS=(huggingface torch nltk_data sentence-transformers opensearch-ml diar-native)
for sub in "${SUBDIRS[@]}"; do mkdir -p "$CACHE/$sub"; done

# ─── Preconditions (exit 3: nothing about the release was measured) ──────────
if ! docker image inspect "$DOWNLOADER_IMAGE" >/dev/null 2>&1; then
    gr_warn "image $DOWNLOADER_IMAGE is not present — build it first (./scripts/release.sh build <version>)"
    exit 3
fi

if (( IMAGE_OVERRIDDEN || SCRIPT_OVERRIDDEN )) || [[ -n "${OT_RELEASE_TEST_ALLOW_STALE_IMAGE:-}" ]]; then
    # Both are how an operator deliberately examines an image that is NOT this release (the
    # stale-image escape makes gr_assert_image_is_the_code_under_test return 0). Evidence
    # about that image is still evidence of failure, never of this release passing.
    echo "UNVERIFIED (DOWNLOADER_IMAGE/DOWNLOADER_PY override or OT_RELEASE_TEST_ALLOW_STALE_IMAGE): $DOWNLOADER_IMAGE" \
        > "$TEST_ROOT/provenance.txt"
    gr_warn "image provenance UNVERIFIED — this run can report a failure but never a pass"
elif ( gr_assert_image_is_the_code_under_test "$DOWNLOADER_IMAGE" \
        "$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)" ); then
    echo "verified $DOWNLOADER_IMAGE" > "$TEST_ROOT/provenance.txt"
else
    exit 3
fi

if [[ -z "${HUGGINGFACE_TOKEN:-}" ]]; then
    secrets="${OT_RELEASE_TEST_SECRETS:-$SCRIPT_DIR/.env.test-secrets}"
    if [[ -f "$secrets" ]]; then
        # shellcheck disable=SC1090
        source "$secrets"
    fi
fi
if [[ -z "${HUGGINGFACE_TOKEN:-}" ]]; then
    gr_warn "no HUGGINGFACE_TOKEN (env or ${OT_RELEASE_TEST_SECRETS:-$SCRIPT_DIR/.env.test-secrets}) — the gated pyannote groups cannot run; NOT MEASURED"
    exit 3
fi
export HUGGINGFACE_TOKEN WHISPER_MODEL OPENSEARCH_MODELS

# Passed by NAME (-e VAR), never `-e VAR=value`: the token must not appear in `ps` output.
# NVIDIA_VISIBLE_DEVICES=void and CUDA_VISIBLE_DEVICES='' keep a host whose daemon defaults to
# the nvidia runtime from handing the container every GPU — including cards this project must
# never touch.
run_in_image() {
    local name="$1"; shift
    # --no-healthcheck: the image's HEALTHCHECK probes the API, which a one-shot downloader
    # never serves, so every container would read "unhealthy" in `docker ps`.
    docker run --rm --no-healthcheck --name "ot-reltest-coldcache-$$-$name" --label "$TEST_LABEL" \
        -e NVIDIA_VISIBLE_DEVICES=void -e CUDA_VISIBLE_DEVICES= \
        -e HUGGINGFACE_TOKEN -e WHISPER_MODEL -e OPENSEARCH_MODELS \
        -e USE_GPU=false -e COMPUTE_TYPE="${COMPUTE_TYPE:-float16}" \
        -e DIARIZATION_MODEL="${DIARIZATION_MODEL:-pyannote/speaker-diarization-community-1}" \
        -e DOWNLOAD_ALL_OPENSEARCH_MODELS=false -e DOWNLOAD_REDACTION_MODELS=true \
        "$@"
}

cuda_visible="$(run_in_image cuda-probe "$DOWNLOADER_IMAGE" \
    python -c 'import torch; print(torch.cuda.is_available())' 2>/dev/null | tail -1 || true)"
if [[ "$cuda_visible" != "False" ]]; then
    gr_warn "the container reports torch.cuda.is_available()='$cuda_visible', not False — this leg must be CPU-only"
    exit 3
fi
gr_ok "container sees no GPU (CPU-only leg)"

# ─── 1. Every group, one container each, one shared empty cache ─────────────
# Mirrors scripts/download-models.sh's mount set; test_release_cold_model_download.py
# fails if the two diverge.
CACHE_MOUNTS=(
    -v "$CACHE/huggingface:/home/appuser/.cache/huggingface"
    -v "$CACHE/torch:/home/appuser/.cache/torch"
    -v "$CACHE/nltk_data:/home/appuser/.cache/nltk_data"
    -v "$CACHE/sentence-transformers:/home/appuser/.cache/sentence-transformers"
    -v "$CACHE/opensearch-ml:/home/appuser/.cache/opensearch-ml"
    -v "$CACHE/diar-native:/models"
    -v "$DOWNLOADER_PY:/app/download-models.py:ro"
)

# One group: its log, then one `<group>\t<rc>\t<seconds>` row in groups/<group>.row. A group
# that never writes its row (killed, or never started) is MISSING from groups.tsv, which the
# oracle reports as not-measured — never as a pass.
run_group() {
    local group="$1" started=$SECONDS rc=0
    run_in_image "$group" "${CACHE_MOUNTS[@]}" "$DOWNLOADER_IMAGE" \
        python /app/download-models.py --only "$group" > "$TEST_ROOT/groups/$group.log" 2>&1 || rc=$?
    printf '%s\t%s\t%s\n' "$group" "$rc" "$(( SECONDS - started ))" > "$TEST_ROOT/groups/$group.row"
    if (( rc == 0 )); then
        gr_ok "group '$group' exited 0 ($(( SECONDS - started ))s)"
    else
        # Diagnostics first, while they exist: the log is the only record of why.
        gr_warn "group '$group' exited $rc — last lines of $TEST_ROOT/groups/$group.log:"
        tail -25 "$TEST_ROOT/groups/$group.log" | sed 's/^/      /' >&2
    fi
}

# Groups run in DOWNLOAD_GROUPS order, as a user's single run does — with ONE exception, for
# wall time. diar-native is a CPU ONNX export of the pyannote weights and was 561 s of a
# 1,073 s run (measured 2026-11-01). It starts in the background as soon as `pyannote` has
# finished, so it never races that group for the same hub files, and the remaining groups
# (unrelated repos) run beside it. Every other group keeps the user's order: pyannote's warm-up
# loads WHISPER_MODEL, which a real run has already fetched through the whisperx group.
BACKGROUND_GROUPS=" diar-native "
background_pids=()
started_background=0
start_background_groups() {
    (( started_background )) && return 0
    started_background=1
    local g
    for g in "${GROUPS_TO_RUN[@]}"; do
        [[ "$BACKGROUND_GROUPS" == *" $g "* ]] || continue
        gr_log "downloader group '$g' (cold cache, CPU, in the background) …"
        run_group "$g" &
        background_pids+=("$!")
    done
}
case " ${GROUPS_TO_RUN[*]} " in *" pyannote "*) ;; *) start_background_groups ;; esac
for group in "${GROUPS_TO_RUN[@]}"; do
    [[ "$BACKGROUND_GROUPS" == *" $group "* ]] && continue
    gr_log "downloader group '$group' (cold cache, CPU) …"
    run_group "$group"
    [[ "$group" == "pyannote" ]] && start_background_groups
done
for pid in "${background_pids[@]}"; do wait "$pid" || true; done
: > "$TEST_ROOT/groups.tsv"
for group in "${GROUPS_TO_RUN[@]}"; do
    if [[ -f "$TEST_ROOT/groups/$group.row" ]]; then
        cat "$TEST_ROOT/groups/$group.row" >> "$TEST_ROOT/groups.tsv"
    fi
done

# ─── 2. Independent artifact check ──────────────────────────────────────────
whisper_repo="$(run_in_image catalog "$DOWNLOADER_IMAGE" python -c '
import os
from app.core.local_whisper_catalog import canonical_id, get
entry = get(canonical_id(os.environ["WHISPER_MODEL"]) or "")
print(entry.repo_id if entry else "")' 2>/dev/null | tail -1 || true)"
selected_csv="$(IFS=,; echo "${GROUPS_TO_RUN[*]}")"
verify_rc=0
python3 "$ORACLE" verify --cache "$CACHE" --groups "$selected_csv" \
    --whisper-repo "$whisper_repo" --opensearch-models "$OPENSEARCH_MODELS" \
    > "$TEST_ROOT/artifacts.tsv" || verify_rc=$?
while IFS=$'\t' read -r g verdict detail; do
    if [[ "$verdict" == "pass" ]]; then gr_ok "artifacts: $g"; else gr_warn "artifacts: $g — $detail"; fi
done < "$TEST_ROOT/artifacts.tsv"

# ─── 3. nltk on a hardlink-seeded cache ─────────────────────────────────────
hardlink_leg() {
    local src="$CACHE/nltk_data" dst="$TEST_ROOT/hardlink/nltk_data"
    if [[ -z "$(ls -A "$src" 2>/dev/null)" ]]; then
        printf 'not-measured\tthe cold run produced no nltk_data to hardlink\n'
        return
    fi
    mkdir -p "$TEST_ROOT/hardlink"
    if ! cp -al "$src" "$dst" 2>/dev/null; then
        printf 'not-measured\tcp -al could not hardlink %s (protected_hardlinks?)\n' "$src"
        return
    fi
    local rc=0 log="$TEST_ROOT/groups/nltk-hardlinked.log"
    run_in_image nltk-hardlinked -v "$dst:/home/appuser/.cache/nltk_data" \
        -v "$DOWNLOADER_PY:/app/download-models.py:ro" "$DOWNLOADER_IMAGE" \
        python /app/download-models.py --only nltk > "$log" 2>&1 || rc=$?
    local linked=0
    while IFS= read -r -d '' _; do linked=$(( linked + 1 )); done \
        < <(find "$dst" -type f -links +1 -print0 2>/dev/null)
    if (( rc != 0 )); then
        if [[ "$(grep -c -E 'pathsec|multiply-linked' "$log" || true)" -gt 0 ]]; then
            printf 'pass\texited %s and named the pathsec refusal (fails loudly, never silently)\n' "$rc"
        else
            printf 'fail\texited %s without naming the cause (%s)\n' "$rc" "$log"
        fi
    elif (( linked > 0 )); then
        printf 'fail\texited 0 but left %s multiply-linked file(s): nltk >= 3.10 refuses them at runtime\n' "$linked"
    elif run_in_image nltk-load -v "$dst:/home/appuser/.cache/nltk_data" "$DOWNLOADER_IMAGE" \
            python -c 'import nltk; nltk.data.path.insert(0, "/home/appuser/.cache/nltk_data"); nltk.sent_tokenize("One. Two.")' \
            >> "$log" 2>&1; then
        printf 'pass\texited 0 and repaired the links; the corpus loads\n'
    else
        printf 'fail\texited 0 but the corpus does not load (%s)\n' "$log"
    fi
    # The links also raised st_nlink on the COLD cache Scenario A is about to seed from.
    rm -rf "$TEST_ROOT/hardlink"
}
hardlink_leg > "$TEST_ROOT/hardlink.tsv"
hl_v="$(python3 "$ORACLE" outcome --root "$TEST_ROOT" --criterion nltk-hardlink)"
hl_verdict="${hl_v%%$'\t'*}"
hl_detail="${hl_v#*$'\t'}"
if [[ "$hl_verdict" == "pass" ]]; then gr_ok "nltk hardlink leg: $hl_detail"; else gr_warn "nltk hardlink leg: $hl_detail"; fi

# ─── Verdict ────────────────────────────────────────────────────────────────
download_v="$(python3 "$ORACLE" outcome --root "$TEST_ROOT" --criterion download)"
weights_v="$(python3 "$ORACLE" outcome --root "$TEST_ROOT" --criterion weights)"
{
    echo "# Scenario D — cold-cache model download"
    echo
    echo "- image: \`$DOWNLOADER_IMAGE\` ($(cat "$TEST_ROOT/provenance.txt"))"
    echo "- downloader: \`scripts/download-models.py\` at $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')"
    echo "- WHISPER_MODEL=$WHISPER_MODEL, CPU-only (torch.cuda.is_available() == False)"
    echo
    echo "| criterion | outcome | detail |"
    echo "|---|---|---|"
    echo "| cold-cache-model-download | ${download_v%%$'\t'*} | ${download_v#*$'\t'} |"
    echo "| cold-cache-weights-present | ${weights_v%%$'\t'*} | ${weights_v#*$'\t'} |"
    echo "| cold-cache-nltk-hardlinks-fail-loudly | $hl_verdict | $hl_detail |"
    echo
    echo "| group | rc | seconds |"
    echo "|---|---|---|"
    while IFS=$'\t' read -r g rc secs; do echo "| $g | $rc | $secs |"; done < "$TEST_ROOT/groups.tsv"
} > "$TEST_ROOT/REPORT.md"
gr_log "report: $TEST_ROOT/REPORT.md"

if [[ "${download_v%%$'\t'*}" == "pass" && "${weights_v%%$'\t'*}" == "pass" \
      && "$hl_verdict" == "pass" && $verify_rc -eq 0 ]]; then
    gr_ok "Scenario D passed"
    exit 0
fi
if [[ "${download_v%%$'\t'*}" == "fail" || "${weights_v%%$'\t'*}" == "fail" || "$hl_verdict" == "fail" ]]; then
    exit 1
fi
exit 3
