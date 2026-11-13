#!/usr/bin/env bash
# Sourced by docker-build-push.sh: per-process buildx builder selection.
# Never `docker buildx use`: it rewrites the host's current builder for every other shell and
# build, and concurrent invocations raced on it. BUILDX_BUILDER is scoped to this process
# (Docker docs, "Build variables"). Needs the caller's USE_REMOTE_BUILDER, BUILD_MODE,
# BUILD_CACHE_REGISTRY, BUILD_TARGET, *_BUILDER_NAME and effective_platforms_for_target.

# True when every platform built is the daemon's own (the `docker` driver builds it natively).
targets_host_arch_only() {
    local host rest
    host="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null)" || return 1
    [ -n "${host}" ] || return 1
    rest="$(effective_platforms_for_target "${BUILD_TARGET}")"
    rest="${rest//${host}/}"
    rest="${rest//,/}"
    [ -z "${rest}" ]
}

select_buildx_builder() {
    if [ "${USE_REMOTE_BUILDER}" = "true" ]; then
        # Use remote multi-arch builder
        if ! docker buildx inspect "${REMOTE_BUILDER_NAME}" > /dev/null 2>&1; then
            print_error "Remote builder '${REMOTE_BUILDER_NAME}' not found!"
            print_info "Please run: ./scripts/setup-remote-builder.sh setup"
            print_info "Or set USE_REMOTE_BUILDER=false to use QEMU emulation"
            exit 1
        fi
        print_success "Using remote multi-arch builder: ${REMOTE_BUILDER_NAME}"
        print_info "This will use native ARM64 builds on your remote machine (much faster!)"
        # Per-process builder (Docker docs, "Build variables"); never `docker buildx use`.
        export BUILDX_BUILDER="${REMOTE_BUILDER_NAME}"
        docker buildx inspect --bootstrap
    elif [ "${BUILD_MODE}" = "local" ] && [ "${BUILD_CACHE_REGISTRY}" != "true" ] \
        && targets_host_arch_only; then
        # Host-arch --load on the `docker` driver (Docker docs, "Build drivers"): no tarball
        # export/re-import. Registry-cache opt-in keeps the container driver.
        export BUILDX_BUILDER="${LOCAL_LOAD_BUILDER:-default}"
        print_info "Local --load build on builder '${BUILDX_BUILDER}' (docker driver)"
    else
        # Use local builder with QEMU emulation (slower but works without setup)
        if ! docker buildx inspect "${DEFAULT_BUILDER_NAME}" > /dev/null 2>&1; then
            print_info "Creating local buildx builder (with QEMU emulation)..."
            docker buildx create --name "${DEFAULT_BUILDER_NAME}"
        else
            print_info "Using existing local buildx builder (with QEMU emulation)"
        fi
        export BUILDX_BUILDER="${DEFAULT_BUILDER_NAME}"
        docker buildx inspect --bootstrap
        if [[ "$(effective_platforms_for_target "${BUILD_TARGET}")" == *"arm64"* ]]; then
            print_warning "Building ARM64 with QEMU emulation (slow)"
            print_info "For faster builds, set up remote builder: ./scripts/setup-remote-builder.sh"
            print_info "Then use: USE_REMOTE_BUILDER=true $0"
        fi
    fi
}
