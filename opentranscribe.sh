#!/bin/bash
set -e

YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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
    echo "  logs [svc]    View logs (all or specific service)"
    echo "  update        Pull latest Docker images and restart"
    echo "  update-full   Update images AND configuration files"
    echo "  clean         Remove all volumes and data (CAUTION)"
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
    echo "  ./opentranscribe.sh update           # Update containers only"
    echo "  ./opentranscribe.sh update-full      # Update everything"
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

fix_model_cache_permissions() {
    # Read MODEL_CACHE_DIR from .env if it exists
    local MODEL_CACHE_DIR=""
    if [ -f .env ]; then
        MODEL_CACHE_DIR=$(grep 'MODEL_CACHE_DIR' .env | grep -v '^#' | cut -d'#' -f1 | cut -d'=' -f2 | tr -d ' "' | head -1)
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
            if docker run --rm -v "$MODEL_CACHE_DIR:/models" busybox:latest sh -c "chown -R 1000:1000 /models && chmod -R 755 /models" > /dev/null 2>&1; then
                echo -e "${GREEN}✅ Model cache permissions fixed using Docker${NC}"
                return 0
            fi
        fi

        # Fallback: try direct chown if user has permissions
        if chown -R 1000:1000 "$MODEL_CACHE_DIR" > /dev/null 2>&1 && chmod -R 755 "$MODEL_CACHE_DIR" > /dev/null 2>&1; then
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
        value=$(grep '^FORCE_CPU_MODE=' .env 2>/dev/null \
            | cut -d'=' -f2 | tr -d ' "' | head -1)
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
    if [ -f .env ]; then
        nginx_server_name=$(grep '^NGINX_SERVER_NAME=' .env | grep -v '^#' | cut -d'=' -f2 | tr -d ' "' | head -1)
    fi

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

    echo "$compose_files"
}

