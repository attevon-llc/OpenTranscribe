#!/bin/bash
set -e

# Remote ARM64 Builder Setup Script
# Configures Mac Studio (or other ARM64 machine) as a remote builder for Docker Buildx
# This dramatically speeds up ARM64 builds by using native compilation instead of QEMU emulation

# shellcheck source=scripts/lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/log.sh"

# Configuration
BUILDER_NAME="opentranscribe-multiarch"
REMOTE_BUILDER_NAME="remote-arm64"

# Function to show usage
show_usage() {
    cat << EOF
Usage: $0 [COMMAND] [OPTIONS]

Setup and manage remote ARM64 builder for faster multi-platform Docker builds

Commands:
    setup       Configure remote ARM64 builder (interactive)
    test        Test remote builder connectivity and capability
    status      Show current builder configuration
    remove      Remove remote builder configuration
    help        Show this help message

Options:
    --host USER@HOST    Remote builder SSH host (e.g., user@mac-studio.local)
    --name NAME         Builder name (default: ${BUILDER_NAME})

Examples:
    # Interactive setup
    $0 setup

    # Setup with specific host
    $0 setup --host user@192.168.1.100

    # Test connectivity
    $0 test

    # Check status
    $0 status

    # Remove configuration
    $0 remove

How it works:
    1. Creates SSH connection to your Mac Studio (or other ARM64 machine)
    2. Creates a multi-node Docker buildx builder:
       - Node 1 (local): Builds linux/amd64 images
       - Node 2 (remote): Builds linux/arm64 images natively
    3. Parallel builds: Both platforms build simultaneously
    4. 10-20x faster ARM64 builds (native vs QEMU emulation)

Prerequisites:
    - Remote machine must have Docker installed and running
    - SSH key-based authentication configured (recommended)
    - Both machines accessible on network

EOF
}

# Function to check if builder exists
builder_exists() {
    docker buildx inspect "${BUILDER_NAME}" > /dev/null 2>&1
}

# Function to test remote connection
test_remote_connection() {
    local remote_host=$1

    print_info "Testing SSH connection to ${remote_host}..."
    if ssh -o ConnectTimeout=5 -o BatchMode=yes "${remote_host}" "echo 'SSH connection successful'" 2>/dev/null; then
        print_success "SSH connection OK"
    else
        print_error "Cannot connect via SSH to ${remote_host}"
        print_info "Please ensure:"
        print_info "  1. SSH is enabled on the remote machine"
        print_info "  2. SSH key-based authentication is configured"
        print_info "  3. You can run: ssh ${remote_host} 'echo test'"
        return 1
    fi

    print_info "Testing Docker on remote machine..."
    # Try direct docker command first, then try sourcing profile if needed
    if ssh "${remote_host}" "docker info" > /dev/null 2>&1; then
        print_success "Docker is running on remote machine"
    elif ssh "${remote_host}" "source ~/.zprofile 2>/dev/null && docker info" > /dev/null 2>&1; then
        print_success "Docker is running on remote machine (via .zprofile)"
        print_warning "Note: Docker requires sourcing .zprofile for SSH sessions"
    elif ssh "${remote_host}" "source ~/.bash_profile 2>/dev/null && docker info" > /dev/null 2>&1; then
        print_success "Docker is running on remote machine (via .bash_profile)"
        print_warning "Note: Docker requires sourcing .bash_profile for SSH sessions"
    else
        print_error "Docker is not accessible on remote machine"
        print_info "Please ensure Docker is installed and running on ${remote_host}"
        print_info "Or create a symlink: sudo ln -sf /Applications/Docker.app/Contents/Resources/bin/docker /usr/local/bin/docker"
        return 1
    fi

    print_info "Checking remote platform..."
    # Try to get platform, sourcing profile if needed
    local remote_platform
    remote_platform=$(ssh "${remote_host}" "docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || (source ~/.zprofile 2>/dev/null && docker version --format '{{.Server.Os}}/{{.Server.Arch}}') || (source ~/.bash_profile 2>/dev/null && docker version --format '{{.Server.Os}}/{{.Server.Arch}}')")
    print_info "Remote platform: ${remote_platform}"

    if [[ "${remote_platform}" == *"arm64"* ]] || [[ "${remote_platform}" == *"aarch64"* ]]; then
        print_success "Remote machine is ARM64 - perfect for native ARM builds!"
    else
        print_warning "Remote machine is ${remote_platform} (not ARM64)"
        print_warning "This will still work, but won't provide the native build speed benefit"
    fi

    return 0
}

