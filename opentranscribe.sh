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
        grep -E "^${key}=" "$env_file" 2>/dev/null \
            | head -1 \
            | cut -d= -f2- \
            | sed -E 's/[[:space:]]+#.*$//' \
            | tr -d ' "' \
            || true
    }
fi

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
        echo -e "     mkdir -p scripts && curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/scripts/common.sh -o scripts/common.sh && chmod +x scripts/common.sh"
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

    # Check if model cache directory exists
    if [ ! -d "$MODEL_CACHE_DIR" ]; then
        echo -e "${BLUE}📁 Creating model cache directory: $MODEL_CACHE_DIR${NC}"
        mkdir -p "$MODEL_CACHE_DIR/huggingface" "$MODEL_CACHE_DIR/torch"
    fi

    # Check current ownership
    local current_owner
    current_owner=$(stat -c '%u' "$MODEL_CACHE_DIR" 2>/dev/null || stat -f '%u' "$MODEL_CACHE_DIR" 2>/dev/null || echo "unknown")

    # If directory is owned by root (0) or doesn't match container user (1000), fix permissions
    if [ "$current_owner" = "0" ] || [ "$current_owner" != "1000" ]; then
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

    echo "$compose_files"
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

case "${1:-help}" in
    start)
        check_environment
        fix_model_cache_permissions
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
        compose_files=$(get_compose_files)
        docker compose $compose_files up -d
        echo -e "${GREEN}✅ OpenTranscribe started!${NC}"
        show_access_info
        ;;
    stop)
        check_environment
        echo -e "${YELLOW}🛑 Stopping OpenTranscribe...${NC}"
        compose_files=$(get_compose_files)
        docker compose $compose_files down
        echo -e "${GREEN}✅ OpenTranscribe stopped${NC}"
        ;;
    restart)
        check_environment
        fix_model_cache_permissions
        echo -e "${YELLOW}🔄 Restarting OpenTranscribe...${NC}"
        compose_files=$(get_compose_files)
        docker compose $compose_files down
        docker compose $compose_files up -d
        echo -e "${GREEN}✅ OpenTranscribe restarted!${NC}"
        show_access_info
        ;;
    status)
        check_environment
        echo -e "${BLUE}📊 Container Status:${NC}"
        compose_files=$(get_compose_files)
        docker compose $compose_files ps
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
        get_compose_files
        ;;
    logs)
        check_environment
        service=${2:-}
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
        fix_model_cache_permissions

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

        # GitHub raw URL base - supports OPENTRANSCRIBE_BRANCH env var for testing
        BRANCH="${OPENTRANSCRIBE_BRANCH:-master}"
        # URL-encode the branch name (replace / with %2F for feature branches)
        ENCODED_BRANCH=$(echo "$BRANCH" | sed 's|/|%2F|g')
        GITHUB_RAW="https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/${ENCODED_BRANCH}"

        if [ "$BRANCH" != "master" ]; then
            echo -e "${BLUE}ℹ️  Using branch: $BRANCH${NC}"
        fi

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
        echo "  Downloading release-manifest.txt..."
        if ! curl -fsSL "$GITHUB_RAW/release-manifest.txt" -o release-manifest.txt.new; then
            echo -e "  ${RED}✗${NC} could not fetch release-manifest.txt from $BRANCH"
            echo -e "  ${YELLOW}Refusing to update config files from an unknown artifact list.${NC}"
            echo -e "  ${YELLOW}Use './opentranscribe.sh update' to update images only.${NC}"
            exit 1
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

        echo ""
        echo -e "${BLUE}🐳 Updating Docker images...${NC}"
        fix_model_cache_permissions
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
        compose_files=$(get_compose_files)
        docker compose $compose_files exec "$service" /bin/bash || docker compose $compose_files exec "$service" /bin/sh
        ;;
    backup|restore)
        check_environment
        require_db_helpers
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
            echo "   curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/scripts/generate-ssl-cert.sh -o scripts/generate-ssl-cert.sh"
            echo "   chmod +x scripts/generate-ssl-cert.sh"
            exit 1
        fi

        # Check if docker-compose.nginx.yml exists
        if [ ! -f docker-compose.nginx.yml ]; then
            echo -e "${RED}❌ NGINX docker-compose file not found${NC}"
            echo "   Expected: docker-compose.nginx.yml"
            echo ""
            echo "   Download it from:"
            echo "   curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/docker-compose.nginx.yml -o docker-compose.nginx.yml"
            exit 1
        fi

        # Check if nginx/site.conf.template exists
        if [ ! -f nginx/site.conf.template ]; then
            echo -e "${YELLOW}⚠️  NGINX configuration template not found${NC}"
            echo "   Downloading nginx/site.conf.template..."
            mkdir -p nginx/ssl
            curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/nginx/site.conf.template -o nginx/site.conf.template || {
                echo -e "${RED}❌ Failed to download nginx configuration${NC}"
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
