#!/bin/bash

# OpenTranscribe Offline Management Script
# Wrapper around standard OpenTranscribe operations for offline deployments
# Usage: ./opentr-offline.sh [command] [options]

# Installation directory
INSTALL_DIR="/opt/opentranscribe"
# Compose files are built dynamically to ensure correct override order
# CRITICAL: offline.yml MUST be last to ensure offline settings override everything
BASE_COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"
OFFLINE_COMPOSE_FILE="$INSTALL_DIR/docker-compose.offline.yml"
GPU_SCALE_COMPOSE_FILE="$INSTALL_DIR/docker-compose.gpu-scale.yml"
DIAR_NATIVE_COMPOSE_FILE="$INSTALL_DIR/docker-compose.diar-native.yml"
DIAR_NATIVE_GPU_COMPOSE_FILE="$INSTALL_DIR/docker-compose.diar-native-gpu.yml"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

#######################
# HELPER FUNCTIONS
#######################

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

# Display help menu
show_help() {
    echo -e "${CYAN}🚀 OpenTranscribe Offline Management${NC}"
    echo "-------------------------------"
    echo "Usage: ./opentr.sh [command] [options]"
    echo ""
    echo "Basic Commands:"
    echo "  start [--gpu-scale]      - Start all services"
    echo "                             --gpu-scale: Enable multi-GPU worker scaling"
    echo "  stop                     - Stop all services"
    echo "  restart [--gpu-scale]    - Restart all services"
    echo "  status                   - Show service status"
    echo "  logs [service]           - View logs (all services by default)"
    echo ""
    echo "Service Management:"
    echo "  restart-backend          - Restart backend services only"
    echo "  restart-frontend         - Restart frontend only"
    echo "  shell [service]          - Open shell in a service container"
    echo ""
    echo "Maintenance:"
    echo "  health                   - Check health status of all services"
    echo "  clean                    - Clean up stopped containers and unused volumes"
    echo "  backup                   - Create database backup"
    echo "  help                     - Show this help menu"
    echo ""
    echo "Examples:"
    echo "  ./opentr.sh start"
    echo "  ./opentr.sh start --gpu-scale      # Enable multi-GPU scaling"
    echo "  ./opentr.sh logs backend"
    echo "  ./opentr.sh restart-backend"
    echo ""
}

# Check if running from correct directory
check_location() {
    if [ ! -f "$INSTALL_DIR/docker-compose.yml" ] || [ ! -f "$INSTALL_DIR/docker-compose.offline.yml" ]; then
        print_error "Docker Compose files not found in: $INSTALL_DIR"
        print_info "Required: docker-compose.yml and docker-compose.offline.yml"
        exit 1
    fi
}

# Check if .env file exists and has required values
check_env() {
    if [ ! -f "$INSTALL_DIR/.env" ]; then
        print_error "Configuration file not found: $INSTALL_DIR/.env"
        exit 1
    fi

    # Check for offline mode (HF_HUB_OFFLINE=1 means models are pre-installed, no token needed)
    # shellcheck source=/dev/null  # Runtime .env file, not available during static analysis
    source "$INSTALL_DIR/.env"

    # In offline mode, HuggingFace token is NOT required (models are pre-downloaded)
    if [ "${HF_HUB_OFFLINE}" != "1" ]; then
        # Not in offline mode - check if token is set
        if [ -z "$HUGGINGFACE_TOKEN" ]; then
            print_warning "HUGGINGFACE_TOKEN is not set in .env file"
            print_warning "Speaker diarization will not work without it"
            print_info "Get your token at: https://huggingface.co/settings/tokens"
        fi
    fi
}

# Compose command wrapper - COMPOSE_FILES must be set before calling
dc() {
    if [ -z "$COMPOSE_FILES" ]; then
        print_error "COMPOSE_FILES not set! This is a bug."
        exit 1
    fi
    docker compose $COMPOSE_FILES "$@"
}