# Function to create remote builder
setup_builder() {
    local remote_host=$1

    print_info "Setting up multi-architecture builder..."

    # Test connection first
    if ! test_remote_connection "${remote_host}"; then
        print_error "Remote connection test failed. Cannot proceed."
        return 1
    fi

    # Remove existing builder if it exists
    if builder_exists; then
        print_warning "Builder '${BUILDER_NAME}' already exists"
        read -p "Remove and recreate? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            print_info "Removing existing builder..."
            docker buildx rm "${BUILDER_NAME}"
        else
            print_info "Keeping existing builder. Use '$0 remove' to remove it."
            return 0
        fi
    fi

    # Create Docker context for remote machine
    print_info "Creating Docker context for remote machine..."
    if docker context inspect "${REMOTE_BUILDER_NAME}" > /dev/null 2>&1; then
        print_warning "Docker context '${REMOTE_BUILDER_NAME}' already exists, removing..."
        docker context rm "${REMOTE_BUILDER_NAME}"
    fi

    docker context create "${REMOTE_BUILDER_NAME}" \
        --docker "host=ssh://${remote_host}" \
        --description "Remote ARM64 builder for OpenTranscribe"

    print_success "Docker context created: ${REMOTE_BUILDER_NAME}"

    # Create multi-node buildx builder
    print_info "Creating multi-node buildx builder..."

    # Create builder with local node (AMD64)
    docker buildx create \
        --name "${BUILDER_NAME}" \
        --platform linux/amd64 \
        --driver docker-container \
        --use

    # Add remote node (ARM64)
    docker buildx create \
        --name "${BUILDER_NAME}" \
        --append \
        --platform linux/arm64 \
        --driver docker-container \
        "${REMOTE_BUILDER_NAME}"

    print_success "Multi-node builder created: ${BUILDER_NAME}"

    # Bootstrap the builder (starts the build containers)
    print_info "Bootstrapping builder (this may take a minute)..."
    docker buildx inspect --bootstrap

    print_success "Builder setup complete!"
    print_info ""
    print_info "Configuration summary:"
    docker buildx inspect "${BUILDER_NAME}"

    print_info ""
    print_success "✅ Remote builder is ready!"
    print_info ""
    print_info "Next steps:"
    print_info "  1. Test the builder: $0 test"
    print_info "  2. Update docker-build-push.sh to use remote builder:"
    print_info "     export USE_REMOTE_BUILDER=true"
    print_info "     ./scripts/docker-build-push.sh"
    print_info ""
    print_info "The builder will automatically distribute builds:"
    print_info "  - Local machine: linux/amd64 (native)"
    print_info "  - Remote machine (${remote_host}): linux/arm64 (native)"
    print_info ""
    print_info "Expected speedup: 10-20x faster ARM64 builds!"
}

