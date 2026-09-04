#!/bin/bash
set -e

YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Container user ownership. appuser in the backend image is `useradd -u 1000` (UID pinned)
# but `groupadd -r appuser` (system group, no GID pin) — it lands at gid 999, so a chown to
# 1000:1000 sets a group that does not exist in the image (issue #580). Kept in sync with
# CONTAINER_UID_GID in scripts/common.sh; this script ships standalone to end users, so it
# defines its own.
CONTAINER_UID_GID="${CONTAINER_UID_GID:-1000:999}"

# scripts/common.sh ships with every install (release-manifest.txt:52) and holds the ONE
# implementation of the database backup/restore path (#599/#600/#610). Sourced here rather
# than reimplemented so a production install cannot drift from the dev one — issue #613.
#
# Conditional on purpose: an install that predates the manifest entry may not have it yet,
# and every OTHER command must keep working without it. The backup/restore arms check for
# the functions explicitly (require_db_helpers) and fail with a remedy.
#
# Position matters. common.sh also defines fix_model_cache_permissions(); sourcing BEFORE
# this script's own definition means the local one still wins (bash: last definition wins),
# preserving today's behaviour exactly. Pinned by a test — do not move this below the
# fix_model_cache_permissions definition.
#
# ⚠️ Because the local one always wins, it must be kept behaviourally IDENTICAL to
# common.sh's (subdirectory list, and looping the ownership check over every
# subdirectory rather than just the parent) — this script has no way to fall back to
# common.sh's version for this function even when common.sh is present and newer. A
# real install ran for a release with these two silently diverged (missing diar-native
# in the mkdir list, parent-only ownership check); both are pinned by
# backend/tests/unit/test_fix_model_cache_permissions_parity.py so they cannot drift
# apart again without a failing test.
if [ -f ./scripts/common.sh ]; then
    # shellcheck source=scripts/common.sh
    . ./scripts/common.sh
fi

# Fallback definition: common.sh is sourced conditionally above (an install predating
# release-manifest.txt:52 may not have it), and every non-DB command must keep working
# without it. Identical body; common.sh's wins when present. Same standalone-shipping
# rationale as CONTAINER_UID_GID above.
if ! declare -F read_env_value >/dev/null 2>&1; then
    read_env_value() {
        local key="$1" env_file="${2:-.env}"
        [ -f "$env_file" ] || { echo ""; return 0; }
        # Kept in lockstep with scripts/common.sh's read_env_value (including the
        # leading-whitespace / `export ` stripping — compose honours both, see that
        # function's docstring) since this is the standalone fallback for an install
        # predating release-manifest.txt:52.
        sed -E 's/^[[:space:]]+//; s/^export[[:space:]]+//' "$env_file" 2>/dev/null \
            | grep -E "^${key}=" \
            | head -1 \
            | cut -d= -f2- \
            | sed -E 's/[[:space:]]+#.*$//' \
            | tr -d ' "' \
            || true
    }
fi

# The repo's default branch, asked for at runtime rather than written down.
#
# Every hardcoded "master" in a download URL is a rename waiting to break an install.
# Echoes empty on any failure (offline, rate-limited, malformed); callers MUST treat
# empty as "no fallback available" and fail closed rather than guessing a branch name.
# Defined here rather than only in common.sh for the same reason as read_env_value
# above: this script ships standalone, and update-full needs this BEFORE it has
# downloaded a newer common.sh.
if ! declare -F resolve_default_branch >/dev/null 2>&1; then
    resolve_default_branch() {
        curl -fsSL --connect-timeout 10 --max-time 20 \
            "https://api.github.com/repos/attevon-llc/OpenTranscribe" 2>/dev/null \
            | grep -m1 '"default_branch"' \
            | sed -E 's/.*"default_branch"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' \
            || true
    }
fi

# Which ref does `update-full` take CONFIG files from? Echoes it; messages go to stderr.
#
# This was `${OPENTRANSCRIBE_BRANCH:-master}`, so an install pinned to vX.Y.Z re-downloaded
# tip-of-development compose files on top of vX.Y.Z images. It did not 404 — it silently
# produced exactly the config/image mismatch OT_IMAGE_TAG exists to prevent, which is worse
# than failing (issue #683). Service definitions live in docker-compose.yml, so a newer base
# file can reference services, images and env keys the pinned containers know nothing about.
#
# So: follow the pin. Fall back to the default branch ONLY for installs predating pinning
# (OT_IMAGE_TAG unset or 'latest'), and say so out loud. OPENTRANSCRIBE_BRANCH still
# overrides, for testing. A function, not inline in the update-full arm, so the invariant
# is testable — scripts/verify-install-paths.sh and the unit tests both run this directly.
deployment_ref() {
    local pinned fallback
    pinned=$(read_env_value OT_IMAGE_TAG)
    case "$pinned" in
        '' | latest)
            fallback=$(resolve_default_branch)
            [ -n "$fallback" ] || return 1
            printf '%s\tfallback\n' "$fallback"
            ;;
        *) printf '%s\tpinned\n' "$pinned" ;;
    esac
}

# A raw.githubusercontent URL for <path>, at THIS deployment's ref.
#
# Every URL printed or fetched by this script used to say /master/ literally, which hands
# a v0.4.1 user a v0.5.0-development file — the same config/image mismatch as #683, just
# arriving through a copy-pasted remedy instead of update-full. Falls back to a visible
# placeholder rather than guessing a branch name, so an unresolvable ref reads as unknown
# instead of silently wrong.
raw_url_for() {
    local out ref=""
    out=$(deployment_ref) && ref=${out%%$'\t'*}
    printf 'https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/%s/%s\n' \
        "${ref:-<your-release-tag>}" "$1"
}

resolve_config_ref() {
    # Read the override into a defaulted local ONCE rather than expanding the env var at
    # each use site: `set -u` aborts on a bare $OPENTRANSCRIBE_BRANCH even when an earlier
    # `[ -n "${VAR:-}" ]` guard proves it is set, and test_shell_expansion_guards.py
    # enforces that repo-wide (it caught exactly this).
    local override="${OPENTRANSCRIBE_BRANCH:-}"
    if [ -n "$override" ]; then
        echo -e "${BLUE}ℹ️  Using branch override: ${override}${NC}" >&2
        printf '%s\n' "$override"
        return 0
    fi

    local out ref how
    if ! out=$(deployment_ref); then
        echo -e "${RED}❌ This install is not pinned to a release (OT_IMAGE_TAG is unset${NC}" >&2
        echo -e "${RED}   or 'latest') and the default branch could not be resolved.${NC}" >&2
        echo -e "${YELLOW}   Pin it first:  ./opentranscribe.sh update --version vX.Y.Z${NC}" >&2
        echo -e "${YELLOW}   Or update images only:  ./opentranscribe.sh update${NC}" >&2
        return 1
    fi
    ref=${out%%$'\t'*}
    how=${out##*$'\t'}

    if [ "$how" = pinned ]; then
        echo -e "${BLUE}ℹ️  Updating config files at the pinned release: ${ref}${NC}" >&2
    else
        echo -e "${YELLOW}⚠️  This install is not pinned to a release; taking config from '${ref}'.${NC}" >&2
        echo -e "${YELLOW}   That config is not guaranteed to match your running images.${NC}" >&2
        echo -e "${YELLOW}   Pin it with:  ./opentranscribe.sh update --version vX.Y.Z${NC}" >&2
    fi
    printf '%s\n' "$ref"
}

function show_help {
    echo -e "${BLUE}OpenTranscribe Management Script${NC}"
    echo ""
    echo "Usage: ./opentranscribe.sh [command]"
    echo ""
    echo "Commands:"
    echo "  start         Start all services"
    echo "  stop          Stop all services"
    echo "  restart       Restart all services"
    echo "  status        Show container status"
    echo "  compose-files Print the resolved 'docker compose -f ...' chain"
    echo "  download-models [group]"
    echo "                Fetch model assets not bundled in the images. 'diar-native'"
    echo "                provisions the native diarizer's weights; omit for everything."
    echo "  logs [svc]    View logs (all or specific service)"
    echo "  update        Pull latest Docker images and restart"
    echo "  update-full   Update images AND configuration files"
    echo "  clean         Remove all volumes and data (CAUTION)"
    echo "  backup        Backup the database (optional --encrypt)"
    echo "  restore       Restore the database from a backup file (see --help below)"
    echo "  shell [svc]   Open shell in container (default: backend)"
    echo "  config        Show current configuration"
    echo "  health        Check service health"
    echo "  setup-ssl     Set up HTTPS with self-signed SSL certificates"
    echo "  version       Show version and check for updates"
    echo "  help          Show this help message"
    echo ""
    echo "Examples:"
    echo "  ./opentranscribe.sh start"
    echo "  ./opentranscribe.sh logs backend"
    echo "  ./opentranscribe.sh compose-files    # which overlays did it pick?"
    echo "  ./opentranscribe.sh download-models diar-native  # provision native diarizer weights"
    echo "  ./opentranscribe.sh update           # Update containers only"
    echo "  ./opentranscribe.sh update-full      # Update everything"
    echo "  ./opentranscribe.sh backup           # Dump the database to ./backups"
    echo "  ./opentranscribe.sh restore --yes ./backups/opentranscribe_backup_....sql"
    echo "  ./opentranscribe.sh setup-ssl"
    echo ""
}

check_environment() {
    if [ ! -f .env ]; then
        echo -e "${RED}❌ .env file not found${NC}"
        echo "Please run the setup script first."
        exit 1
    fi

    if [ ! -f docker-compose.yml ]; then
        echo -e "${RED}❌ docker-compose.yml not found${NC}"
        echo "Please run the setup script first."
        exit 1
    fi
}

# issue #709: `update-full`'s "new .env keys" report (below, in the update-full arm) can only
# see a key that is ABSENT. Two real cases on this branch are a key that is PRESENT but whose
# VALUE has rotted — correct when written, silently wrong now:
#
#   1. ENGINE_SHARED_VOLUME_PATH=/tmp/transcription — issue #661 E2 removed the
#      transcription-temp volume this path named. `os.makedirs` recreates it INSIDE the
#      writer's own container, so the write "succeeds" and the reader finds nothing; every
#      job silently drops to the MinIO round-trip fallback. `resolve_engine_shared_volume_path()`
#      (backend/app/core/constants.py) already self-heals this at runtime by preferring the
#      coded default when the configured path doesn't exist — that saves the install. This
#      warning is complementary, not a duplicate: it tells the OPERATOR their .env is drifting,
#      which the silent runtime fallback deliberately does not.
#   2. GPU_SCALE_WORKERS explicitly set higher than DIAR_NATIVE_MAX_INFLIGHT — defeats the
#      docker-compose.gpu-scale.yml derivation (`${GPU_SCALE_WORKERS:-${DIAR_NATIVE_MAX_INFLIGHT:-2}}`)
#      added specifically to prevent that contention (test_env_example_gpu_scale_derivation.py).
#
# Consulted by BOTH update-full's post-download report and preflight_upgrade_env, so a release
# that invalidates a value says so BEFORE teardown, while the operator can still act — same
# placement issue #670 uses for the native-diarizer refusal.
#
# ⚠️ WARN and name the fix. NEVER rewrite .env — this repo never edits an operator's .env
# without confirmation (see the "new .env keys" report below), and a silent correction here is
# exactly how the original bug (case 1) hid in the first place.
#
# To add a third case: append ONE row to STALE_ENV_CHECKS ("KEY|remedy text") and one `KEY)`
# arm to the case statement in check_stale_env_values() below — no new function, no new call
# site, no change to either caller.
STALE_ENV_CHECKS=(
    "ENGINE_SHARED_VOLUME_PATH|remove this line from .env (or set it to /scratch/opentranscribe/engine) — the path it names was removed by issue #661 E2's pipeline_scratch consolidation"
    "GPU_SCALE_WORKERS|comment this out in .env so docker-compose.gpu-scale.yml derives it from DIAR_NATIVE_MAX_INFLIGHT instead — an explicit value here can oversubscribe the diar-native sidecar's admission gate"
)

# Prints one "  • KEY=value — remedy" line per stale key found in $1 (default .env) to
# stdout; prints nothing when none are stale. Never modifies the file.
check_stale_env_values() {
    local env_file="${1:-.env}"
    [ -f "$env_file" ] || return 0

    local row key remedy val
    for row in "${STALE_ENV_CHECKS[@]}"; do
        key="${row%%|*}"
        remedy="${row#*|}"
        val=$(read_env_value "$key" "$env_file")
        [ -z "$val" ] && continue

        case "$key" in
            ENGINE_SHARED_VOLUME_PATH)
                [ "$val" = "/tmp/transcription" ] || continue
                ;;
            GPU_SCALE_WORKERS)
                local max_inflight
                max_inflight=$(read_env_value DIAR_NATIVE_MAX_INFLIGHT "$env_file")
                max_inflight="${max_inflight:-2}"
                case "$val" in
                    ''|*[!0-9]*) continue ;;
                esac
                case "$max_inflight" in
                    ''|*[!0-9]*) continue ;;
                esac
                [ "$val" -gt "$max_inflight" ] || continue
                ;;
            *)
                continue
                ;;
        esac

        echo "  • ${key}=${val} — ${remedy}"
    done
}

