#!/bin/bash
set -e

# OpenTranscribe Offline Uninstallation Script
# Removes OpenTranscribe from air-gapped systems
# Usage: sudo /opt/opentranscribe/uninstall.sh

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
INSTALL_DIR="/opt/opentranscribe"
# Use base + offline override pattern
COMPOSE_FILES="-f $INSTALL_DIR/docker-compose.yml -f $INSTALL_DIR/docker-compose.offline.yml"
BACKUP_DIR="$INSTALL_DIR/backups"

# Grace period (seconds) for CUDA-holding services on stop/down (issue #782). This script
# ships standalone inside the offline package and sources nothing else, so it carries its
# own copy of the default -- same value as docker-compose.yml's `${OT_STOP_GRACE_GPU:-60}s`
# and scripts/common.sh's OT_STOP_GRACE_GPU, checked by
# backend/tests/unit/test_stop_grace_period_wiring.py.
OT_STOP_GRACE_GPU="${OT_STOP_GRACE_GPU:-60}"

# Give the CUDA-holding services a chance to release VRAM cleanly before `down` reaches
# them. Compose v2.29.7 bakes `stop_grace_period` into the container's Config.StopTimeout
# at CREATE time and the daemon honours it even for this bare `stop`, but an install
# predating that key still needs the explicit -t here. Scoped to the CUDA-holding
# SERVICES ONLY -- a global `down -t N` would also give Postgres/MinIO/OpenSearch N
# seconds to hang in. Safe against an absent service name: 2>/dev/null || true.
ot_drain_gpu_workers() {
    # shellcheck disable=SC2086
    docker compose $COMPOSE_FILES stop -t "$OT_STOP_GRACE_GPU" \
        celery-worker celery-worker-gpu-transcribe celery-worker-gpu-diarize \
        celery-worker-gpu-scaled celery-redaction celery-cpu-worker diar-native \
        2>/dev/null || true
}

#######################
# HELPER FUNCTIONS
#######################