# Function to test builder
test_builder() {
    if ! builder_exists; then
        print_error "Builder '${BUILDER_NAME}' does not exist"
        print_info "Run '$0 setup' to create it first"
        return 1
    fi

    print_info "Testing builder configuration..."
    docker buildx inspect "${BUILDER_NAME}"

    print_info ""
    print_info "Testing connectivity to all nodes..."

    # Create a minimal test Dockerfile
    local test_dir
    test_dir=$(mktemp -d)
    cat > "${test_dir}/Dockerfile" << 'EOF'
FROM alpine:latest
RUN echo "Platform: $(uname -m)" > /platform.txt
CMD cat /platform.txt
EOF

    print_info "Building test image for both platforms..."

    if docker buildx build \
        --builder "${BUILDER_NAME}" \
        --platform linux/amd64,linux/arm64 \
        --tag opentranscribe-test:latest \
        "${test_dir}"; then
        print_success "✅ Test build successful on all platforms!"
    else
        print_error "Test build failed"
        rm -rf "${test_dir}"
        return 1
    fi

    rm -rf "${test_dir}"

    print_info ""
    print_success "Remote builder is working correctly!"
    print_info "You can now use it with docker-build-push.sh"
}

# Function to show status
show_status() {
    print_info "Current builder configuration:"
    print_info ""

    if builder_exists; then
        docker buildx inspect "${BUILDER_NAME}"
        print_info ""
        print_success "Remote builder '${BUILDER_NAME}' is configured"

        # Check if it's currently active
        local current_builder
        current_builder=$(docker buildx inspect --bootstrap 2>/dev/null | grep "^Name:" | awk '{print $2}')
        if [ "${current_builder}" = "${BUILDER_NAME}" ]; then
            print_success "✅ Remote builder is ACTIVE"
        else
            print_warning "Remote builder exists but is not active"
            print_info "To activate: docker buildx use ${BUILDER_NAME}"
        fi
    else
        print_warning "Remote builder '${BUILDER_NAME}' is not configured"
        print_info "Run '$0 setup' to create it"
    fi

    print_info ""
    print_info "All builders:"
    docker buildx ls
}

# Function to remove builder
remove_builder() {
    if ! builder_exists; then
        print_warning "Builder '${BUILDER_NAME}' does not exist"
        return 0
    fi

    print_warning "This will remove the remote builder configuration"
    read -p "Are you sure? (y/N): " -n 1 -r
    echo

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Cancelled"
        return 0
    fi

    print_info "Removing builder..."
    docker buildx rm "${BUILDER_NAME}"

    print_info "Removing Docker context..."
    if docker context inspect "${REMOTE_BUILDER_NAME}" > /dev/null 2>&1; then
        docker context rm "${REMOTE_BUILDER_NAME}"
    fi

    print_success "Remote builder removed"
    print_info "You can recreate it anytime with '$0 setup'"
}

# Interactive setup
interactive_setup() {
    print_info "OpenTranscribe Remote ARM64 Builder Setup"
    print_info "=========================================="
    print_info ""
    print_info "This will configure your Mac Studio (or other ARM64 machine) as a"
    print_info "remote builder for dramatically faster ARM64 Docker builds."
    print_info ""

    # Get remote host
    print_info "Enter the SSH connection string for your remote builder"
    print_info "Format: user@hostname or user@ip-address"
    print_info "Example: user@mac-studio.local or user@192.168.1.100"
    read -p "Remote host: " remote_host

    if [ -z "${remote_host}" ]; then
        print_error "Remote host cannot be empty"
        return 1
    fi

    setup_builder "${remote_host}"
}

# Main script
main() {
    local command=${1:-help}
    local remote_host=""

    # Parse arguments
    shift || true
    while [[ $# -gt 0 ]]; do
        case $1 in
            --host)
                remote_host="$2"
                shift 2
                ;;
            --name)
                BUILDER_NAME="$2"
                shift 2
                ;;
            *)
                print_error "Unknown option: $1"
                show_usage
                exit 1
                ;;
        esac
    done

    case "${command}" in
        setup)
            if [ -n "${remote_host}" ]; then
                setup_builder "${remote_host}"
            else
                interactive_setup
            fi
            ;;
        test)
            test_builder
            ;;
        status)
            show_status
            ;;
        remove)
            remove_builder
            ;;
        help|--help|-h)
            show_usage
            ;;
        *)
            print_error "Unknown command: ${command}"
            show_usage
            exit 1
            ;;
    esac
}

# Run main function
main "$@"