show_access_info() {
    # Source .env to get port values
    source .env 2>/dev/null || true

    # Check if NGINX/HTTPS is configured
    local nginx_server_name=""
    if [ -f .env ]; then
        nginx_server_name=$(grep '^NGINX_SERVER_NAME=' .env | grep -v '^#' | cut -d'=' -f2 | tr -d ' "' | head -1)
    fi

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
        echo -e "${YELLOW}📥 Updating to latest Docker images...${NC}"
        compose_files=$(get_compose_files)

        # Read backend port from .env for direct health polling (below)
        backend_port=$(grep -E '^BACKEND_PORT=' .env 2>/dev/null | cut -d'=' -f2 | tr -d ' "' | head -1)
        backend_port="${backend_port:-5174}"

        docker compose $compose_files down
        docker compose $compose_files pull

        # Two-phase startup to work around compose's dependency resolver, which
        # gives up on `service_healthy` waits long before our backend's 600s
        # start_period elapses when upgrading a populated v0.3.x database.
        # Running the 45-migration Alembic chain + model warm-preload on first
        # boot can take 60-120 seconds; compose would otherwise report
        # "dependency failed to start: container opentranscribe-backend is
        # unhealthy" around the ~45s mark and SIGTERM the backend mid-migration.
        #
        # Phase 1: infrastructure + backend only, no dependents
        echo -e "${BLUE}▶ Starting infrastructure + backend (phase 1/2)...${NC}"
        docker compose $compose_files up -d postgres redis minio opensearch backend

        # Phase 2: poll the backend /health endpoint DIRECTLY (bypassing
        # compose's dependency resolver). Wait up to 15 minutes — enough
        # time for any realistic cold-boot migration path.
        echo -e "${BLUE}⏳ Waiting for backend to become healthy (up to 15 minutes)...${NC}"
        waited=0
        max_wait=900
        while [ $waited -lt $max_wait ]; do
            if curl -sf --connect-timeout 2 --max-time 4 \
                "http://localhost:${backend_port}/health" >/dev/null 2>&1; then
                echo -e "${GREEN}✓ Backend healthy after ${waited}s${NC}"
                break
            fi
            # Detect hard-fail: if backend container has exited with a non-zero
            # code, bail out rather than waste the full 15 minutes.
            state=$(docker inspect opentranscribe-backend \
                --format '{{.State.Status}}:{{.State.ExitCode}}' 2>/dev/null || echo "unknown:?")
            case "$state" in
                exited:0|restarting:*)
                    # Code 0 exit or restarting = docker restart policy is
                    # cycling the container; keep polling.
                    ;;
                exited:*)
                    echo -e "${RED}❌ Backend exited with non-zero code: $state${NC}"
                    echo -e "${YELLOW}Last 40 lines of backend logs:${NC}"
                    docker logs --tail 40 opentranscribe-backend 2>&1 || true
                    exit 1
                    ;;
            esac
            sleep 5
            waited=$((waited + 5))
        done

        if [ $waited -ge $max_wait ]; then
            echo -e "${RED}❌ Backend failed to become healthy within ${max_wait}s${NC}"
            echo -e "${YELLOW}Last 40 lines of backend logs:${NC}"
            docker logs --tail 40 opentranscribe-backend 2>&1 || true
            exit 1
        fi

        # Phase 3: backend is healthy — safe to start everything else
        echo -e "${BLUE}▶ Starting remaining services (phase 2/2)...${NC}"
        docker compose $compose_files up -d

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
        GITHUB_RAW="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${ENCODED_BRANCH}"

        if [ "$BRANCH" != "master" ]; then
            echo -e "${BLUE}ℹ️  Using branch: $BRANCH${NC}"
        fi

        # Backup current opentranscribe.sh
        cp opentranscribe.sh opentranscribe.sh.bak 2>/dev/null || true

        echo -e "${BLUE}📄 Updating configuration files...${NC}"

        # Update docker-compose files
        echo "  Downloading docker-compose.prod.yml..."
        curl -fsSL "$GITHUB_RAW/docker-compose.prod.yml" -o docker-compose.prod.yml.new && \
            mv docker-compose.prod.yml.new docker-compose.prod.yml && \
            echo -e "  ${GREEN}✓${NC} docker-compose.prod.yml" || \
            echo -e "  ${YELLOW}⚠️${NC} docker-compose.prod.yml (skipped)"

        echo "  Downloading docker-compose.nginx.yml..."
        curl -fsSL "$GITHUB_RAW/docker-compose.nginx.yml" -o docker-compose.nginx.yml.new && \
            mv docker-compose.nginx.yml.new docker-compose.nginx.yml && \
            echo -e "  ${GREEN}✓${NC} docker-compose.nginx.yml" || \
            echo -e "  ${YELLOW}⚠️${NC} docker-compose.nginx.yml (skipped)"

        echo "  Downloading docker-compose.gpu.yml..."
        curl -fsSL "$GITHUB_RAW/docker-compose.gpu.yml" -o docker-compose.gpu.yml.new && \
            mv docker-compose.gpu.yml.new docker-compose.gpu.yml && \
            echo -e "  ${GREEN}✓${NC} docker-compose.gpu.yml" || \
            echo -e "  ${YELLOW}⚠️${NC} docker-compose.gpu.yml (skipped)"

        echo "  Downloading docker-compose.blackwell.yml..."
        curl -fsSL "$GITHUB_RAW/docker-compose.blackwell.yml" -o docker-compose.blackwell.yml.new && \
            mv docker-compose.blackwell.yml.new docker-compose.blackwell.yml && \
            echo -e "  ${GREEN}✓${NC} docker-compose.blackwell.yml" || \
            echo -e "  ${YELLOW}⚠️${NC} docker-compose.blackwell.yml (skipped)"

        # Update NGINX configuration
        mkdir -p nginx/ssl
        echo "  Downloading nginx/site.conf.template..."
        curl -fsSL "$GITHUB_RAW/nginx/site.conf.template" -o nginx/site.conf.template.new && \
            mv nginx/site.conf.template.new nginx/site.conf.template && \
            echo -e "  ${GREEN}✓${NC} nginx/site.conf.template" || \
            echo -e "  ${YELLOW}⚠️${NC} nginx/site.conf.template (skipped)"

        # Update scripts
        mkdir -p scripts
        echo "  Downloading scripts/generate-ssl-cert.sh..."
        curl -fsSL "$GITHUB_RAW/scripts/generate-ssl-cert.sh" -o scripts/generate-ssl-cert.sh.new && \
            mv scripts/generate-ssl-cert.sh.new scripts/generate-ssl-cert.sh && \
            chmod +x scripts/generate-ssl-cert.sh && \
            echo -e "  ${GREEN}✓${NC} scripts/generate-ssl-cert.sh" || \
            echo -e "  ${YELLOW}⚠️${NC} scripts/generate-ssl-cert.sh (skipped)"

        echo "  Downloading scripts/fix-model-permissions.sh..."
        curl -fsSL "$GITHUB_RAW/scripts/fix-model-permissions.sh" -o scripts/fix-model-permissions.sh.new && \
            mv scripts/fix-model-permissions.sh.new scripts/fix-model-permissions.sh && \
            chmod +x scripts/fix-model-permissions.sh && \
            echo -e "  ${GREEN}✓${NC} scripts/fix-model-permissions.sh" || \
            echo -e "  ${YELLOW}⚠️${NC} scripts/fix-model-permissions.sh (skipped)"

        # Update this management script itself
        echo "  Downloading opentranscribe.sh..."
        curl -fsSL "$GITHUB_RAW/opentranscribe.sh" -o opentranscribe.sh.new && \
            mv opentranscribe.sh.new opentranscribe.sh && \
            chmod +x opentranscribe.sh && \
            echo -e "  ${GREEN}✓${NC} opentranscribe.sh" || \
            echo -e "  ${YELLOW}⚠️${NC} opentranscribe.sh (skipped)"

        echo ""
        echo -e "${BLUE}🐳 Updating Docker images...${NC}"
        fix_model_cache_permissions
        compose_files=$(get_compose_files)
        docker compose $compose_files down
        docker compose $compose_files pull
        docker compose $compose_files up -d

        echo ""
        echo -e "${GREEN}✅ Full update complete!${NC}"
        echo ""
        echo -e "${YELLOW}📝 Notes:${NC}"
        echo "  • Your .env configuration was preserved"
        echo "  • SSL certificates were preserved (if configured)"
        echo "  • Database and transcriptions were preserved"
        echo "  • Old script backed up to opentranscribe.sh.bak"
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
        if [ -f .env ]; then
            nginx_server_name=$(grep '^NGINX_SERVER_NAME=' .env | grep -v '^#' | cut -d'=' -f2 | tr -d ' "' | head -1)
        fi

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
            echo "   curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/scripts/generate-ssl-cert.sh -o scripts/generate-ssl-cert.sh"
            echo "   chmod +x scripts/generate-ssl-cert.sh"
            exit 1
        fi

        # Check if docker-compose.nginx.yml exists
        if [ ! -f docker-compose.nginx.yml ]; then
            echo -e "${RED}❌ NGINX docker-compose file not found${NC}"
            echo "   Expected: docker-compose.nginx.yml"
            echo ""
            echo "   Download it from:"
            echo "   curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/docker-compose.nginx.yml -o docker-compose.nginx.yml"
            exit 1
        fi

        # Check if nginx/site.conf.template exists
        if [ ! -f nginx/site.conf.template ]; then
            echo -e "${YELLOW}⚠️  NGINX configuration template not found${NC}"
            echo "   Downloading nginx/site.conf.template..."
            mkdir -p nginx/ssl
            curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/nginx/site.conf.template -o nginx/site.conf.template || {
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
        if [ -f .env ]; then
            current_hostname=$(grep '^NGINX_SERVER_NAME=' .env | grep -v '^#' | cut -d'=' -f2 | tr -d ' "' | head -1)
        fi

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

        # Get local version from backend container if running
        local_version="unknown"
        if docker compose ps 2>/dev/null | grep -q "backend.*Up"; then
            local_version=$(docker compose exec -T backend python -c "from app.core.version import VERSION; print(VERSION)" 2>/dev/null || echo "unknown")
        fi

        # Try to get version from docker image labels
        if [ "$local_version" = "unknown" ]; then
            local_version=$(docker inspect davidamacey/opentranscribe-backend:latest 2>/dev/null | grep -o '"version":"[^"]*"' | head -1 | cut -d'"' -f4 || echo "unknown")
        fi

        echo "  Local version: ${local_version:-unknown}"

        # Check for latest version from GitHub
        echo ""
        echo -e "${BLUE}Checking for updates...${NC}"
        latest_version=$(curl -fsSL --connect-timeout 5 "https://api.github.com/repos/davidamacey/OpenTranscribe/releases/latest" 2>/dev/null | grep '"tag_name"' | head -1 | sed -E 's/.*"v?([^"]+)".*/\1/' || echo "")

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