print_header() {
    echo -e "\n${CYAN}================================================================${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}================================================================${NC}\n"
}

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

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check if running as root
check_root() {
    if [ "$EUID" -ne 0 ]; then
        print_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# Check if installation exists
check_installation() {
    if [ ! -d "$INSTALL_DIR" ]; then
        print_error "OpenTranscribe installation not found at: $INSTALL_DIR"
        exit 1
    fi

    if [ ! -f "$INSTALL_DIR/docker-compose.yml" ] || [ ! -f "$INSTALL_DIR/docker-compose.offline.yml" ]; then
        print_warning "Docker Compose files not found in: $INSTALL_DIR"
        print_warning "Installation may be incomplete or already removed"
    fi
}

#######################
# BACKUP
#######################

offer_backup() {
    print_header "Database Backup"

    echo -e "${YELLOW}Would you like to create a database backup before uninstalling?${NC}"
    echo "This will save your transcription data for future use."
    echo ""
    read -p "Create backup? (y/N) " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        create_backup
    else
        print_info "Skipping backup"
    fi
}

create_backup() {
    print_info "Creating database backup..."

    mkdir -p "$BACKUP_DIR"

    local backup_file
    backup_file="$BACKUP_DIR/opentranscribe_uninstall_backup_$(date +%Y%m%d_%H%M%S).sql"

    # Check if Docker is running and postgres container exists
    if ! docker info > /dev/null 2>&1; then
        print_warning "Docker is not running - cannot create backup"
        return 1
    fi

    # Try to create backup if postgres container exists
    if docker compose $COMPOSE_FILES ps postgres | grep -q "Up"; then
        docker compose $COMPOSE_FILES exec -T postgres pg_dump -U postgres opentranscribe > "$backup_file" 2>/dev/null || {
            print_warning "Could not create backup - postgres may not be running"
            return 1
        }

        if [ -f "$backup_file" ] && [ -s "$backup_file" ]; then
            print_success "Backup created: $backup_file"
            local size
            size=$(du -sh "$backup_file" 2>/dev/null | cut -f1 || echo "unknown")
            print_info "Backup size: $size"
            print_info "Backup preserved at: $backup_file"
            echo ""
            print_warning "IMPORTANT: Move this backup file to a safe location before continuing!"
            read -r -p "Press Enter to continue after saving the backup..."
        else
            print_warning "Backup file is empty or failed to create"
            return 1
        fi
    else
        print_warning "PostgreSQL container is not running - cannot create backup"
        return 1
    fi
}

#######################
# UNINSTALL
#######################

confirm_uninstall() {
    print_header "Confirm Uninstallation"

    echo -e "${RED}WARNING: This will completely remove OpenTranscribe from your system!${NC}"
    echo ""
    echo "The following will be removed:"
    echo "  - All Docker containers and services"
    echo "  - All Docker volumes (database, uploaded files, etc.)"
    echo "  - Installation directory: $INSTALL_DIR"
    echo "  - AI models cache (~38GB)"
    echo "  - All configuration files"
    echo ""
    echo -e "${RED}THIS CANNOT BE UNDONE!${NC}"
    echo ""
    echo "Type 'yes' to confirm uninstallation:"
    read -r confirmation

    if [ "$confirmation" != "yes" ]; then
        print_info "Uninstallation cancelled"
        exit 0
    fi
}

stop_services() {
    print_header "Stopping Services"

    if [ ! -f "$INSTALL_DIR/docker-compose.yml" ] || [ ! -f "$INSTALL_DIR/docker-compose.offline.yml" ]; then
        print_warning "Docker Compose files not found - skipping service stop"
        return
    fi

    # Check if Docker is running
    if ! docker info > /dev/null 2>&1; then
        print_warning "Docker is not running - skipping service stop"
        return
    fi

    print_info "Stopping OpenTranscribe services..."

    cd "$INSTALL_DIR" || {
        print_warning "Could not change to $INSTALL_DIR"
        return
    }

    # Drain CUDA-holding workers before `down` reaches them (issue #782).
    ot_drain_gpu_workers

    # Stop services without removing volumes yet
    docker compose $COMPOSE_FILES down 2>/dev/null || {
        print_warning "Some services may have already been stopped"
    }

    print_success "Services stopped"
}

remove_volumes() {
    print_header "Removing Docker Volumes"

    if [ ! -f "$INSTALL_DIR/docker-compose.yml" ] || [ ! -f "$INSTALL_DIR/docker-compose.offline.yml" ]; then
        print_warning "Docker Compose files not found - skipping volume removal"
        return
    fi

    # Check if Docker is running
    if ! docker info > /dev/null 2>&1; then
        print_warning "Docker is not running - skipping volume removal"
        return
    fi

    echo -e "${RED}WARNING: This will delete all OpenTranscribe data!${NC}"
    echo "  - Database (all transcriptions)"
    echo "  - Uploaded media files"
    echo "  - Redis cache"
    echo "  - OpenSearch indices"
    echo "  - Flower task history"
    echo ""
    read -p "Remove all volumes and data? (yes/N) " -r confirmation

    if [ "$confirmation" != "yes" ]; then
        print_info "Skipping volume removal"
        print_warning "Docker volumes will remain - use 'docker volume prune' to clean manually"
        return
    fi

    print_info "Removing Docker volumes..."

    cd "$INSTALL_DIR" || {
        print_warning "Could not change to $INSTALL_DIR"
        return
    }

    # Drain CUDA-holding workers before the destructive `down -v` below (issue #782).
    # A no-op if stop_services() already ran (services already stopped/gone).
    ot_drain_gpu_workers

    docker compose $COMPOSE_FILES down -v 2>/dev/null || {
        print_warning "Some volumes may have already been removed"
    }

    print_success "Docker volumes removed"
}

remove_images() {
    print_header "Docker Images"

    echo "Would you like to remove OpenTranscribe Docker images?"
    echo "  - davidamacey/opentranscribe-backend:latest (~4GB)"
    echo "  - davidamacey/opentranscribe-frontend:latest (~100MB)"
    echo "  - postgres:17.5-alpine"
    echo "  - redis:8.2.2-alpine3.22"
    echo "  - minio/minio:RELEASE.2025-09-07T16-13-09Z"
    echo "  - opensearchproject/opensearch:3.4.0"
    echo ""
    echo "Note: Removing images will require re-loading them if you reinstall"
    echo ""
    read -p "Remove Docker images? (y/N) " -n 1 -r
    echo

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Skipping image removal - images preserved for future use"
        return
    fi

    print_info "Removing Docker images..."

    # Define images to remove (matching docker-compose.offline.yml)
    local images=(
        "davidamacey/opentranscribe-backend:latest"
        "davidamacey/opentranscribe-frontend:latest"
        "davidamacey/opentranscribe-docs:latest"
        "postgres:17.5-alpine"
        "redis:8.2.2-alpine3.22"
        "minio/minio:RELEASE.2025-09-07T16-13-09Z"
        "opensearchproject/opensearch:3.4.0"
    )

    for image in "${images[@]}"; do
        if docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "^${image}$"; then
            print_info "  Removing: $image"
            docker rmi "$image" 2>/dev/null || print_warning "  Could not remove: $image"
        else
            print_info "  Not found: $image (already removed)"
        fi
    done

    print_success "Docker images removed"
}

remove_installation() {
    print_header "Removing Installation Files"

    if [ ! -d "$INSTALL_DIR" ]; then
        print_warning "Installation directory not found - already removed?"
        return
    fi

    print_info "Removing installation directory: $INSTALL_DIR"
    print_warning "This includes:"
    echo "  - Configuration files (.env)"
    echo "  - Docker Compose configuration"
    echo "  - AI models cache (~38GB)"
    echo "  - Management scripts"
    echo "  - Database backups (if not moved)"
    echo ""

    # Check if backups exist
    if [ -d "$BACKUP_DIR" ] && [ "$(ls -A $BACKUP_DIR 2>/dev/null)" ]; then
        echo -e "${YELLOW}IMPORTANT: Backups found in $BACKUP_DIR${NC}"
        echo "Make sure you have saved these backups to another location!"
        echo ""
        read -r -p "Press Enter to continue (backups will be deleted)..."
    fi

    # Remove the entire installation directory
    rm -rf "$INSTALL_DIR" || {
        print_error "Failed to remove $INSTALL_DIR"
        print_info "You may need to remove it manually: sudo rm -rf $INSTALL_DIR"
        exit 1
    }

    print_success "Installation files removed"
}

#######################
# CLEANUP
#######################

final_cleanup() {
    print_header "Final Cleanup"

    echo "Would you like to clean up unused Docker resources?"
    echo "This removes:"
    echo "  - Stopped containers"
    echo "  - Unused networks"
    echo "  - Dangling images"
    echo "  - Build cache"
    echo ""
    read -p "Run Docker system cleanup? (y/N) " -n 1 -r
    echo

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Skipping Docker system cleanup"
        return
    fi

    print_info "Running Docker system cleanup..."

    docker system prune -f 2>/dev/null || {
        print_warning "Could not run Docker system prune"
    }

    print_success "Docker system cleanup complete"
}

#######################
# MAIN
#######################

main() {
    print_header "OpenTranscribe Uninstaller"

    print_info "This will uninstall OpenTranscribe from: $INSTALL_DIR"
    echo ""

    # Pre-flight checks
    check_root
    check_installation

    # Offer backup before proceeding
    offer_backup

    # Confirm uninstallation
    confirm_uninstall

    # Execute uninstallation steps
    stop_services
    remove_volumes
    remove_images
    remove_installation
    final_cleanup

    # Final summary
    print_header "Uninstallation Complete"

    echo -e "${GREEN}✅ OpenTranscribe has been successfully uninstalled!${NC}\n"

    echo -e "${CYAN}What was removed:${NC}"
    echo -e "  ✓ All Docker containers and services"
    echo -e "  ✓ Docker volumes (database, files, cache)"
    echo -e "  ✓ Installation directory: $INSTALL_DIR"
    echo -e "  ✓ Configuration files"
    echo -e "  ✓ AI models cache"
    echo ""

    if [ -d "$BACKUP_DIR" ]; then
        echo -e "${YELLOW}Note: Backup directory may still exist if not included in installation${NC}"
        echo -e "      Check manually: ls $BACKUP_DIR"
        echo ""
    fi

    echo -e "${CYAN}To reinstall OpenTranscribe:${NC}"
    echo -e "  1. Extract the offline package"
    echo -e "  2. Run: sudo ./install.sh"
    echo ""

    print_success "Uninstallation complete! 🎉"
}

# Run main function
main