# The two DB commands are the only ones that need scripts/common.sh. Everything else still
# works standalone, so the source above is unconditional but this check is not — an install
# whose common.sh predates issue #613 (or is somehow missing) gets a remedy instead of an
# "unknown function" crash mid-backup/restore.
require_db_helpers() {
    if ! declare -F backup_database >/dev/null || ! declare -F restore_database >/dev/null; then
        echo -e "${RED}❌ scripts/common.sh is missing or too old — it provides the database${NC}"
        echo -e "${RED}   backup/restore implementation.${NC}"
        echo -e "${YELLOW}   Fix with:  ./opentranscribe.sh update-full${NC}"
        echo -e "${YELLOW}   Or fetch it directly:${NC}"
        echo -e "     mkdir -p scripts && curl -fsSL $(raw_url_for scripts/common.sh) -o scripts/common.sh && chmod +x scripts/common.sh"
        exit 1
    fi
}

fix_model_cache_permissions() {
    # Read MODEL_CACHE_DIR from .env if it exists
    local MODEL_CACHE_DIR=""
    if [ -f .env ]; then
        MODEL_CACHE_DIR=$(read_env_value MODEL_CACHE_DIR)
    fi

    # Use default if not set
    MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-./models}"

    if [ ! -d "$MODEL_CACHE_DIR" ]; then
        echo -e "${BLUE}📁 Creating model cache directory: $MODEL_CACHE_DIR${NC}"
    fi

    # Kept in lockstep with scripts/common.sh's subdirectory list — pinned by
    # backend/tests/unit/test_fix_model_cache_permissions_parity.py, do not let these two
    # lists diverge again. Unconditional (not gated behind "parent dir didn't exist"):
    # an install whose MODEL_CACHE_DIR already existed before diar-native was added to
    # this list never re-entered the old `if [ ! -d ... ]` branch, so diar-native was
    # never created here, and dockerd then created it root-owned on `compose up` (the
    # exact NOT_WRITABLE / exit-7 failure this function exists to prevent). `2>/dev/null`
    # matches common.sh's version — the ownership loop below only repairs directories
    # that exist, so creating this one is what lets the repair reach it.
    mkdir -p "$MODEL_CACHE_DIR/huggingface" "$MODEL_CACHE_DIR/torch" "$MODEL_CACHE_DIR/nltk_data" \
        "$MODEL_CACHE_DIR/sentence-transformers" "$MODEL_CACHE_DIR/opensearch-ml" \
        "$MODEL_CACHE_DIR/diar-native" 2>/dev/null

    # Check ownership of parent AND all subdirectories — a subdirectory can be
    # root-owned even when the parent is correctly owned by UID 1000 (e.g. dockerd
    # creating a bind-mount source that predates this fix). A parent-only check missed
    # exactly that case for diar-native.
    local needs_fix=false current_owner
    for dir in "$MODEL_CACHE_DIR" "$MODEL_CACHE_DIR"/*/; do
        [ -d "$dir" ] || continue
        current_owner=$(stat -c '%u' "$dir" 2>/dev/null || stat -f '%u' "$dir" 2>/dev/null || echo "unknown")
        if [ "$current_owner" != "1000" ]; then
            needs_fix=true
            break
        fi
    done

    if [ "$needs_fix" = true ]; then
        echo -e "${YELLOW}🔧 Fixing model cache permissions for non-root container (UID 1000)...${NC}"

        # Try using Docker to fix permissions (works without sudo)
        if command -v docker &> /dev/null; then
            if docker run --rm -v "$MODEL_CACHE_DIR:/models" busybox:latest sh -c "chown -R $CONTAINER_UID_GID /models && chmod -R 755 /models" > /dev/null 2>&1; then
                echo -e "${GREEN}✅ Model cache permissions fixed using Docker${NC}"
                return 0
            fi
        fi

        # Fallback: try direct chown if user has permissions
        if chown -R "$CONTAINER_UID_GID" "$MODEL_CACHE_DIR" > /dev/null 2>&1 && chmod -R 755 "$MODEL_CACHE_DIR" > /dev/null 2>&1; then
            echo -e "${GREEN}✅ Model cache permissions fixed${NC}"
            return 0
        fi

        # If both methods fail, show warning
        echo -e "${YELLOW}⚠️  Warning: Could not automatically fix model cache permissions${NC}"
        echo "   If you encounter permission errors, run: ./scripts/fix-model-permissions.sh"
        return 1
    fi

    return 0
}

detect_nvidia_runtime() {
    # Check if NVIDIA Container Runtime is available
    if docker info 2>/dev/null | grep -q "Runtimes.*nvidia"; then
        echo "nvidia"
    else
        echo "default"
    fi
}

is_blackwell_gpu() {
    # Detect Blackwell architecture (compute capability 12.x)
    # DGX Spark / GB10 GPUs report compute_cap=12.1 via nvidia-smi
    local compute_cap
    compute_cap=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '[:space:]')
    [[ "$compute_cap" == 12.* ]]
}

# force_cpu_mode_requested: returns success (0) when the user opted out of
# GPU acceleration at install time (setup-opentranscribe.sh --cpu, or the
# OPENTRANSCRIBE_FORCE_CPU env var), which persisted FORCE_CPU_MODE=true to
# .env. When true, get_compose_files skips the GPU overlay(s) regardless of
# whether Docker reports an nvidia runtime. This is the authoritative signal
# because Docker's runtime presence is a necessary but not sufficient
# condition for a working GPU (e.g. WSL2 with toolkit installed but no
# adapter passthrough still advertises the runtime). Also honors
# OPENTRANSCRIBE_FORCE_CPU set in the current shell for one-off overrides.
force_cpu_mode_requested() {
    if [ -n "${OPENTRANSCRIBE_FORCE_CPU:-}" ]; then
        return 0
    fi
    if [ -f .env ]; then
        local value
        value=$(read_env_value FORCE_CPU_MODE)
        [ "$value" = "true" ]
        return
    fi
    return 1
}

# Pin DIAR_NATIVE_IMAGE to the Blackwell tag when this deployment will load
# docker-compose.blackwell.yml, so docker-compose.diar-native.yml's own
# `${DIAR_NATIVE_IMAGE:-...}` interpolation resolves to it instead of falling through to
# the plain `${OT_IMAGE_TAG:-latest}` tag — the exact `celery-worker -> :blackwell` /
# `diar-native -> :latest` mismatch docker-compose.blackwell.yml's own comment documents.
# A retag inside that compose file cannot fix it: get_compose_files() always appends
# docker-compose.blackwell.yml BEFORE docker-compose.diar-native.yml, and compose's
# last-file-wins merge means diar-native.yml's `image:` key always overrides whatever the
# earlier file set, regardless of what that file's `image:` line says.
#
# MUST be called as a plain statement, never through `$(...)`: every call site below
# resolves the compose chain with `compose_files=$(get_compose_files)`, which forks a
# subshell for the whole command substitution — an `export` from inside get_compose_files
# itself (or any wrapper invoked the same way) would vanish the instant that subshell
# exits, before the `docker compose` command that actually needs it ever runs. Calling
# this separately, first, keeps the export in THIS shell.
#
# But an in-process `export` is not enough on its own, for a second reason: the
# `compose-files` arm's own DOCUMENTED usage is
# `docker compose $(./opentranscribe.sh compose-files 2>/dev/null) up` — the whole
# script runs as a SEPARATE PROCESS there, so any export made inside it dies with that
# process the instant `compose-files` finishes printing, before the caller's `docker
# compose up` ever starts. `docker compose` re-reads `.env` off disk on every invocation
# regardless of shell/process, so persisting the pin there (once, non-destructively) is
# what makes it visible to that usage too — the in-process export below still matters for
# every OTHER call site here, which builds `$compose_files` and runs `docker compose` in
# the SAME process.
pin_diar_native_image_for_blackwell() {
    force_cpu_mode_requested && return 0
    [ "$(detect_nvidia_runtime)" = "nvidia" ] || return 0
    is_blackwell_gpu || return 0
    [ -f docker-compose.blackwell.yml ] || return 0

    # An operator's own .env pin (or a private-registry mirror) always wins. Reading it
    # through read_env_value — not a bare `${DIAR_NATIVE_IMAGE:-...}` shell-env
    # expansion — is what lets it win: a value an operator (or an earlier run of this
    # same function, see the persistence below) put in .env is invisible to a bare shell
    # expansion unless something already exported it into this process.
    local existing
    existing=$(read_env_value DIAR_NATIVE_IMAGE)
    if [ -n "$existing" ]; then
        export DIAR_NATIVE_IMAGE="$existing"
        return 0
    fi

    local tag pinned
    tag=$(read_env_value OT_BLACKWELL_IMAGE_TAG)
    pinned="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend:${tag:-blackwell}"
    export DIAR_NATIVE_IMAGE="$pinned"

    # Persist so a later, separate `docker compose` invocation (including the
    # compose-files subshell usage above, and `download-models diar-native` via
    # resolve_diar_native_downloader_image's own DIAR_NATIVE_IMAGE read) resolves the
    # same image without needing this function to have run first in that process. Only
    # writes when the key is truly absent — re-checked with a raw grep rather than
    # read_env_value's parsed form, so a key present-but-empty (an operator's deliberate
    # "do not pin" choice) is never clobbered.
    if [ -f .env ] && ! grep -qE '^[[:space:]]*(export[[:space:]]+)?DIAR_NATIVE_IMAGE=' .env 2>/dev/null; then
        printf '\nDIAR_NATIVE_IMAGE=%s\n' "$pinned" >> .env
    fi
}

# Whether this deployment should run GPU split — celery-worker-gpu-transcribe /
# celery-worker-gpu-diarize on separate host GPUs (issue #708).
#
# Gated on the SAME variable app/core/constants.py's gpu_split_enabled() reads to route
# dispatch onto those two queues. One operator-facing switch for both halves: set
# ENGINE_GPU_SPLIT=true in .env and the app routes to the split queues AND this script
# loads the overlay that gives those queues a consumer at all. Before this function
# existed, opentranscribe.sh had no reference to gpu-split whatsoever — an operator who
# set ENGINE_GPU_SPLIT=true got the app-side routing with nothing to receive it (the
# exact silent-misconfiguration issue #703's live-consumer check now falls back safely
# from, but the fallback is "process on the shared gpu queue", not "actually split").
#
# Also requires an nvidia runtime and no FORCE_CPU_MODE opt-out — same probe
# get_compose_files() uses for the plain GPU overlay — since a GPU-reservation overlay
# cannot load on a host that decided not to use its GPU at all.
gpu_split_active() {
    [ -f docker-compose.gpu-split.yml ] || return 1
    local split_enabled
    split_enabled=$(read_env_value ENGINE_GPU_SPLIT | tr '[:upper:]' '[:lower:]')
    [ "$split_enabled" = "true" ] || return 1
    force_cpu_mode_requested && return 1
    [ "$(detect_nvidia_runtime)" = "nvidia" ] || return 1
    return 0
}

# Companion to pin_diar_native_image_for_blackwell, same calling contract: MUST be
# called as a plain statement, before `compose_files=$(get_compose_files)`, never
# through `$(...)`. An `export COMPOSE_PROFILES=...` made inside a function invoked via
# command substitution dies with that subshell before the `docker compose` command that
# needs it ever runs — the identical reasoning documented on that function above.
#
# docker-compose.yml gates celery-worker-gpu-transcribe / celery-worker-gpu-diarize
# behind `profiles: [gpu-split]`, so appending docker-compose.gpu-split.yml to the `-f`
# chain (in get_compose_files(), below) is not enough on its own to bring them up —
# COMPOSE_PROFILES has to name that profile too.
pin_gpu_split_profile() {
    if gpu_split_active; then
        export COMPOSE_PROFILES="gpu-split"
    fi
}

# Resolve where the diar-native ONNX/PLDA export lives (or will land), from .env alone.
#
# This is the shipped, standalone script, so — unlike opentr.sh's dev-only same-named
# helper — there is no machine-local legacy path to fall back to: just the explicit
# override, then the standard MODEL_CACHE_DIR-relative location every installer and
# `download-models diar-native` agree on.
resolve_diar_native_models_dir() {
    local dir
    dir=$(read_env_value DIAR_NATIVE_MODELS_DIR)
    if [ -z "$dir" ]; then
        local cache_dir
        cache_dir=$(read_env_value MODEL_CACHE_DIR)
        dir="${cache_dir:-./models}/diar-native"
    fi
    echo "$dir"
}

# Whether $1 (a models directory) carries a provisioning marker worth trusting.
#
# This cannot be the same check diar-server itself makes — that lives in a Rust binary
# inside the backend image, and this runs before we even know the new image is pullable.
# It approximates: present, non-empty and (where python3 exists) valid JSON. That is
# strictly weaker than the binary's own validation, which also checks the recorded
# exporter/model-set version — so this can call a marker "present" that `provision-models`
# would still choose to re-export. It only needs to answer one question: has this
# directory EVER been provisioned, or is native diarization certain to silently become
# PyAnnote the moment this upgrade lands.
diar_native_marker_present() {
    local marker="$1/diar-provision.json"
    [ -s "$marker" ] || return 1
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$marker" >/dev/null 2>&1
        return $?
    fi
    return 0
}

get_compose_files() {
    local compose_files="-f docker-compose.yml"

    # Production deployment always uses prod overrides
    if [ -f docker-compose.prod.yml ]; then
        compose_files="$compose_files -f docker-compose.prod.yml"
    fi

    # Add GPU overlay if NVIDIA runtime is available and overlay exists,
    # unless the user explicitly chose CPU-only mode at install time.
    local docker_runtime
    docker_runtime=$(detect_nvidia_runtime)
    if force_cpu_mode_requested; then
        if [ "$docker_runtime" = "nvidia" ]; then
            echo -e "${BLUE}🧮 CPU-only mode (FORCE_CPU_MODE=true in .env) — skipping GPU overlay despite nvidia runtime being available${NC}" >&2
        fi
    elif [ "$docker_runtime" = "nvidia" ]; then
        if is_blackwell_gpu && [ -f docker-compose.blackwell.yml ]; then
            compose_files="$compose_files -f docker-compose.blackwell.yml"
            echo -e "${BLUE}Blackwell GPU overlay enabled (SM_12x detected)${NC}" >&2
        elif [ -f docker-compose.gpu.yml ]; then
            compose_files="$compose_files -f docker-compose.gpu.yml"
            echo -e "${BLUE}GPU acceleration enabled (NVIDIA Container Toolkit detected)${NC}" >&2
        fi
    fi

    # Add NGINX overlay if NGINX_SERVER_NAME is configured
    local nginx_server_name=""
    nginx_server_name=$(read_env_value NGINX_SERVER_NAME)

    if [ -n "$nginx_server_name" ] && [ -f docker-compose.nginx.yml ]; then
        # Check for SSL certificates
        local cert_file="${NGINX_CERT_FILE:-./nginx/ssl/server.crt}"
        local key_file="${NGINX_CERT_KEY:-./nginx/ssl/server.key}"

        if [ -f "$cert_file" ] && [ -f "$key_file" ]; then
            compose_files="$compose_files -f docker-compose.nginx.yml"
            echo -e "${BLUE}🔒 HTTPS enabled (NGINX reverse proxy with SSL)${NC}" >&2
            echo -e "${BLUE}   Server name: $nginx_server_name${NC}" >&2
        else
            echo -e "${YELLOW}⚠️  NGINX_SERVER_NAME is set but SSL certificates not found${NC}" >&2
            echo -e "${YELLOW}   Expected: $cert_file and $key_file${NC}" >&2
            echo -e "${YELLOW}   Generate with: ./opentranscribe.sh setup-ssl${NC}" >&2
            echo -e "${YELLOW}   Continuing without HTTPS...${NC}" >&2
        fi
    fi

    # Add the scheduled-backup overlay when explicitly opted into. Deliberately keyed on a
    # DEDICATED toggle, not on BACKUP_HOST_PATH being non-empty: .env.example ships
    # BACKUP_HOST_PATH=./backups SET, so a non-empty test would enable this for every
    # install by default — and this overlay also sets `path.repo` on the opensearch
    # service, so that would force-recreate OpenSearch on every existing deployment's
    # next `update`. Same trap .env.example already documents for MEDIA_NAS_PATH (#597).
    # BACKUP_HOST_PATH stays what it has always been: WHERE the mount points, not WHETHER.
    local backup_overlay_enabled=""
    backup_overlay_enabled=$(read_env_value BACKUP_OVERLAY_ENABLED)
    if [ "$backup_overlay_enabled" = "true" ] && [ -f docker-compose.backup.yml ]; then
        compose_files="$compose_files -f docker-compose.backup.yml"
        echo -e "${BLUE}💾 Scheduled-backup overlay enabled (BACKUP_OVERLAY_ENABLED=true)${NC}" >&2
        echo -e "${BLUE}   Destination: ${BACKUP_HOST_PATH:-./backups} → /backups${NC}" >&2
        echo -e "${YELLOW}   Note: this also sets path.repo on OpenSearch — the opensearch container will be recreated.${NC}" >&2
    fi

    # Add the native diarization sidecar when its weights have been exported, OR when
    # they can still be — i.e. a HUGGINGFACE_TOKEN is on file for the backend's own
    # startup provisioning (backend/app/transcription/native_provision.py) to use.
    #
    # engine.diarizer_backend defaults to `native`, but before issue #639 no self-hosted
    # deployment could ever run the sidecar: the overlay was not in release-manifest.txt,
    # so it never reached disk, and this script had no reference to it at all. Every
    # install therefore served every file from the in-process PyAnnote fallback while
    # the config, the docs and the admin UI all said `native`.
    #
    # Weights-present was the ONLY signal until issue #654's fix: `download-models
    # diar-native` was advertised but did not exist, so this arm could never actually
    # fire from a fresh install. Now that the backend provisions its own weights on
    # startup (given a token), gating on the weights already existing would need TWO
    # `update`/`start` cycles to converge on a fresh install — one for the backend to
    # provision, a second for this script to notice. A configured HUGGINGFACE_TOKEN is
    # what lets that provisioning step succeed, so it stands in for "weights exist" until
    # they do. With neither signal, nothing can produce the weights and loading the
    # overlay would just crash-loop the sidecar (empty bind-mount source).
    #
    # read_env_value does NOT expand `${VAR}` references inside a value — a
    # HUGGINGFACE_TOKEN written as `HUGGINGFACE_TOKEN=${SOME_OTHER_VAR}` reads back
    # literally and passes this non-empty check without being a usable token. That is
    # the same limitation every other read_env_value call in this file already lives
    # with; not new here.
    local diar_models_dir=""
    diar_models_dir=$(resolve_diar_native_models_dir)

    local diar_weights_present="0"
    if [ -d "$diar_models_dir" ] && [ -n "$(ls -A "$diar_models_dir" 2>/dev/null)" ]; then
        diar_weights_present="1"
    fi
    local diar_hf_token=""
    diar_hf_token=$(read_env_value HUGGINGFACE_TOKEN)

    # This gate used to ignore ENGINE_DIARIZER_BACKEND entirely, so
    # docker-compose.diar-native.yml's own documented rollback ("set
    # ENGINE_DIARIZER_BACKEND=pyannote and stop this service") was defeated the moment the
    # operator next ran `start`/`update`: weights-or-token was still true, so the overlay
    # reloaded regardless of the variable — even though preflight_upgrade_env below reads
    # this exact same variable for the exact same feature. Same read here, same default.
    # backend/app/transcription/config.py:357-366 resolves this value with
    # .strip().lower() and fail-safes anything unrecognised to "native" — a
    # case-sensitive compare here read 'Native'/'NATIVE' as "not native" and silently
    # skipped the sidecar the backend was about to use anyway. Normalise the same way.
    local diar_backend=""
    diar_backend=$(read_env_value ENGINE_DIARIZER_BACKEND | tr '[:upper:]' '[:lower:]')

    # Lite is NOT excluded. It was, on the premise that native_provision.py skipped
    # provisioning under DEPLOYMENT_MODE=lite because the lite image shipped no Python
    # exporter toolchain — so /models could never fill itself in and the overlay would
    # crash-loop diar-server against an empty --models-dir (`diar-server serve` with an
    # empty DIAR_MODELS_DIR exits 8; that is still true, and is what the weights/token
    # gate below protects against).
    #
    # The premise removed the feature it was protecting. The ONNX/PLDA graphs are
    # non-redistributable derivatives of gated weights, so a deployment that cannot export
    # cannot obtain them at all — excluding lite guaranteed it had no local voiceprint
    # path, on the one local model job a cloud-ASR deployment still has. `diar-server`
    # carries the export itself (its Python scripts are compiled into the binary, which
    # Dockerfile.lite already copies in); only the packages those scripts import were
    # missing, and requirements-lite.txt now installs them. Lite provisions itself on
    # first boot like any other deployment, so it goes through the same gate: weights
    # present, or a token configured to produce them.
    if [ "${diar_backend:-native}" = "native" ] \
       && { [ "$diar_weights_present" = "1" ] || [ -n "$diar_hf_token" ]; }; then
        if [ -f docker-compose.diar-native.yml ]; then
            compose_files="$compose_files -f docker-compose.diar-native.yml"
            if [ "$diar_weights_present" = "1" ]; then
                echo -e "${BLUE}🎙️  Native diarization sidecar enabled (weights present)${NC}" >&2
                echo -e "${BLUE}   Weights: ${diar_models_dir} → /models${NC}" >&2
            else
                echo -e "${BLUE}🎙️  Native diarization sidecar enabled (HUGGINGFACE_TOKEN set — backend will provision weights on startup)${NC}" >&2
                echo -e "${BLUE}   Weights will land at: ${diar_models_dir} → /models${NC}" >&2
            fi
            # The overlay above is CPU-safe by construction (no device reservation,
            # DIAR_MODE defaults to cpu) so it can load on a GPU-less or FORCE_CPU_MODE
            # host. The reservation and the `cuda` override are in a second file, added
            # only when a GPU overlay was actually selected above — keyed off the same
            # $docker_runtime probe, so the sidecar can never claim a device the rest of
            # the stack decided not to use (#660).
            if [ "$docker_runtime" = "nvidia" ] && ! force_cpu_mode_requested \
               && [ -f docker-compose.diar-native-gpu.yml ]; then
                compose_files="$compose_files -f docker-compose.diar-native-gpu.yml"
                echo -e "${BLUE}   Holds ~2.2 GB of GPU memory on device ${DIAR_NATIVE_GPU:-${GPU_DEVICE_ID:-0}} while up.${NC}" >&2
            else
                # "identical output" was RETRACTED upstream (#679), and the replacement
                # claim of bit-identity did not survive measurement either: 2026-09-04,
                # two real sidecars, CPU-vs-CUDA max delta 4.11e-04 (cosine 0.999999816),
                # with CUDA differing from ITSELF by 2.86e-04 run to run. Embeddings are
                # EQUIVALENT for speaker matching, which is what makes CPU routing safe
                # for the embedding path — but
                # diarization segment boundaries can differ by up to one segmentation frame
                # (0.016875 s) when a posterior lands on the binarisation threshold. Below
                # anything a transcript renders, but it must never be stated as identical:
                # an operator reading that would be entitled to diff two runs and expect a
                # match. Kept in parity with opentr.sh's wording.
                echo -e "${BLUE}   Running on CPU — slower; embeddings identical, diarization${NC}" >&2
                echo -e "${BLUE}   boundaries may differ by up to 0.016875s (#679).${NC}" >&2
            fi
        else
            # Loud, unlike the GPU overlays' silent `[ -f ]` fallthrough: the operator
            # exported these weights (or configured a token to) on purpose, and quietly
            # serving PyAnnote instead is the exact defect #639 is about.
            echo -e "${YELLOW}⚠️  Native diarization is configured but docker-compose.diar-native.yml is missing.${NC}" >&2
            echo -e "${YELLOW}   Diarization will fall back to the in-process PyAnnote engine.${NC}" >&2
            echo -e "${YELLOW}   Run './opentranscribe.sh update-full' to fetch it.${NC}" >&2
        fi
    fi

    # GPU split overlay (issue #708): separate GPUs for transcription vs diarization.
    # gpu_split_active() is the single gate; pin_gpu_split_profile() (called earlier, as
    # a plain statement, by every arm that reaches here) already exported
    # COMPOSE_PROFILES=gpu-split so the profile-gated services in docker-compose.yml
    # actually come up alongside this overlay's GPU reservations for them.
    if gpu_split_active; then
        compose_files="$compose_files -f docker-compose.gpu-split.yml"
        echo -e "${BLUE}🔀 GPU split overlay enabled (ENGINE_GPU_SPLIT=true) — transcription and diarization run on separate GPUs${NC}" >&2
        echo -e "${BLUE}   GPU_TRANSCRIBE_DEVICE_ID / GPU_DIARIZE_DEVICE_ID default to 0/1 — set both in .env if that is wrong for this host.${NC}" >&2
    fi

    echo "$compose_files"
}

# issue #656 (remaining item): a diarization surface for `status`, shown only when
# docker-compose.diar-native.yml is actually in the resolved compose chain — the same test
# `get_compose_files` above already decided the answer to, so we only need to look at its
# output rather than re-deriving anything.
#
# ⚠️ This answers "can the sidecar serve RIGHT NOW", nothing else. It deliberately does NOT
# derive "which engine served a given file" — that is `media_file.diarization_provider`
# (issue #706), and deriving it from the *configured* value (rather than what actually ran)
# is the exact bug #706 closed. Don't reintroduce it here.
#
# ⚠️ /healthz reports two different device lists: `devices` (what is actually LOADED) and
# `supported_devices` (build-time capability, i.e. "could be loaded"). Only `devices` belongs
# in an operator-facing status line — conflating the two caused a live outage on this branch.
print_diar_native_status() {
    local compose_files="$1"

    case "$compose_files" in
        *docker-compose.diar-native.yml*) ;;
        *) return 0 ;;
    esac

    echo ""
    echo -e "${BLUE}Diarization:${NC}"

    local configured
    configured=$(read_env_value ENGINE_DIARIZER_BACKEND | tr '[:upper:]' '[:lower:]')
    echo "  configured  ${configured:-native}            (ENGINE_DIARIZER_BACKEND / engine.diarizer_backend)"

    # shellcheck disable=SC2086  # intentional word-splitting of the -f chain
    local health_json ready_code
    health_json=$(docker compose $compose_files exec -T diar-native curl -sf localhost:8701/healthz 2>/dev/null)
    # shellcheck disable=SC2086
    ready_code=$(docker compose $compose_files exec -T diar-native \
        curl -s -o /dev/null -w '%{http_code}' localhost:8701/readyz 2>/dev/null)

    if [ -z "$health_json" ]; then
        echo -e "  sidecar     ${RED}unreachable${NC}         (/healthz did not respond — container down or not yet started)"
    else
        local models_state models_reason devices
        models_state=$(printf '%s' "$health_json" | python3 -c \
            'import json,sys; print(json.load(sys.stdin).get("models_state",""))' 2>/dev/null)
        models_reason=$(printf '%s' "$health_json" | python3 -c \
            'import json,sys; print(json.load(sys.stdin).get("models_reason") or "")' 2>/dev/null)
        # NOT supported_devices — that is build-time capability, not what is loaded.
        devices=$(printf '%s' "$health_json" | python3 -c \
            'import json,sys; d=json.load(sys.stdin).get("devices"); print(",".join(d) if isinstance(d, list) else (d or ""))' 2>/dev/null)

        if [ "$ready_code" = "200" ]; then
            echo -e "  sidecar     ${GREEN}ready${NC}               (/healthz 200, /readyz 200${devices:+, devices=$devices})"
        else
            echo -e "  sidecar     ${YELLOW}not ready${NC}           (/healthz 200, /readyz ${ready_code:-no response})"
            if [ -n "$models_state" ]; then
                echo "              models_state=${models_state}${models_reason:+ — $models_reason}"
            fi
        fi
    fi

    local max_inflight
    max_inflight=$(read_env_value DIAR_NATIVE_MAX_INFLIGHT)
    max_inflight="${max_inflight:-2}"
    echo "  admission   ${max_inflight} permits       DIAR_NATIVE_MAX_INFLIGHT"

    # Same predicate as backend/tests/unit/test_env_example_gpu_scale_derivation.py — one
    # rule, two consumers. Do not write a second copy of this comparison.
    local gpu_scale_workers effective_workers
    gpu_scale_workers=$(read_env_value GPU_SCALE_WORKERS)
    effective_workers="${gpu_scale_workers:-$max_inflight}"
    if echo "$effective_workers" | grep -qE '^[0-9]+$' && echo "$max_inflight" | grep -qE '^[0-9]+$' \
       && [ "$effective_workers" -gt "$max_inflight" ]; then
        echo -e "  workers     ${effective_workers}                 ${YELLOW}⚠️  GPU_SCALE_WORKERS exceeds the sidecar's permits${NC}"
    else
        echo "  workers     ${effective_workers}                 GPU_SCALE_WORKERS (effective, after derivation)"
    fi

    local timeout_s
    timeout_s=$(read_env_value DIAR_NATIVE_TIMEOUT_S)
    echo "  timeout     ${timeout_s:-1800}s ceiling    DIAR_NATIVE_TIMEOUT_S"
}

# Refuse an upgrade that the new backend will reject, BEFORE tearing anything down.
#
# v0.5.0 flipped security enforcement from fail-open to fail-closed (#284 A0.3):
# ENVIRONMENT now defaults to production, so a v0.4.x deployment that never set it
# skipped every production secret check and now gets all of them. The first
# symptom was the backend exiting with "REDIS_PASSWORD is required in production
# environment" AFTER `down` had already run -- stack stopped, new one refusing to
# boot, user holding a stack-trace (#410).
#
# Checking first turns that into a refusal with a remedy, while the old stack is
# still running.
preflight_upgrade_env() {
    local problems=()

    # Relaxed environments opt out of all of this, exactly as the backend does.
    local env_name
    env_name=$(read_env_value ENVIRONMENT | tr '[:upper:]' '[:lower:]')
    case "$env_name" in
        development|dev|testing|test|local) return 0 ;;
    esac

    # REDIS_PASSWORD / JWT_SECRET_KEY / ENCRYPTION_KEY deliberately keep the raw
    # `cut -d= -f2-` form rather than read_env_value: a secret may legitimately
    # contain a `#` (even ` #`), and read_env_value's inline-comment stripping
    # would silently truncate it.
    local redis_pw
    redis_pw=$(grep -E '^REDIS_PASSWORD=' .env 2>/dev/null | cut -d= -f2- | tr -d ' "' | head -1)
    [ -z "$redis_pw" ] && problems+=("REDIS_PASSWORD is empty or missing")

    local jwt
    jwt=$(grep -E '^JWT_SECRET_KEY=' .env 2>/dev/null | cut -d= -f2- | tr -d ' "' | head -1)
    case "$jwt" in
        ""|*change_me*|*CHANGE_ME*) problems+=("JWT_SECRET_KEY is unset or still a placeholder") ;;
    esac

    local enc
    enc=$(grep -E '^ENCRYPTION_KEY=' .env 2>/dev/null | cut -d= -f2- | tr -d ' "' | head -1)
    case "$enc" in
        ""|*change_me*|*CHANGE_ME*) problems+=("ENCRYPTION_KEY is unset or still a placeholder") ;;
    esac

    # issue #670: engine.diarizer_backend's coded (and DB-configured) default is `native`,
    # and backend/app/transcription/native_provision.py deliberately never aborts startup
    # over a failed export — a degraded diarizer is a supported configuration, not a crash
    # (that is what makes it safe to run on every boot). So nothing else ever tells the
    # operator that an upgrade just silently traded the native engine for the in-process
    # PyAnnote fallback; this is the one place left to say so, before the current stack is
    # torn down and the outcome is already decided. Same DB-blindness as get_compose_files:
    # this script cannot see the SystemSettings value, only the ENGINE_DIARIZER_BACKEND
    # .env fallback read below.
    #
    # The HARD refusal below prints an actionable remedy ("run download-models diar-native,
    # or set HUGGINGFACE_TOKEN"), and since #654 restored the export toolchain to
    # requirements-lite.txt that remedy now works on lite too — lite provisions itself on
    # first boot exactly like a full install. (This comment previously said the opposite,
    # citing native_provision.py's EXIT_NO_EXPORTER_ENV remedy; that remedy has since been
    # updated and the claim is dead.)
    #
    # Lite is nonetheless kept on warn-don't-block, now for a different and narrower reason:
    # an UPGRADE is the wrong moment to start hard-refusing a deployment shape that has been
    # running fine, and lite's requirements-lite.txt ships pyannote.audio (CPU) so the
    # in-process engine remains a working fallback while the operator sorts out a token.
    # The hard refusal stays everywhere else — same case issue #670 was written for.
    local deployment_mode
    deployment_mode=$(read_env_value DEPLOYMENT_MODE | tr '[:upper:]' '[:lower:]')

    # Same case-fold as get_compose_files' identical gate above: the backend resolves
    # this value case-insensitively (config.py:357-366) and fail-safes unknowns to
    # "native", so a case-sensitive compare here ('Native', 'NATIVE') would silently
    # skip the #670 preflight guard for a backend that is about to run native anyway.
    local diar_backend
    diar_backend=$(read_env_value ENGINE_DIARIZER_BACKEND | tr '[:upper:]' '[:lower:]')
    if [ "${diar_backend:-native}" = "native" ]; then
        local diar_dir diar_token
        diar_dir=$(resolve_diar_native_models_dir)
        diar_token=$(read_env_value HUGGINGFACE_TOKEN)
        if ! diar_native_marker_present "$diar_dir" && [ -z "$diar_token" ]; then
            if [ "$deployment_mode" = "lite" ]; then
                echo -e "${YELLOW}⚠️  engine.diarizer_backend resolves to native, but this is a lite deployment${NC}"
                echo -e "${YELLOW}   (DEPLOYMENT_MODE=lite): $diar_dir has never been provisioned and no${NC}"
                echo -e "${YELLOW}   HUGGINGFACE_TOKEN is set — a lite image cannot export it locally.${NC}"
                echo -e "${YELLOW}   Diarization will use the in-process PyAnnote engine; not blocking the upgrade.${NC}"
            else
                problems+=("engine.diarizer_backend resolves to native, but $diar_dir has never been provisioned and no HUGGINGFACE_TOKEN is set to provision it on next startup — run './opentranscribe.sh download-models diar-native' first, or set HUGGINGFACE_TOKEN in .env")
            fi
        fi
    fi

    # Issue #661 E2: production installs have NEVER had a pipeline_scratch chown.
    # `fix_pipeline_scratch_permissions` (scripts/common.sh) is opentr.sh-only; this curl-style
    # installer sources scripts/common.sh only conditionally (see the `if [ -f
    # ./scripts/common.sh ]` guard near the top of this file), and a fresh curl install has no
    # such file at all yet, so fall back to an inline chown using the same CONTAINER_UID_GID
    # this script already uses for MODEL_CACHE_DIR above. Best-effort and non-blocking (never
    # added to `problems`): a stale volume here degrades to the MinIO fallback, it doesn't
    # break the upgrade.
    if command -v fix_pipeline_scratch_permissions > /dev/null 2>&1; then
        fix_pipeline_scratch_permissions
    elif command -v docker > /dev/null 2>&1; then
        local scratch_vol
        scratch_vol=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E '_pipeline_scratch$' | head -1)
        if [ -n "$scratch_vol" ]; then
            docker run --rm -v "$scratch_vol:/scratch" alpine:3 \
                sh -c "mkdir -p /scratch/engine /scratch/diar && chown -R $CONTAINER_UID_GID /scratch && chmod 775 /scratch/engine /scratch/diar" \
                > /dev/null 2>&1 \
                && echo -e "${BLUE}🔧 pipeline_scratch permissions checked/fixed${NC}" \
                || echo -e "${YELLOW}⚠️  Could not fix pipeline_scratch permissions — scratch handoff may fall back to MinIO${NC}"
        fi
    fi

    # A sidecar container created before this consolidation keeps its OLD mount set until it
    # is RECREATED, not merely restarted (diarizer_native.py documents the identical failure
    # in reverse for the original transcription-temp mount). A sidecar left on the old mounts
    # degrades to PyAnnote SILENTLY, so tell the operator here rather than let them discover
    # it via a slow-and-unexplained diarization.
    if [ "${diar_backend:-native}" = "native" ]; then
        echo -e "${YELLOW}ℹ️  This release consolidates the pipeline scratch volumes onto one${NC}"
        echo -e "${YELLOW}   ('pipeline_scratch'). If you run the diar-native sidecar, RECREATE it${NC}"
        echo -e "${YELLOW}   (not just restart) after this upgrade:${NC}"
        echo "     docker compose ... up -d --force-recreate diar-native"
    fi

    # issue #709: stale .env VALUES (key present, value rotted) — warn only, never blocking
    # and never rewritten. Printed here, before teardown, alongside the other preflight
    # findings above.
    local stale_findings
    stale_findings=$(check_stale_env_values .env)
    if [ -n "$stale_findings" ]; then
        echo -e "${YELLOW}⚠️  .env has settings that look present but whose VALUE has gone stale:${NC}"
        echo "$stale_findings"
        echo -e "${YELLOW}   Your .env was NOT modified — update these manually before/after upgrading.${NC}"
        echo ""
    fi

    [ ${#problems[@]} -eq 0 ] && return 0

    echo -e "${RED}❌ This release enforces production secrets that your .env does not satisfy.${NC}"
    echo -e "${RED}   Refusing to upgrade — your current stack is untouched and still running.${NC}"
    echo ""
    for p in "${problems[@]}"; do echo "   • $p"; done
    echo ""
    echo -e "${YELLOW}Why now:${NC} security enforcement moved from fail-open to fail-closed."
    echo "  Previously ENVIRONMENT defaulted to \"development\", so these checks were skipped"
    echo "  on any deployment that never set it. They now apply by default."
    echo ""
    echo -e "${YELLOW}To fix:${NC}"
    [ -z "$redis_pw" ] && echo "  echo \"REDIS_PASSWORD=\$(openssl rand -hex 16)\" >> .env"
    echo "  # then re-run: ./opentranscribe.sh update"
    echo ""
    echo "  A single-user install on a trusted network can instead set ENVIRONMENT=development,"
    echo "  but that disables every hardening control."
    return 1
}

# Bring the stack down for an upgrade, tolerating the stale-network race.
#
# `docker compose down` removes the containers and then the network. The daemon
# occasionally keeps a stale endpoint record for an already-removed container, so
# the network removal fails with "has active endpoints" even though every
# container is gone. compose reports that as an overall failure and, with the
# upgrade wired to abort on it, a routine `update` dies half-way -- containers
# down, nothing brought back up.
#
# A user cannot restart the Docker daemon to get out of that, so treat it for
# what it is: the teardown succeeded, only the cleanup of an empty network
# raced. Verify no containers remain, clear the network if it is genuinely
# empty, and continue. Any other failure is still fatal.
compose_down_for_upgrade() {
    local compose_files="$1"

    # shellcheck disable=SC2086  # intentional word-splitting of the -f chain
    if docker compose $compose_files down; then
        return 0
    fi

    local remaining
    remaining=$(docker ps -aq --filter "name=^opentranscribe-" | wc -l)
    if [ "$remaining" -ne 0 ]; then
        echo -e "${RED}❌ Teardown failed and $remaining container(s) remain${NC}"
        return 1
    fi

    local net=opentranscribe_default
    if docker network inspect "$net" >/dev/null 2>&1; then
        local attached
        attached=$(docker network inspect "$net" --format '{{len .Containers}}' 2>/dev/null || echo 0)
        if [ "$attached" = "0" ]; then
            echo -e "${YELLOW}⚠️  Clearing stale empty network '$net' (daemon endpoint race)${NC}"
            docker network rm "$net" >/dev/null 2>&1 || true
        fi
    fi

    echo -e "${GREEN}✓ All containers removed; continuing upgrade${NC}"
    return 0
}

# Bring the stack up in phases, polling the backend's own /health rather than
# letting compose's dependency resolver decide when the backend is ready.
#
# Compose gives up on a `service_healthy` wait long before the backend's 600s
# start_period elapses on a populated database. Running the Alembic chain plus
# the model warm-preload on first boot after an upgrade routinely takes 60-120
# seconds; compose reports "dependency failed to start: container
# opentranscribe-backend is unhealthy" around the ~45s mark and SIGTERMs the
# backend MID-MIGRATION.
#
# Shared by `update` and `update-full`. It used to live inline in `update` only,
# so `update-full` — the command people run when crossing releases, and therefore
# the one MOST likely to run a long migration chain — did a bare `up -d` and was
# exposed to exactly this failure.
#
# Usage: perform_phased_restart "$compose_files"   (returns non-zero on failure)
perform_phased_restart() {
    local compose_files="$1"
    local backend_port waited max_wait state

    backend_port=$(read_env_value BACKEND_PORT)
    backend_port="${backend_port:-5174}"

    # Phase 1: infrastructure + backend only, no dependents.
    echo -e "${BLUE}▶ Starting infrastructure + backend (phase 1/2)...${NC}"
    # shellcheck disable=SC2086  # intentional word-splitting of the -f chain
    docker compose $compose_files up -d postgres redis minio opensearch backend

    # Phase 2: poll /health directly. 15 minutes covers any realistic cold-boot
    # migration path.
    echo -e "${BLUE}⏳ Waiting for backend to become healthy (up to 15 minutes)...${NC}"
    waited=0
    max_wait=900
    while [ $waited -lt $max_wait ]; do
        if curl -sf --connect-timeout 2 --max-time 4 \
            "http://localhost:${backend_port}/health" >/dev/null 2>&1; then
            echo -e "${GREEN}✓ Backend healthy after ${waited}s${NC}"
            break
        fi
        # Hard-fail detection: don't burn the full 15 minutes on a dead container.
        state=$(docker inspect opentranscribe-backend \
            --format '{{.State.Status}}:{{.State.ExitCode}}' 2>/dev/null || echo "unknown:?")
        case "$state" in
            exited:0|restarting:*)
                # Clean exit or restart-policy cycling; keep polling.
                ;;
            exited:*)
                echo -e "${RED}❌ Backend exited with non-zero code: $state${NC}"
                echo -e "${YELLOW}Last 40 lines of backend logs:${NC}"
                docker logs --tail 40 opentranscribe-backend 2>&1 || true
                return 1
                ;;
        esac
        sleep 5
        waited=$((waited + 5))
    done

    if [ $waited -ge $max_wait ]; then
        echo -e "${RED}❌ Backend failed to become healthy within ${max_wait}s${NC}"
        echo -e "${YELLOW}Last 40 lines of backend logs:${NC}"
        docker logs --tail 40 opentranscribe-backend 2>&1 || true
        return 1
    fi

    # Phase 3: backend is healthy — safe to start everything else.
    echo -e "${BLUE}▶ Starting remaining services (phase 2/2)...${NC}"
    # shellcheck disable=SC2086  # intentional word-splitting of the -f chain
    docker compose $compose_files up -d

    # Report what actually came up, so an upgrade that silently ran the wrong
    # image is visible at the point of upgrade rather than weeks later.
    local running_version
    running_version=$(curl -fsS --connect-timeout 3 \
        "http://localhost:${backend_port}/api/version" 2>/dev/null \
        | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4 || echo "")
    if [ -n "$running_version" ]; then
        echo -e "${GREEN}✓ Running version: ${running_version}${NC}"
    fi

    return 0
}

show_access_info() {
    # Source .env to get port values
    source .env 2>/dev/null || true

    # Check if NGINX/HTTPS is configured
    local nginx_server_name=""
    nginx_server_name=$(read_env_value NGINX_SERVER_NAME)

    local cert_file="${NGINX_CERT_FILE:-./nginx/ssl/server.crt}"
    local key_file="${NGINX_CERT_KEY:-./nginx/ssl/server.key}"
    local https_enabled=false

    if [ -n "$nginx_server_name" ] && [ -f "$cert_file" ] && [ -f "$key_file" ] && [ -f docker-compose.nginx.yml ]; then
        https_enabled=true
    fi

    echo -e "${GREEN}🌐 Access Information:${NC}"

    if [ "$https_enabled" = true ]; then
        echo "  🔒 HTTPS Mode (via NGINX reverse proxy)"
        echo "  • Web Interface:     https://$nginx_server_name"
        echo "  • Documentation:     https://$nginx_server_name/docs/"
        echo "  • API:               https://$nginx_server_name/api"
        echo "  • API Documentation: https://$nginx_server_name/api/docs"
        echo "  • Flower Dashboard:  https://$nginx_server_name/flower/"
        echo "  • MinIO Console:     https://$nginx_server_name/minio/"
        echo ""
        echo -e "${YELLOW}📝 Note: Add '$nginx_server_name' to your DNS or /etc/hosts${NC}"
        echo -e "${YELLOW}   Trust nginx/ssl/server.crt on client devices for no warnings${NC}"
    else
        echo "  • Web Interface:     http://localhost:${FRONTEND_PORT:-5173}"
        echo "  • Documentation:     http://localhost:${FRONTEND_PORT:-5173}/docs/"
        echo "  • API Documentation: http://localhost:${BACKEND_PORT:-5174}/docs"
        echo "  • API Endpoint:      http://localhost:${BACKEND_PORT:-5174}/api"
        echo "  • Flower Dashboard:  http://localhost:${FLOWER_PORT:-5175}/flower"
        echo "  • MinIO Console:     http://localhost:${MINIO_CONSOLE_PORT:-5179}"
        if [ -z "$nginx_server_name" ]; then
            echo ""
            echo -e "${YELLOW}💡 For HTTPS (required for mic recording from other devices):${NC}"
            echo -e "${YELLOW}   Run: ./opentranscribe.sh setup-ssl${NC}"
        fi
    fi
    echo ""
    echo -e "${YELLOW}⏳ Please wait a moment for all services to initialize...${NC}"
}

# The image whose diar-server binary should perform the export. Same reasoning as
# scripts/download-models.sh's resolve_downloader_image(): the export MUST come from
# the version this deployment actually runs, because the diar-native sidecar is that
# SAME image with its CMD replaced — an export made by a different backend build than
# the one the sidecar will run from is not something either of them is contracted to
# tolerate.
resolve_diar_native_downloader_image() {
    # A Blackwell pin (from .env, or written there by pin_diar_native_image_for_blackwell
    # via the `download_models_diar_native` call below) must win here too — otherwise
    # `start` runs the sidecar at `:blackwell` while this export runs at the plain
    # release tag, violating the "export and serve agree" contract documented above.
    local pinned
    pinned=$(read_env_value DIAR_NATIVE_IMAGE)
    if [ -n "$pinned" ]; then
        echo "$pinned"
        return 0
    fi
    local tag
    tag=$(read_env_value OT_IMAGE_TAG)
    echo "${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend:${tag:-latest}"
}

# `download-models diar-native`: produce the ONNX/PLDA export `diar-server` needs,
# without running the full model downloader. This is the command five files already
# advertised (.env.example, this script's own get_compose_files comment, two test
# files, and native_provision.py's docstring) before it existed anywhere — issue #654.
#
# Calls the Rust binary directly (`diar-server provision-models`) rather than routing
# through download-models.py: that script's `diar-native` DOWNLOAD_GROUPS entry exists
# for the FULL-downloader / offline-packaging callers that already invoke it in one
# container per run, and duplicating this one export inside that flow would mean two
# independent places decide the export's flags. One-shot container construction mirrors
# scripts/download-models.sh's docker run (lines ~354-371): HUGGINGFACE_TOKEN in the
# environment (never the command line — visible to any `ps` on the host), the
# HuggingFace cache mounted so a re-run does not re-fetch the underlying weights, and
# the pinned deployment image via resolve_diar_native_downloader_image() above.
download_models_diar_native() {
    check_environment
    # Same Blackwell-tag pin `start`/`restart`/`status` apply, before resolving the
    # downloader image below — without this call here, a Blackwell host's `start` used
    # `:blackwell` while `download-models diar-native` used the plain release tag (#4 in
    # the audit that found this).
    pin_diar_native_image_for_blackwell

    local token
    token=$(read_env_value HUGGINGFACE_TOKEN)
    if [ -z "$token" ]; then
        echo -e "${RED}❌ HUGGINGFACE_TOKEN not set in .env.${NC}"
        echo ""
        echo "   1. Create a token (Read access): https://huggingface.co/settings/tokens"
        echo "   2. Signed in as that account, accept the terms at:"
        echo "      https://huggingface.co/pyannote/speaker-diarization-community-1"
        echo "   3. Add it to .env:  HUGGINGFACE_TOKEN=your_token_here"
        echo ""
        echo "   Then re-run: ./opentranscribe.sh download-models diar-native"
        exit 1
    fi

    local models_dir
    models_dir=$(resolve_diar_native_models_dir)
    local cache_dir
    cache_dir=$(read_env_value MODEL_CACHE_DIR)
    cache_dir="${cache_dir:-./models}"

    # Created (empty, if this is the first run) and chowned to the container's user
    # BEFORE the container writes into either mount — a bare `mkdir -p` from whatever
    # user invoked this script would leave both root-owned, and appuser cannot write
    # a root-owned bind-mount source. Safe to create empty here specifically: this
    # function always has a token in hand by this point, and it is about to populate
    # the directory in the same breath — not a code path that could leave an empty
    # diar-native dir sitting for `start` to auto-load against and crash-loop.
    mkdir -p "$models_dir" "$cache_dir/huggingface"
    # `|| true`: this script runs under `set -e`, and fix_model_cache_permissions
    # returns 1 (after printing its own warning) when it could not fix ownership —
    # e.g. no docker and no chown permission. A bare call here aborted the WHOLE
    # command right there, before `docker run provision-models` itself ever ran, which
    # is exactly the scenario that produces the documented exit-7 NOT_WRITABLE remedy
    # below. Let the fix attempt fail non-fatally and continue: either the directory was
    # writable anyway (fix reported false-negative, e.g. `stat` unavailable), or it
    # genuinely is not and `docker run` below fails with exit 7, whose remedy is what an
    # operator actually needs to see.
    fix_model_cache_permissions || true

    local image
    image=$(resolve_diar_native_downloader_image)
    echo -e "${BLUE}🎙️  Provisioning diar-native models via ${image}${NC}"
    echo -e "${BLUE}   (diar-server provision-models — several hundred MB, a couple of minutes)${NC}"
    echo ""

    # Captured via `|| rc=$?`, not `if docker run ...; then ... fi; rc=$?` — under this
    # script's `set -e`, a bare `if CMD; then ...; fi` with no `else` leaves $? reset to
    # the `if` statement's own (zero) status once control falls past the `fi`, so a later
    # `rc=$?` always read 0 regardless of what docker run actually exited with. That made
    # every case arm below (5 token-denied, 7 not-writable, 6 no-exporter, etc.) dead code.
    local rc=0
    docker run --rm \
        -e HUGGINGFACE_TOKEN="$token" \
        -v "$(realpath "$models_dir"):/models" \
        -v "$(realpath "$cache_dir/huggingface"):/home/appuser/.cache/huggingface" \
        "$image" \
        diar-server provision-models \
            --models-dir /models \
            --set fast \
            --mode cpu \
            --smoke-clip /usr/local/share/diar-native/smoke.wav \
            --json || rc=$?

    if [ "$rc" -eq 0 ]; then
        echo ""
        echo -e "${GREEN}✅ diar-native models provisioned at ${models_dir}${NC}"
        echo "   Run './opentranscribe.sh restart' (or 'start') to pick up the native sidecar."
        return 0
    fi

    # Stable exit codes from crates/diar-core/src/provision/mod.rs::exit — branched on
    # rather than parsed out of diar-server's own message text, which it also prints
    # above this. 0/2/3/4/5/6/7/9 are the whole contract; anything else is unexpected.
    echo ""
    echo -e "${RED}❌ diar-native provisioning failed (exit ${rc}).${NC}"
    case "$rc" in
        5)
            echo -e "${YELLOW}   Token denied. The gate is per-account and auto-approved — a valid${NC}"
            echo -e "${YELLOW}   token whose account never accepted the terms fails identically.${NC}"
            echo -e "${YELLOW}   Confirm at: https://huggingface.co/pyannote/speaker-diarization-community-1${NC}"
            ;;
        7)
            echo -e "${YELLOW}   ${models_dir} is not writable by the container. Run:${NC}"
            echo -e "${YELLOW}     ./scripts/fix-model-permissions.sh${NC}"
            ;;
        6)
            echo -e "${YELLOW}   This image has no Python exporter environment. That should not happen${NC}"
            echo -e "${YELLOW}   on a published backend image — please report it.${NC}"
            ;;
        3)
            echo -e "${YELLOW}   The export completed but failed its own smoke test — see the output above.${NC}"
            ;;
        4)
            echo -e "${YELLOW}   The export itself failed — see the output above.${NC}"
            ;;
        9)
            echo -e "${YELLOW}   Device unavailable. This step runs on CPU and needs no GPU; please report this.${NC}"
            ;;
        *)
            echo -e "${YELLOW}   See diar-server's own output above for detail.${NC}"
            ;;
    esac
    exit 1
}

case "${1:-help}" in
    start)
        check_environment
        # `|| true`: same set -e / exit-7-remedy hazard as download_models_diar_native's
        # call above — a failed fix here must not abort this command before the actual
        # `docker compose` step runs and reports its own, more specific error.
        fix_model_cache_permissions || true
        # Same first-run fix as opentr.sh's start_app()/reset_and_init() (issue #614):
        # a genuinely fresh `cp .env.example .env` ships MINIO_KMS_SECRET_KEY as an
        # unusable placeholder, which crash-loops MinIO on its very first boot. #613
        # promoted this script to the real production entry point, so it needs the
        # same first-run protection -- guarded (not require_db_helpers' hard failure)
        # because an install whose scripts/common.sh predates this fix should still be
        # able to `start`; it just doesn't get the auto-fix and hits the pre-existing
        # MinIO KMS error, exactly as it did before common.sh was ever sourced here.
        if declare -F ensure_minio_kms_secret >/dev/null; then
            ensure_minio_kms_secret ".env"
        fi
        echo -e "${YELLOW}🚀 Starting OpenTranscribe...${NC}"
        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        compose_files=$(get_compose_files)
        docker compose $compose_files up -d
        echo -e "${GREEN}✅ OpenTranscribe started!${NC}"
        show_access_info
        ;;
    stop)
        check_environment
        echo -e "${YELLOW}🛑 Stopping OpenTranscribe...${NC}"
        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        compose_files=$(get_compose_files)
        docker compose $compose_files down
        echo -e "${GREEN}✅ OpenTranscribe stopped${NC}"
        ;;
    restart)
        check_environment
        # `|| true`: same set -e / exit-7-remedy hazard as download_models_diar_native's
        # call above — a failed fix here must not abort this command before the actual
        # `docker compose` step runs and reports its own, more specific error.
        fix_model_cache_permissions || true
        echo -e "${YELLOW}🔄 Restarting OpenTranscribe...${NC}"
        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        compose_files=$(get_compose_files)
        docker compose $compose_files down
        docker compose $compose_files up -d
        echo -e "${GREEN}✅ OpenTranscribe restarted!${NC}"
        show_access_info
        ;;
    status)
        check_environment
        echo -e "${BLUE}📊 Container Status:${NC}"
        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        compose_files=$(get_compose_files)
        docker compose $compose_files ps
        print_diar_native_status "$compose_files"
        ;;
    compose-files)
        # Print the resolved `-f` chain on stdout, and nothing else.
        #
        # get_compose_files() is the SINGLE owner of overlay selection — GPU vs
        # Blackwell vs CPU-only, nginx, scheduled backup — but every other arm
        # consumed it internally, so there was no way to ASK what it chose. Two
        # consequences this arm exists to remove:
        #
        #   * A support request ("why is my GPU not being used?") had to be
        #     answered by inference. Now: `./opentranscribe.sh compose-files`.
        #   * The release rehearsal hand-built its own parallel `-f` list rather
        #     than driving this one, so the whole selection layer was never
        #     exercised by a release gate and had already drifted. See
        #     scripts/release-tests/REHEARSAL_ALIGNMENT_PLAN.md.
        #
        # The selection banners get_compose_files() prints go to stderr, so stdout
        # is exactly the chain and stays composable:
        #   docker compose $(./opentranscribe.sh compose-files 2>/dev/null) ps
        check_environment
        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        get_compose_files
        ;;
    download-models)
        check_environment
        model_group="${2:-}"
        case "$model_group" in
            ""|all)
                if [ ! -f scripts/download-models.sh ]; then
                    echo -e "${RED}❌ scripts/download-models.sh not found.${NC}"
                    echo -e "${YELLOW}   Fix with: ./opentranscribe.sh update-full${NC}"
                    exit 1
                fi
                dl_cache_dir=$(read_env_value MODEL_CACHE_DIR)
                bash scripts/download-models.sh "${dl_cache_dir:-./models}"
                ;;
            diar-native)
                download_models_diar_native
                ;;
            *)
                echo -e "${RED}❌ Unknown model group: '${model_group}'${NC}"
                echo "   Known groups: diar-native"
                echo "   Omit the group to download the full model set."
                exit 1
                ;;
        esac
        ;;
    logs)
        check_environment
        service=${2:-}
        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        compose_files=$(get_compose_files)

        if [ -z "$service" ]; then
            echo -e "${BLUE}📋 Showing all logs (Ctrl+C to exit):${NC}"
            docker compose $compose_files logs -f
        else
            echo -e "${BLUE}📋 Showing logs for $service (Ctrl+C to exit):${NC}"
            docker compose $compose_files logs -f "$service"
        fi
        ;;
    update)
        check_environment
        # `|| true`: same set -e / exit-7-remedy hazard as download_models_diar_native's
        # call above — a failed fix here must not abort this command before the actual
        # `docker compose` step runs and reports its own, more specific error.
        fix_model_cache_permissions || true

        # Optional target: `update --version vX.Y.Z` moves this install to a
        # specific release; `--rollback` returns it to the previous one. Both
        # work by rewriting OT_IMAGE_TAG in .env, which the compose files read as
        # ${OT_IMAGE_TAG:-latest}. With no argument, behaviour is unchanged.
        target_version=""
        do_rollback=false
        force_downgrade=false
        shift || true
        while [ $# -gt 0 ]; do
            case "$1" in
                --version) target_version="${2:-}"; shift 2 ;;
                --rollback) do_rollback=true; shift ;;
                --force-downgrade) force_downgrade=true; shift ;;
                *) echo -e "${YELLOW}⚠️  Unknown argument: $1 (ignored)${NC}"; shift ;;
            esac
        done

        current_tag=$(read_env_value OT_IMAGE_TAG)
        current_tag="${current_tag:-latest}"

        if [ "$do_rollback" = true ]; then
            # Deliberately NOT read_env_value: that helper anchors on `^KEY=`, and this
            # key is written commented-out (`^# *OT_PREVIOUS_IMAGE_TAG=`) by design (it's a
            # rollback marker, not an active setting) -- not worth a second helper parameter
            # for this one caller.
            target_version=$(grep -E '^# *OT_PREVIOUS_IMAGE_TAG=' .env 2>/dev/null | cut -d= -f2 | tr -d ' "' | head -1)
            if [ -z "$target_version" ]; then
                echo -e "${RED}❌ No previous version recorded — nothing to roll back to.${NC}"
                echo -e "${YELLOW}   A rollback target is only recorded by a prior 'update --version'.${NC}"
                echo -e "${YELLOW}   Use: ./opentranscribe.sh update --version vX.Y.Z${NC}"
                exit 1
            fi
            echo -e "${YELLOW}⏪ Rolling back: $current_tag → $target_version${NC}"
            echo -e "${YELLOW}⚠️  The migration chain is ONE-WAY. Rolling the images back does NOT${NC}"
            echo -e "${YELLOW}   revert the database. Restore a backup taken before the upgrade if${NC}"
            echo -e "${YELLOW}   the newer schema is not readable by $target_version.${NC}"
        fi

        if [ -n "$target_version" ]; then
            case "$target_version" in v*) ;; *) target_version="v${target_version}" ;; esac

            # Refuse a downgrade unless asked twice. Newer releases add columns
            # and tables the older image does not know about; the chain does not
            # go backwards.
            if [ "$do_rollback" = false ] && [ "$force_downgrade" = false ] && [ "$current_tag" != "latest" ]; then
                older=$(printf '%s\n%s\n' "${current_tag#v}" "${target_version#v}" | sort -V | head -1)
                if [ "$older" = "${target_version#v}" ] && [ "$target_version" != "$current_tag" ]; then
                    echo -e "${RED}❌ Refusing to downgrade $current_tag → $target_version${NC}"
                    echo -e "${YELLOW}   The migration chain is one-way. Use --rollback (which warns${NC}"
                    echo -e "${YELLOW}   about the database) or --force-downgrade if you are certain.${NC}"
                    exit 1
                fi
            fi

            # Record where we came from so --rollback has a target.
            if grep -q '^# *OT_PREVIOUS_IMAGE_TAG=' .env; then
                sed -i.bak "s|^# *OT_PREVIOUS_IMAGE_TAG=.*|# OT_PREVIOUS_IMAGE_TAG=${current_tag}|" .env
            else
                echo "# OT_PREVIOUS_IMAGE_TAG=${current_tag}" >> .env
            fi
            if grep -q '^OT_IMAGE_TAG=' .env; then
                sed -i.bak "s|^OT_IMAGE_TAG=.*|OT_IMAGE_TAG=${target_version}|" .env
            else
                echo "OT_IMAGE_TAG=${target_version}" >> .env
            fi
            rm -f .env.bak
            echo -e "${GREEN}✓ Pinned OT_IMAGE_TAG=${target_version} (was ${current_tag})${NC}"
            echo -e "${YELLOW}📥 Updating to ${target_version}...${NC}"

            # --rollback preflight (issue #610's companion, the mirror-image gap): an
            # OLDER image booting against a NEWER schema it cannot read fails with a
            # cryptic "Can't locate revision identified by '<head>'" -> SystemExit(1).
            # Alembic's migration chain has no forward-compat story, so check BEFORE
            # tearing anything down, not after. Read the live head now, while postgres
            # is still up — compose_down_for_upgrade (below) takes the WHOLE stack
            # down, postgres included, so this is the last point a plain
            # `docker compose exec postgres` can reach it.
            if [ "$do_rollback" = true ] && [ "$force_downgrade" = false ]; then
                pin_diar_native_image_for_blackwell
                pin_gpu_split_profile
                rollback_compose_files=$(get_compose_files)
                # shellcheck disable=SC2086  # intentional word-splitting of the -f chain
                rollback_live_head=$(docker compose $rollback_compose_files exec -T postgres psql -tA \
                    -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-opentranscribe}" \
                    -c 'SELECT version_num FROM alembic_version;' 2>/dev/null | tr -d '[:space:]')

                if [ -z "$rollback_live_head" ]; then
                    echo -e "${YELLOW}⚠️  Could not read the live database's alembic head — skipping the${NC}"
                    echo -e "${YELLOW}   rollback-compatibility check (postgres may not be running).${NC}"
                # Ask the TARGET image whether it knows that revision — no Python imports,
                # no env, no DB: every published backend image ships its own
                # alembic/versions/ tree (backend/Dockerfile.prod:229, COPY --chown into /app).
                elif ! docker run --rm --entrypoint sh "davidamacey/opentranscribe-backend:${target_version}" -c \
                        "grep -lq 'revision = \"${rollback_live_head}\"' /app/alembic/versions/*.py" \
                        >/dev/null 2>&1; then
                    echo -e "${RED}❌ Refusing to roll back to ${target_version}: the database is at${NC}"
                    echo -e "${RED}   ${rollback_live_head}, which ${target_version} does not know.${NC}"
                    echo -e "${YELLOW}   Restore a pre-upgrade backup first — it leaves services stopped for${NC}"
                    echo -e "${YELLOW}   you to choose the image (issue #610):${NC}"
                    echo -e "${YELLOW}     ./opentranscribe.sh restore <backup>${NC}"
                    echo -e "${YELLOW}   then re-run this command. Override with --force-downgrade if certain.${NC}"
                    exit 1
                fi
            fi
        else
            echo -e "${YELLOW}📥 Updating to the newest images for tag '${current_tag}'...${NC}"
        fi

        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        compose_files=$(get_compose_files)

        preflight_upgrade_env || exit 1
        compose_down_for_upgrade "$compose_files" || exit 1
        docker compose $compose_files pull

        if ! perform_phased_restart "$compose_files"; then
            echo -e "${RED}❌ Upgrade did not complete successfully.${NC}"
            echo -e "${YELLOW}   See https://docs.opentranscribe.app/docs/operations/upgrading#common-upgrade-issues${NC}"
            echo -e "${YELLOW}   for recovery steps, or run './opentranscribe.sh logs backend' for details.${NC}"
            exit 1
        fi

        echo -e "${GREEN}✅ OpenTranscribe containers updated!${NC}"
        echo ""
        echo -e "${YELLOW}💡 Tip: Run './opentranscribe.sh update-full' to also update scripts and config files${NC}"
        show_access_info
        ;;
    update-full)
        check_environment
        echo -e "${YELLOW}📥 Full update: Updating configuration files and Docker images...${NC}"
        echo ""

        BRANCH=$(resolve_config_ref) || exit 1

        # URL-encode the branch name (replace / with %2F for feature branches)
        ENCODED_BRANCH=$(echo "$BRANCH" | sed 's|/|%2F|g')
        GITHUB_RAW="https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/${ENCODED_BRANCH}"

        # Backup current opentranscribe.sh
        cp opentranscribe.sh opentranscribe.sh.bak 2>/dev/null || true

        echo -e "${BLUE}📄 Updating configuration files...${NC}"

        # Back up the base compose file too — it is now updated, so a bad release
        # should be recoverable without re-running the installer.
        cp docker-compose.yml docker-compose.yml.bak 2>/dev/null || true

        # The artifact list comes from release-manifest.txt, not from a list
        # hardcoded here. See that file's header for the two silent-breakage bugs
        # the old duplicated lists caused (missing docker-compose.yml on upgrade,
        # missing blackwell overlay on fresh install).
        # Releases published before release-manifest.txt existed have no list to read, and
        # following the pin (above) means we now ask them for one. Borrow the list from the
        # default branch in that case — the ARTIFACTS still come from $GITHUB_RAW, i.e. the
        # pinned release, so this cannot un-pin the install. The manifest's `optional` flag
        # absorbs entries the older release does not have. Same fallback as
        # setup-opentranscribe.sh's download_release_manifest_artifacts(); see issue #683.
        echo "  Downloading release-manifest.txt..."
        if ! curl -fsSL "$GITHUB_RAW/release-manifest.txt" -o release-manifest.txt.new; then
            rm -f release-manifest.txt.new
            MANIFEST_FALLBACK_BRANCH=$(resolve_default_branch)

            if [ -n "$MANIFEST_FALLBACK_BRANCH" ] && [ "$MANIFEST_FALLBACK_BRANCH" != "$BRANCH" ] &&
                curl -fsSL "https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/${MANIFEST_FALLBACK_BRANCH}/release-manifest.txt" \
                    -o release-manifest.txt.new; then
                echo -e "  ${YELLOW}⚠️${NC}  $BRANCH predates release-manifest.txt — using the file list from '${MANIFEST_FALLBACK_BRANCH}'."
                echo -e "     Config files are still downloaded from $BRANCH."
            else
                rm -f release-manifest.txt.new
                echo -e "  ${RED}✗${NC} could not fetch release-manifest.txt from $BRANCH"
                echo -e "  ${YELLOW}Refusing to update config files from an unknown artifact list.${NC}"
                echo -e "  ${YELLOW}Use './opentranscribe.sh update' to update images only.${NC}"
                exit 1
            fi
        fi
        mv release-manifest.txt.new release-manifest.txt

        update_failed=0
        while IFS= read -r manifest_line || [ -n "$manifest_line" ]; do
            # Strip comments and blanks.
            case "$manifest_line" in ''|'#'*) continue ;; esac

            artifact_path=$(printf '%s' "$manifest_line" | cut -f1 | tr -d '[:space:]')
            artifact_flags=$(printf '%s' "$manifest_line" | cut -s -f2)
            [ -n "$artifact_path" ] || continue

            case ",$artifact_flags," in *,preserve,*) continue ;; esac

            artifact_dir=$(dirname "$artifact_path")
            [ "$artifact_dir" = "." ] || mkdir -p "$artifact_dir"

            # Download to .new first so a failed fetch never truncates a working file.
            if curl -fsSL "$GITHUB_RAW/$artifact_path" -o "${artifact_path}.new"; then
                mv "${artifact_path}.new" "$artifact_path"
                case ",$artifact_flags," in *,exec,*) chmod +x "$artifact_path" ;; esac
                echo -e "  ${GREEN}✓${NC} $artifact_path"
            else
                rm -f "${artifact_path}.new"
                case ",$artifact_flags," in
                    *,optional,*)
                        echo -e "  ${YELLOW}⚠️${NC} $artifact_path (optional, not in this release)"
                        ;;
                    *)
                        echo -e "  ${RED}✗${NC} $artifact_path (REQUIRED — download failed)"
                        update_failed=1
                        ;;
                esac
            fi
        done < release-manifest.txt

        if [ "$update_failed" -ne 0 ]; then
            echo ""
            echo -e "${RED}❌ One or more required files failed to download.${NC}"
            echo -e "${YELLOW}Not restarting: a partial config set is worse than the old one.${NC}"
            echo -e "${YELLOW}Your previous docker-compose.yml is at docker-compose.yml.bak${NC}"
            exit 1
        fi

        # Report new .env keys rather than touching the user's .env. A release that
        # adds a required setting is otherwise invisible: Settings uses
        # extra="ignore", so a missing var is silently defaulted, not an error.
        if [ -f .env ] && [ -f .env.example ]; then
            new_keys=$(grep -oE '^[A-Z_][A-Z0-9_]*=' .env.example 2>/dev/null | tr -d '=' | sort -u \
                | while read -r key; do
                    grep -qE "^${key}=" .env || echo "  • $key"
                done)
            if [ -n "$new_keys" ]; then
                echo ""
                echo -e "${YELLOW}📋 New settings in this release (your .env was NOT modified):${NC}"
                echo "$new_keys"
                echo -e "${YELLOW}   Defaults apply unless you add them to .env — see .env.example.${NC}"
            fi
        fi

        # issue #709: the report above only sees a key that is ABSENT. Also flag a key
        # that is PRESENT but whose VALUE has rotted (see check_stale_env_values()'s
        # docstring above check_environment() for the two seeded cases). Same
        # warn-only, never-rewrite contract; run before teardown below.
        if [ -f .env ]; then
            stale_findings=$(check_stale_env_values .env)
            if [ -n "$stale_findings" ]; then
                echo ""
                echo -e "${YELLOW}⚠️  Settings in .env whose VALUE has gone stale (your .env was NOT modified):${NC}"
                echo "$stale_findings"
            fi
        fi

        echo ""
        echo -e "${BLUE}🐳 Updating Docker images...${NC}"
        # `|| true`: same set -e / exit-7-remedy hazard as download_models_diar_native's
        # call above — a failed fix here must not abort this command before the actual
        # `docker compose` step runs and reports its own, more specific error.
        fix_model_cache_permissions || true
        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        compose_files=$(get_compose_files)
        # Same gate as `update`: refuse while the old stack is still running
        # rather than after it is torn down (#410).
        preflight_upgrade_env || exit 1
        compose_down_for_upgrade "$compose_files" || exit 1
        docker compose $compose_files pull

        # Same phased startup `update` uses. This path previously did a bare
        # `up -d`, which lets compose's dependency resolver give up on the
        # backend's health wait and SIGTERM it mid-Alembic — and update-full is
        # the MORE likely of the two to run a long migration chain, since it is
        # what people run when moving across releases.
        if ! perform_phased_restart "$compose_files"; then
            echo -e "${RED}❌ Upgrade did not complete successfully.${NC}"
            echo -e "${YELLOW}   See https://docs.opentranscribe.app/docs/operations/upgrading#common-upgrade-issues${NC}"
            echo -e "${YELLOW}   for recovery steps, or run './opentranscribe.sh logs backend' for details.${NC}"
            exit 1
        fi

        # A release can introduce a NEW model (v0.5.0 adds the chat reranker and
        # the content-redaction weights). Nothing in the upgrade path fetches it:
        #
        #   * Online deployments self-heal — the model lazy-downloads from
        #     HuggingFace on first use, so the only symptom is a long pause the
        #     first time someone touches that feature.
        #   * OFFLINE deployments do not. docker-compose.offline.yml sets
        #     HF_HUB_OFFLINE=1, so a newly-required model that is not already in
        #     the cache is a hard failure with no network to recover from.
        #
        # So say so, and make refreshing the cache one command rather than
        # something the user has to know about.
        if [ -f scripts/download-models.sh ]; then
            echo ""
            echo -e "${BLUE}🧠 Model cache${NC}"
            echo "  This release may require models your cache does not have yet."
            echo "  Online: they download on first use (one slow request)."
            echo "  Offline/air-gapped: pre-fetch them now, or the feature will fail:"
            echo "    bash scripts/download-models.sh \"\${MODEL_CACHE_DIR:-./models}\""
        fi

        echo ""
        echo -e "${GREEN}✅ Full update complete!${NC}"
        echo ""
        echo -e "${YELLOW}📝 Notes:${NC}"
        echo "  • Your .env configuration was preserved"
        echo "  • SSL certificates were preserved (if configured)"
        echo "  • Database and transcriptions were preserved"
        echo "  • Old script backed up to opentranscribe.sh.bak"
        echo "  • Old base compose backed up to docker-compose.yml.bak"
        echo ""
        show_access_info
        ;;
    clean)
        check_environment
        echo -e "${RED}⚠️  WARNING: This will remove ALL data including transcriptions!${NC}"
        read -p "Are you sure you want to continue? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo -e "${YELLOW}🗑️  Removing all data...${NC}"
            pin_diar_native_image_for_blackwell
            pin_gpu_split_profile
            compose_files=$(get_compose_files)
            docker compose $compose_files down -v
            echo -e "${GREEN}✅ All data removed${NC}"
        else
            echo -e "${GREEN}✅ Operation cancelled${NC}"
        fi
        ;;
    shell)
        check_environment
        service=${2:-backend}
        echo -e "${BLUE}🔧 Opening shell in $service container...${NC}"
        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        compose_files=$(get_compose_files)
        docker compose $compose_files exec "$service" /bin/bash || docker compose $compose_files exec "$service" /bin/sh
        ;;
    backup|restore)
        check_environment
        require_db_helpers
        # No pin_diar_native_image_for_blackwell here, deliberately: this arm never starts
        # or pulls the diar-native sidecar — backup_database/restore_database only `docker
        # compose exec` into containers that are already running — so DIAR_NATIVE_IMAGE is
        # not read by anything this arm does.
        compose_files=$(get_compose_files)

        # opentr.sh gets these from its prologue `set -a; source ./.env`; this script
        # deliberately has no such prologue (it greps individual keys — see
        # preflight_upgrade_env). Without these explicit reads a restore would DROP/CREATE
        # the DEFAULT database name on any install that customised them (issue #613).
        POSTGRES_USER=$(read_env_value POSTGRES_USER)
        POSTGRES_DB=$(read_env_value POSTGRES_DB)
        BACKUP_HOST_PATH=$(read_env_value BACKUP_HOST_PATH)
        export POSTGRES_USER POSTGRES_DB BACKUP_HOST_PATH

        cmd="$1"; shift
        # The shared functions are written for opentr.sh's execution semantics, which
        # deliberately omit `set -e` (many `|| true` paths). They contain several statements
        # of the form `[ -n "$x" ] && rm -f "$x"`, which return 1 when $x is empty and would
        # abort THIS script mid-restore under the `set -e` at the top of this file. Rather
        # than retrofit `set -e` correctness across ~350 lines of a DROP DATABASE path —
        # exactly where a subtle retrofit bug is unrecoverable — match the semantics the
        # code was written for, for the duration of this one call.
        set +e
        if [ "$cmd" = "backup" ]; then
            backup_database "$compose_files" "./opentranscribe.sh" "$@"
        else
            restore_database "$compose_files" "./opentranscribe.sh" "$@"
        fi
        rc=$?
        set -e
        exit $rc
        ;;
    config)
        check_environment
        echo -e "${BLUE}⚙️  Current Configuration:${NC}"
        echo "Environment file (.env):"
        grep -E "^[A-Z]" .env | head -20
        echo ""
        echo "Docker Compose configuration:"
        if docker compose config > /dev/null 2>&1; then
            echo "  ✅ Valid"
        else
            echo "  ❌ Invalid"
        fi
        ;;
    health)
        check_environment
        echo -e "${BLUE}🩺 Health Check:${NC}"

        # Check container status
        echo "Container Status:"
        pin_diar_native_image_for_blackwell
        pin_gpu_split_profile
        compose_files=$(get_compose_files)
        docker compose $compose_files ps --format "table {{.Service}}\t{{.Status}}\t{{.Ports}}"

        echo ""
        echo "Service Health:"

        # Source .env to get port values
        source .env 2>/dev/null || true

        # Backend health
        if curl -s http://localhost:${BACKEND_PORT:-5174}/health > /dev/null 2>&1; then
            echo "  ✅ Backend: Healthy"
        else
            echo "  ❌ Backend: Unhealthy"
        fi

        # Frontend health
        if curl -s http://localhost:${FRONTEND_PORT:-5173} > /dev/null 2>&1; then
            echo "  ✅ Frontend: Healthy"
        else
            echo "  ❌ Frontend: Unhealthy"
        fi

        # Database health
        if docker compose $compose_files exec -T postgres pg_isready -U postgres > /dev/null 2>&1; then
            echo "  ✅ Database: Healthy"
        else
            echo "  ❌ Database: Unhealthy"
        fi

        # NGINX health (only if configured)
        nginx_server_name=""
        nginx_server_name=$(read_env_value NGINX_SERVER_NAME)

        if [ -n "$nginx_server_name" ]; then
            if curl -s -k https://localhost:${NGINX_HTTPS_PORT:-443}/health > /dev/null 2>&1 || \
               curl -s http://localhost:${NGINX_HTTP_PORT:-80}/health > /dev/null 2>&1; then
                echo "  ✅ NGINX: Healthy (https://$nginx_server_name)"
            else
                # Check if container is running but not responding
                if docker compose $compose_files ps nginx 2>/dev/null | grep -q "Up"; then
                    echo "  ⚠️  NGINX: Running but not responding"
                else
                    echo "  ❌ NGINX: Not running"
                fi
            fi
        fi
        ;;
    setup-ssl)
        check_environment
        echo -e "${BLUE}🔒 HTTPS/SSL Setup${NC}"
        echo ""

        # Check if generate-ssl-cert.sh exists
        if [ ! -f scripts/generate-ssl-cert.sh ]; then
            echo -e "${RED}❌ SSL certificate generation script not found${NC}"
            echo "   Expected: scripts/generate-ssl-cert.sh"
            echo ""
            echo "   Download it from:"
            echo "   curl -fsSL $(raw_url_for scripts/generate-ssl-cert.sh) -o scripts/generate-ssl-cert.sh"
            echo "   chmod +x scripts/generate-ssl-cert.sh"
            exit 1
        fi

        # Check if docker-compose.nginx.yml exists
        if [ ! -f docker-compose.nginx.yml ]; then
            echo -e "${RED}❌ NGINX docker-compose file not found${NC}"
            echo "   Expected: docker-compose.nginx.yml"
            echo ""
            echo "   Download it from:"
            echo "   curl -fsSL $(raw_url_for docker-compose.nginx.yml) -o docker-compose.nginx.yml"
            exit 1
        fi

        # Check if nginx/site.conf.template exists
        if [ ! -f nginx/site.conf.template ]; then
            echo -e "${YELLOW}⚠️  NGINX configuration template not found${NC}"
            echo "   Downloading nginx/site.conf.template..."
            mkdir -p nginx/ssl
            # A REAL fetch, not a printed remedy: this pulled the nginx template from
            # tip-of-development onto a pinned deployment, so an HTTPS install could get
            # a proxy config written for a backend it is not running.
            curl -fsSL "$(raw_url_for nginx/site.conf.template)" -o nginx/site.conf.template || {
                echo -e "${RED}❌ Failed to download nginx configuration${NC}"
                echo -e "${YELLOW}   Or run:  ./opentranscribe.sh update-full${NC}"
                exit 1
            }
        fi

        # Prompt for hostname
        echo "Enter a hostname for your OpenTranscribe installation:"
        echo "(e.g., opentranscribe.local, transcribe.home, your-hostname.lan)"
        echo ""

        # Get current NGINX_SERVER_NAME from .env if exists
        current_hostname=""
        current_hostname=$(read_env_value NGINX_SERVER_NAME)

        if [ -n "$current_hostname" ]; then
            read -p "Hostname [$current_hostname]: " user_hostname
            hostname="${user_hostname:-$current_hostname}"
        else
            read -p "Hostname [opentranscribe.local]: " user_hostname
            hostname="${user_hostname:-opentranscribe.local}"
        fi

        echo ""
        echo -e "${GREEN}✓ Using hostname: $hostname${NC}"
        echo ""

        # Check for existing certificates
        if [ -f "nginx/ssl/server.crt" ] && [ -f "nginx/ssl/server.key" ]; then
            echo -e "${YELLOW}⚠️  Existing SSL certificates detected!${NC}"
            echo "   nginx/ssl/server.crt and nginx/ssl/server.key already exist."
            echo ""
            read -p "Overwrite existing certificates? (y/N) " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                echo -e "${GREEN}✓ Keeping existing certificates${NC}"
                echo ""
                # Still update .env with the hostname if different
                if [ -f .env ]; then
                    if grep -q "^NGINX_SERVER_NAME=" .env || grep -q "^#.*NGINX_SERVER_NAME=" .env; then
                        sed -i.bak "s|^#*\s*NGINX_SERVER_NAME=.*|NGINX_SERVER_NAME=$hostname|g" .env
                        rm -f .env.bak
                    else
                        echo "" >> .env
                        echo "# HTTPS/SSL Configuration" >> .env
                        echo "NGINX_SERVER_NAME=$hostname" >> .env
                    fi
                    echo -e "${GREEN}✓ Updated .env with NGINX_SERVER_NAME=$hostname${NC}"
                fi
                echo ""
                echo "Run './opentranscribe.sh restart' to apply changes."
                exit 0
            fi
            echo ""
        fi

        # Generate SSL certificates
        echo -e "${BLUE}Generating SSL certificates...${NC}"
        if bash scripts/generate-ssl-cert.sh "$hostname" --auto-ip; then
            echo ""
            echo -e "${GREEN}✓ SSL certificates generated successfully!${NC}"
        else
            echo -e "${RED}❌ Failed to generate SSL certificates${NC}"
            exit 1
        fi

        # Update .env file with NGINX_SERVER_NAME
        if [ -f .env ]; then
            if grep -q "^NGINX_SERVER_NAME=" .env || grep -q "^#.*NGINX_SERVER_NAME=" .env; then
                # Update existing entry
                sed -i.bak "s|^#*\s*NGINX_SERVER_NAME=.*|NGINX_SERVER_NAME=$hostname|g" .env
                rm -f .env.bak
            else
                # Add new entry
                echo "" >> .env
                echo "# HTTPS/SSL Configuration" >> .env
                echo "NGINX_SERVER_NAME=$hostname" >> .env
            fi
            echo -e "${GREEN}✓ Updated .env with NGINX_SERVER_NAME=$hostname${NC}"
        fi

        echo ""
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${YELLOW}📋 HTTPS Setup Complete - Next Steps${NC}"
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        echo "1. Configure DNS (choose one):"
        echo "   • Router DNS: Add $hostname → your server IP"
        echo "   • /etc/hosts: Add 'YOUR_SERVER_IP  $hostname'"
        echo ""
        echo "2. Trust the certificate on each device:"
        echo "   • Copy nginx/ssl/server.crt to client devices"
        echo "   • Import into browser/system trust store"
        echo ""
        echo "3. Restart OpenTranscribe:"
        echo "   ./opentranscribe.sh restart"
        echo ""
        echo "4. Access at: https://$hostname"
        echo ""
        ;;
    version)
        echo -e "${BLUE}OpenTranscribe Version Information${NC}"
        echo ""

        # Ask the running backend what it is. /api/version is unauthenticated and
        # needs no DB, so it answers even on a degraded stack.
        #
        # This replaced `docker compose exec -T backend python -c "from
        # app.core.version import VERSION"`, which had never worked: the module
        # exports APP_VERSION, not VERSION, so the import always raised and the
        # command always fell through to "unknown". The version check was dead
        # code from the day it was written.
        local_version="unknown"
        local_version=$(curl -fsS --connect-timeout 3 \
            "http://localhost:${BACKEND_PORT:-5174}/api/version" 2>/dev/null \
            | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' \
            | head -1 | cut -d'"' -f4 || echo "")
        [ -n "$local_version" ] || local_version="unknown"

        # Fallback: ask the container directly (backend up, port not published).
        if [ "$local_version" = "unknown" ] && docker compose ps 2>/dev/null | grep -q "backend.*Up"; then
            local_version=$(docker compose exec -T backend printenv APP_VERSION 2>/dev/null | tr -d '\r' || echo "unknown")
            [ -n "$local_version" ] || local_version="unknown"
        fi

        # Last resort: the VERSION file the installer wrote next to the compose files.
        if [ "$local_version" = "unknown" ] && [ -f VERSION ]; then
            local_version=$(tr -d '[:space:]' < VERSION)
        fi

        echo "  Local version: ${local_version:-unknown}"

        # Check for latest version from GitHub
        echo ""
        echo -e "${BLUE}Checking for updates...${NC}"
        latest_version=$(curl -fsSL --connect-timeout 5 "https://api.github.com/repos/attevon-llc/OpenTranscribe/releases/latest" 2>/dev/null | grep '"tag_name"' | head -1 | sed -E 's/.*"v?([^"]+)".*/\1/' || echo "")

        if [ -n "$latest_version" ]; then
            echo "  Latest release: $latest_version"
            echo ""

            if [ "$local_version" != "unknown" ] && [ "$local_version" != "$latest_version" ]; then
                echo -e "${YELLOW}📦 Update available!${NC}"
                echo ""
                echo "  To update containers only:"
                echo "    ./opentranscribe.sh update"
                echo ""
                echo "  To update everything (recommended):"
                echo "    ./opentranscribe.sh update-full"
            elif [ "$local_version" = "$latest_version" ]; then
                echo -e "${GREEN}✅ You are running the latest version${NC}"
            else
                echo -e "${YELLOW}💡 Run './opentranscribe.sh update-full' to ensure you have the latest version${NC}"
            fi
        else
            echo -e "${YELLOW}⚠️  Could not check for updates (no internet or GitHub API limit)${NC}"
            echo ""
            echo "  To update manually:"
            echo "    ./opentranscribe.sh update-full"
        fi

        echo ""
        echo -e "${BLUE}Container Images:${NC}"
        docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.CreatedSince}}" 2>/dev/null | grep -E "opentranscribe|REPOSITORY" || echo "  No OpenTranscribe images found"
        echo ""
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo -e "${RED}❌ Unknown command: $1${NC}"
        show_help
        exit 1
        ;;
esac