# Build compose files list in correct order
# Order: base.yml -> [gpu-scale.yml] -> [diar-native.yml] -> offline.yml (offline MUST be last)
build_compose_files() {
    local use_gpu_scale="$1"

    COMPOSE_FILES="-f $BASE_COMPOSE_FILE"

    # Add GPU scaling BEFORE offline (if requested)
    if [ "$use_gpu_scale" = "true" ]; then
        if [ -f "$GPU_SCALE_COMPOSE_FILE" ]; then
            COMPOSE_FILES="$COMPOSE_FILES -f $GPU_SCALE_COMPOSE_FILE"
        else
            print_warning "docker-compose.gpu-scale.yml not found - GPU scaling not available"
        fi
    fi

    # Native diarization sidecar, conditional on its weights ALREADY being present —
    # unlike opentr.sh/opentranscribe.sh, this never checks HUGGINGFACE_TOKEN. An
    # offline install has no network route to HuggingFace at all, so the export can
    # only ever have arrived pre-populated in the package (see build-offline-package.sh's
    # download_models()); a token here could never provision anything and would be a
    # false promise that the sidecar is about to start working.
    #
    # Issue #655 fix item 5 / the feat/diar-native-e2e follow-up: this was previously
    # gated on weights-present ALONE, with no engine gate and no lite gate — so an
    # operator who deliberately set ENGINE_DIARIZER_BACKEND=pyannote (or ships a
    # DEPLOYMENT_MODE=lite offline package, which has no diar-native provisioning
    # toolchain to begin with) still got the sidecar loaded, defeating that
    # configuration outright. Mirrors opentr.sh's add_diar_native_overlay predicate
    # (lite excluded, engine must resolve to native) minus the token/auto-provision
    # half, which cannot apply here.
    #
    # DIAR_NATIVE_IMAGE / Blackwell: deliberately NOT wired here. opentr.sh pins
    # DIAR_NATIVE_IMAGE to match docker-compose.blackwell.yml's celery-worker tag
    # when it detects an SM_12x GPU, but this installer has no --blackwell overlay
    # of any kind (build-offline-package.sh packages a single fixed image set, and
    # nothing in this script ever loads docker-compose.blackwell.yml) — there is no
    # existing Blackwell code path here for the sidecar's tag to disagree with.
    # Adding one is a real offline-Blackwell-support gap, but a materially bigger
    # scope than this fix; recorded as excluded rather than silently absent.
    if [ -f "$DIAR_NATIVE_COMPOSE_FILE" ]; then
        # .env is not guaranteed sourced yet -- cmd_stop()/cmd_restart() never call
        # check_env(), and cmd_start() calls it AFTER build_compose_files(). Source it
        # here directly rather than depending on caller order.
        if [ -z "${MODEL_CACHE_DIR:-}${DIAR_NATIVE_MODELS_DIR:-}${ENGINE_DIARIZER_BACKEND:-}${DEPLOYMENT_MODE:-}" ] && [ -f "$INSTALL_DIR/.env" ]; then
            # shellcheck source=/dev/null  # Runtime .env file, not available during static analysis
            source "$INSTALL_DIR/.env"
        fi
        local diar_models_dir="${DIAR_NATIVE_MODELS_DIR:-${MODEL_CACHE_DIR:-$INSTALL_DIR/models}/diar-native}"
        local deployment_mode_lc
        deployment_mode_lc=$(printf '%s' "${DEPLOYMENT_MODE:-}" | tr '[:upper:]' '[:lower:]')
        if [ "$deployment_mode_lc" = "lite" ]; then
            print_info "Native diarization sidecar skipped (DEPLOYMENT_MODE=lite has no provisioning toolchain)"
        elif [ "${ENGINE_DIARIZER_BACKEND:-native}" != "native" ]; then
            print_info "Native diarization sidecar skipped (ENGINE_DIARIZER_BACKEND=${ENGINE_DIARIZER_BACKEND})"
        elif [ -d "$diar_models_dir" ] && [ -n "$(ls -A "$diar_models_dir" 2>/dev/null)" ]; then
            COMPOSE_FILES="$COMPOSE_FILES -f $DIAR_NATIVE_COMPOSE_FILE"
            print_info "Native diarization sidecar enabled (weights present at $diar_models_dir)"
            # The base overlay is CPU-safe by construction; the device reservation lives
            # in a second file so this one can load on a GPU-less air-gapped host (#660).
            # Gated on the same nvidia probe the GPU-scale overlay uses above, so the
            # sidecar never claims a device the rest of this install cannot see.
            if [ -f "$DIAR_NATIVE_GPU_COMPOSE_FILE" ] && command -v nvidia-smi >/dev/null 2>&1; then
                COMPOSE_FILES="$COMPOSE_FILES -f $DIAR_NATIVE_GPU_COMPOSE_FILE"
                print_info "  GPU reservation added for the diarization sidecar"
            else
                print_info "  Sidecar will run on CPU (slower than GPU, identical output)"
            fi
        fi
    fi

    # ALWAYS add offline last to ensure offline settings override everything
    COMPOSE_FILES="$COMPOSE_FILES -f $OFFLINE_COMPOSE_FILE"
}

#######################
# COMMANDS
#######################

cmd_start() {
    local use_gpu_scale="false"

    # Parse optional flags
    while [ $# -gt 0 ]; do
        case "$1" in
            --gpu-scale)
                use_gpu_scale="true"
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    print_info "Starting OpenTranscribe..."

    # Build compose files in correct order (offline.yml MUST be last)
    build_compose_files "$use_gpu_scale"

    if [ "$use_gpu_scale" = "true" ]; then
        print_info "Multi-GPU scaling enabled"
    fi

    check_env

    dc up -d

    print_success "OpenTranscribe started"
    print_info "Access the application at: http://localhost:5173"
    if [ "$use_gpu_scale" = "true" ]; then
        print_info "View GPU scaled workers: ./opentr.sh logs celery-worker-gpu-scaled"
    else
        print_info "View logs with: ./opentr.sh logs"
    fi
}

cmd_stop() {
    print_info "Stopping OpenTranscribe..."

    # Build compose files (no gpu-scale for stop)
    build_compose_files "false"

    dc down

    print_success "OpenTranscribe stopped"
}

cmd_restart() {
    local use_gpu_scale="false"

    # Parse optional flags
    while [ $# -gt 0 ]; do
        case "$1" in
            --gpu-scale)
                use_gpu_scale="true"
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    print_info "Restarting OpenTranscribe..."

    # Build compose files in correct order (offline.yml MUST be last)
    build_compose_files "$use_gpu_scale"

    if [ "$use_gpu_scale" = "true" ]; then
        print_info "Multi-GPU scaling enabled"
    fi

    dc restart

    print_success "OpenTranscribe restarted"
}

cmd_status() {
    print_info "Service Status:"
    echo ""

    # Build compose files (no gpu-scale for status)
    build_compose_files "false"

    dc ps
}

cmd_logs() {
    local service="${1:-}"

    # Build compose files (no gpu-scale for logs)
    build_compose_files "false"

    if [ -n "$service" ]; then
        print_info "Viewing logs for: $service"
        dc logs -f "$service"
    else
        print_info "Viewing logs for all services (Ctrl+C to exit)"
        dc logs -f
    fi
}

cmd_restart_backend() {
    print_info "Restarting backend services..."

    # Build compose files (no gpu-scale for restart-backend)
    build_compose_files "false"

    dc restart backend celery-worker celery-download-worker celery-cpu-worker celery-nlp-worker \
        celery-embedding-worker celery-redaction celery-cloud-asr-worker celery-beat flower

    print_success "Backend services restarted"
}

cmd_restart_frontend() {
    print_info "Restarting frontend..."

    # Build compose files (no gpu-scale for restart-frontend)
    build_compose_files "false"

    dc restart frontend

    print_success "Frontend restarted"
}

cmd_shell() {
    local service="${1:-backend}"

    print_info "Opening shell in $service container..."

    # Build compose files (no gpu-scale for shell)
    build_compose_files "false"

    dc exec "$service" /bin/bash
}

cmd_health() {
    print_info "Checking service health..."
    echo ""

    # Build compose files (no gpu-scale for health)
    build_compose_files "false"

    # Check each service
    local services=("postgres" "redis" "minio" "opensearch" "backend" "celery-worker" "celery-download-worker" "celery-cpu-worker" "celery-nlp-worker" "celery-embedding-worker" "celery-redaction" "celery-cloud-asr-worker" "celery-beat" "frontend" "flower" "docs")

    for service in "${services[@]}"; do
        if dc ps "$service" 2>/dev/null | grep -q "Up"; then
            local health
            health=$(dc ps "$service" | grep "$service" | awk '{print $6}')
            if [[ "$health" == *"healthy"* ]]; then
                echo -e "  ${GREEN}✓${NC} $service - healthy"
            elif [[ "$health" == *"unhealthy"* ]]; then
                echo -e "  ${RED}✗${NC} $service - unhealthy"
            else
                echo -e "  ${YELLOW}⚠${NC} $service - running (no health check)"
            fi
        else
            echo -e "  ${RED}✗${NC} $service - not running"
        fi
    done
    echo ""
}

cmd_clean() {
    print_warning "This will remove OpenTranscribe containers and volumes"
    print_warning "All data will be lost (database, uploads, models cache)"
    read -p "Continue? (y/N) " -n 1 -r
    echo

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Cancelled"
        exit 0
    fi

    # Build compose files (no gpu-scale for clean)
    build_compose_files "false"

    print_info "Stopping OpenTranscribe services..."
    dc down

    print_info "Removing OpenTranscribe containers and volumes..."
    dc down -v

    print_success "Cleanup complete - OpenTranscribe containers and volumes removed"
    print_info "Models cache preserved at: ${MODEL_CACHE_DIR:-/opt/opentranscribe/models}"
}

cmd_backup() {
    print_info "Creating database backup..."

    # Build compose files (no gpu-scale for backup)
    build_compose_files "false"

    local backup_dir="$INSTALL_DIR/backups"
    mkdir -p "$backup_dir"

    local backup_file
    backup_file="$backup_dir/opentranscribe_backup_$(date +%Y%m%d_%H%M%S).sql"

    if dc exec -T postgres pg_dump -U postgres opentranscribe > "$backup_file"; then
        print_success "Backup created: $backup_file"
        local size
        size=$(du -sh "$backup_file" | cut -f1)
        print_info "Backup size: $size"
    else
        print_error "Backup failed"
        exit 1
    fi
}

#######################
# MAIN
#######################

main() {
    # Check if we're in the right location
    check_location

    # Get command
    local command="${1:-help}"
    shift || true

    case "$command" in
        start)
            cmd_start "$@"
            ;;
        stop)
            cmd_stop
            ;;
        restart)
            cmd_restart "$@"
            ;;
        status)
            cmd_status
            ;;
        logs)
            cmd_logs "$@"
            ;;
        restart-backend)
            cmd_restart_backend
            ;;
        restart-frontend)
            cmd_restart_frontend
            ;;
        shell)
            cmd_shell "$@"
            ;;
        health)
            cmd_health
            ;;
        clean)
            cmd_clean
            ;;
        backup)
            cmd_backup
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "Unknown command: $command"
            echo ""
            show_help
            exit 1
            ;;
    esac
}

# Run main function
main "$@"
