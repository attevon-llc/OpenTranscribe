#!/bin/bash
set -e

# Docker Build and Push Script for OpenTranscribe
# Quick fix for pushing Docker images to Docker Hub locally

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:-davidamacey}"
REPO_BACKEND="${DOCKERHUB_USERNAME}/opentranscribe-backend"
REPO_BACKEND_LITE="${DOCKERHUB_USERNAME}/opentranscribe-backend-lite"
REPO_FRONTEND="${DOCKERHUB_USERNAME}/opentranscribe-frontend"
REPO_DOCS="${DOCKERHUB_USERNAME}/opentranscribe-docs"

# Get commit SHA for tagging
COMMIT_SHA=$(git rev-parse --short HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

# --- Per-component platform + capability table (issue #680) -----------------
#
# Capability lives in the REPOSITORY (opentranscribe-backend is CUDA,
# opentranscribe-backend-lite is CPU), and is RESTATED in the tag as the
# `-<cap>-<arch>` leg (e.g. v0.5.0-cuda-amd64, v0.5.0-cpu-arm64). The vX.Y.Z tag
# on each repo is a multi-arch INDEX assembled from that repo's own legs only —
# never mixed across repos/capabilities.
#
# v0.5.0 ships exactly THREE artifacts: backend cuda-amd64, lite cpu-amd64,
# lite cpu-arm64. A `cuda-arm64` backend leg is RESERVED in this table (empty
# platform list) but not built — diar-native publishes no CUDA arm64 artifact
# (see backend/Dockerfile.prod's diar-native-bin-arm64 stage). blackwell is
# arm64-only, GPU-generation-gated (SM_121+, not host-arch-gated — see
# build_backend_blackwell), and is never built by `all`/`auto`.
#
# `frontend` and `docs` carry no GPU/CPU capability distinction, so their
# "capability" is the literal string `multiarch` and they get no `-<cap>-<arch>`
# leg tags — they keep the historical single multi-platform `buildx build --push`
# under one tag, exactly as before this table existed.
declare -A COMPONENT_CAPABILITY=(
    [backend]="cuda"
    [lite]="cpu"
    [frontend]="multiarch"
    [docs]="multiarch"
    [blackwell]="blackwell"
)
declare -A COMPONENT_PLATFORMS=(
    [backend]="linux/amd64"
    [lite]="linux/amd64,linux/arm64"
    [frontend]="linux/amd64,linux/arm64"
    [docs]="linux/amd64,linux/arm64"
    [blackwell]="linux/arm64"
)

# PLATFORMS is an explicit OVERRIDE, not the default any more (issue #680 —
# the old unconditional "linux/amd64,linux/arm64" default is exactly what let a
# broken/degraded arm64 backend leg publish under the same tag as a good amd64
# one with nothing to notice). Leave unset to use COMPONENT_PLATFORMS above;
# set it to force every component being built onto the same platform list
# (e.g. `PLATFORMS=linux/amd64 $0 backend` for a quick single-arch build).
PLATFORMS="${PLATFORMS:-}"
BUILD_TARGET="${1:-all}"

# Remote builder configuration
# Set USE_REMOTE_BUILDER=true to use remote ARM64 builder (much faster!)
# Set REMOTE_BUILDER_NAME to override the builder name
USE_REMOTE_BUILDER="${USE_REMOTE_BUILDER:-false}"
REMOTE_BUILDER_NAME="${REMOTE_BUILDER_NAME:-opentranscribe-multiarch}"
DEFAULT_BUILDER_NAME="opentranscribe-builder"

# BUILD_MODE=local  — build for the host arch only, --load into the local daemon,
#                     push NOTHING. This is what the release flow uses to produce
#                     images it can scan and run the release scenarios against
#                     BEFORE anything reaches Docker Hub.
# BUILD_MODE=push   — the historical behaviour: multi-arch, --push (default, so
#                     existing callers are unaffected).
#
# Until this existed, EVERY path through this script ended in `buildx --push`:
# there was no way to build a release candidate without publishing it, which
# meant :latest — what every existing user pulls — moved before any release test
# had run against it.
#
# --load cannot export a multi-arch manifest, so local mode is single-ARCH by
# necessity — but not single-arch-HOST. Set PLATFORMS to one platform to choose which
# leg local mode builds, or point DOCKER_CONTEXT at a native builder of that arch.
# See build_platforms() for why: "the other arch is validated after publish" was the
# standing answer here and it is what let a broken arm64 backend reach Docker Hub (#680).
BUILD_MODE="${BUILD_MODE:-push}"

# Publish the moving :latest tag alongside :vX.Y.Z. The release flow sets this to
# false and moves :latest afterwards with `buildx imagetools create`, which copies
# the manifest by digest — so :latest and :vX.Y.Z are provably the same bytes
# rather than two independent builds that happen to share a source tree.
PUSH_LATEST="${PUSH_LATEST:-true}"

# Print the buildx invocations and exit without building.
DRY_RUN="${DRY_RUN:-false}"

# Build identity baked into the images. The backend build context is ./backend,
# so the repo-root VERSION file is NOT in the image — these args are the only
# source a prod container has for its own version. See backend/app/core/version.py.
GIT_SHA_FULL="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Function to print colored output
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

# Function to check if Docker is running
check_docker() {
    if ! docker info > /dev/null 2>&1; then
        print_error "Docker is not running. Please start Docker and try again."
        exit 1
    fi
}

# Function to check if logged into Docker Hub
check_docker_login() {
    if ! docker info | grep -q "Username"; then
        print_warning "Not logged into Docker Hub. Attempting login..."
        docker login
    else
        print_success "Already logged into Docker Hub"
    fi
}

# Function to detect changes since last commit
detect_changes() {
    local component=$1

    if [ -z "$(git status --porcelain "${component}/")" ]; then
        print_info "No uncommitted changes in ${component}"
        # Check last commit
        if git diff --name-only HEAD~1 HEAD | grep -q "^${component}/"; then
            print_info "Changes detected in last commit for ${component}"
            return 0
        else
            print_warning "No recent changes in ${component}"
            return 1
        fi
    else
        print_info "Uncommitted changes detected in ${component}"
        return 0
    fi
}

# Function to update security scanning tool databases
update_security_tools() {
    print_info "Updating security scanning tool databases..."

    # Update Trivy vulnerability database
    if command -v trivy &> /dev/null; then
        print_info "Updating Trivy vulnerability database..."
        trivy image --download-db-only --quiet 2>/dev/null || true
        trivy image --download-java-db-only --quiet 2>/dev/null || true
        print_success "Trivy database updated"
    else
        print_warning "Trivy not found, skipping database update"
    fi

    # Update Grype vulnerability database
    if command -v grype &> /dev/null; then
        print_info "Updating Grype vulnerability database..."
        grype db update --quiet 2>/dev/null || true
        print_success "Grype database updated"
    else
        print_warning "Grype not found, skipping database update"
    fi

    # Update Syft (no database, but check for updates)
    if command -v syft &> /dev/null; then
        print_info "Syft is available (no database update needed)"
    else
        print_warning "Syft not found"
    fi

    # Hadolint and Dockle are rule-based, no database updates needed
    print_info "Hadolint and Dockle are rule-based (no database updates needed)"

    print_success "Security tool updates complete!"
}

# security-scan.sh's exit codes. Kept in sync with the block at the top of that
# script; 2 means "could not scan", which is NOT a kind of finding.
SCAN_EXIT_FINDINGS=1
SCAN_EXIT_COULD_NOT_SCAN=2

# Ask security-scan.sh what it can actually scan, and refuse anything else.
#
# Issue #681: the component list lived in three places that were free to
# disagree. `docs` was in BUILT_COMPONENTS and had a security-scan.sh arm but no
# registry-pull branch here, so in the default (registry) mode it was scanned
# against an image that was never fetched — and any resulting failure was then
# downgraded to a pass. A future `lite` target would have inherited exactly the
# same hole. So the list is DERIVED from the scanner, once, up front.
assert_components_scannable() {
    local requested=("$@")

    if [ ! -f "./scripts/security-scan.sh" ]; then
        print_error "scripts/security-scan.sh is missing — nothing can establish these images were scanned"
        return 1
    fi

    local known=()
    mapfile -t known < <(./scripts/security-scan.sh list-components 2>/dev/null)
    if [ ${#known[@]} -eq 0 ]; then
        print_error "security-scan.sh reported no scannable components"
        return 1
    fi

    local component candidate found rc=0
    for component in "${requested[@]}"; do
        found=false
        for candidate in "${known[@]}"; do
            if [ "${candidate}" = "${component}" ]; then
                found=true
                break
            fi
        done
        if [ "${found}" != true ]; then
            print_error "Component '${component}' has no security-scan.sh arm — it CANNOT be scanned"
            print_error "  scannable components: ${known[*]}"
            rc=1
        fi
    done
    return "${rc}"
}

# The COMPONENT_PLATFORMS/COMPONENT_CAPABILITY table's key set must exactly
# equal security-scan.sh's scannable-component list — the same drift #681
# guarded against (a component nobody scans) has a mirror-image failure here
# (a component nobody knows the platforms for). Checked eagerly, once, so a
# component added to one table and forgotten in the other is a loud failure
# rather than a component silently built with an empty platform list.
assert_platform_table_matches_scan_components() {
    if [ ! -f "./scripts/security-scan.sh" ]; then
        print_error "scripts/security-scan.sh is missing — cannot verify the platform table"
        return 1
    fi

    local scan_known=()
    mapfile -t scan_known < <(./scripts/security-scan.sh list-components 2>/dev/null | sort)
    local table_known=()
    mapfile -t table_known < <(printf '%s\n' "${!COMPONENT_PLATFORMS[@]}" | sort)

    if [ "${scan_known[*]}" != "${table_known[*]}" ]; then
        print_error "COMPONENT_PLATFORMS key set != security-scan.sh list-components"
        print_error "  security-scan.sh: ${scan_known[*]}"
        print_error "  docker-build-push.sh table: ${table_known[*]}"
        return 1
    fi
    return 0
}

# component<TAB>capability<TAB>platforms, one per line, sorted by component.
# The single home for "what does this script build, in what capability, for
# which architectures" — mirrors `list-components` in security-scan.sh.
list_platforms() {
    local component
    for component in $(printf '%s\n' "${!COMPONENT_PLATFORMS[@]}" | sort); do
        printf '%s\t%s\t%s\n' \
            "${component}" \
            "${COMPONENT_CAPABILITY[${component}]}" \
            "${COMPONENT_PLATFORMS[${component}]}"
    done
}

# The union of platforms that will actually be touched by a given BUILD_TARGET,
# used ONLY for the QEMU-emulation advisory messages (never for build dispatch
# itself — each build_* function asks build_platforms() for its OWN component).
effective_platforms_for_target() {
    local target="$1"
    local components=()
    case "${target}" in
        backend|lite|frontend|docs|blackwell) components=("${target}") ;;
        all|auto) components=(backend lite frontend docs) ;;
        *) return 0 ;;
    esac
    local component seen=""
    for component in "${components[@]}"; do
        seen="${seen},$(build_platforms "${component}")"
    done
    echo "${seen}"
}

# Function to run security scan if enabled.
#
# Returns 0 (proceed), 1 (findings the caller's policy refuses to tolerate), or
# SCAN_EXIT_COULD_NOT_SCAN (this component was never scanned).
run_security_scan() {
    local component=$1

    if [ "${SKIP_SECURITY_SCAN}" = "true" ]; then
        print_warning "Security scanning skipped (SKIP_SECURITY_SCAN=true)"
        return 0
    fi

    if [ ! -f "./scripts/security-scan.sh" ]; then
        # Deliberately fatal, and deliberately NOT gated on
        # FAIL_ON_SECURITY_ISSUES. There is already an explicit, named opt-out
        # for "I do not want a scan" — SKIP_SECURITY_SCAN — and an absent
        # scanner is not somebody choosing it.
        print_error "Security scan script not found — refusing to claim ${component} was scanned"
        print_error "Set SKIP_SECURITY_SCAN=true if you genuinely intend to build without scanning"
        return "${SCAN_EXIT_COULD_NOT_SCAN}"
    fi

    # Which TAG gets scanned has to match what this run actually produced, or the scan
    # examines a different artifact than the one just built.
    #
    # security-scan.sh defaults IMAGE_TAG to `latest`, and this call never overrode it —
    # so under BUILD_MODE=local, which tags `repo:vX.Y.Z` and deliberately creates no
    # `:latest`, the scanner looked for an image that does not exist and reported
    # COULD-NOT-SCAN for every component. Measured on the first lite build for #667:
    # "Image not found locally: …-backend-lite:latest → Attempting to pull from registry
    # → not found". That is the correct refusal (#681's 1-vs-2 split doing its job) over
    # the wrong target — the image WAS there, as :v0.5.0.
    #
    # Registry mode is deliberately left on `latest`: that branch above literally runs
    # `docker pull ${repo}:latest`, so scanning :vX.Y.Z there would examine an image it
    # never fetched — the same class of mistake in the opposite direction.
    #
    # scripts/release/50-scan.sh already passes IMAGE_TAG="$VERSION" explicitly, which is
    # why the release pipeline was unaffected and this stayed hidden.
    local scan_tag="latest"
    if [ "${SCAN_SOURCE:-registry}" = "local" ]; then
        scan_tag="${VERSION_FULL}"
    fi

    print_info "Running security scan for ${component} (tag: ${scan_tag})..."
    local scan_rc=0
    OUTPUT_DIR="./security-reports" FAIL_ON_CRITICAL="${FAIL_ON_CRITICAL:-false}" \
        IMAGE_TAG="${scan_tag}" \
        ./scripts/security-scan.sh "${component}" || scan_rc=$?

    if [ "${scan_rc}" -eq 0 ]; then
        print_success "Security scan passed for ${component}"
        return 0
    fi

    if [ "${scan_rc}" -ge "${SCAN_EXIT_COULD_NOT_SCAN}" ]; then
        # NOT the findings branch. FAIL_ON_SECURITY_ISSUES is a statement about
        # which findings are acceptable to ship; it says nothing about shipping
        # an image nobody looked at, so it does not apply here and must not be
        # consulted. Folding this into the branch below is the whole of #681.
        print_error "Security scan could NOT RUN for ${component} (exit ${scan_rc})"
        print_error "This is not a tolerable-findings result — the image was never scanned"
        print_error "FAIL_ON_SECURITY_ISSUES does not apply: it tolerates findings, not the absence of a scan"
        return "${SCAN_EXIT_COULD_NOT_SCAN}"
    fi

    print_warning "Security scan found issues for ${component}"
    if [ "${FAIL_ON_SECURITY_ISSUES}" = "true" ]; then
        print_error "Failing build due to security issues (FAIL_ON_SECURITY_ISSUES=true)"
        return "${SCAN_EXIT_FINDINGS}"
    fi
    print_warning "Continuing despite security issues (set FAIL_ON_SECURITY_ISSUES=true to fail)"
    return 0
}

# ---------------------------------------------------------------------------
# Build-mode helpers. One place decides platform / output / tag set, so the four
# build functions cannot drift apart on it.
# ---------------------------------------------------------------------------

# Platforms for this run, keyed by component. Local mode is SINGLE-arch —
# `--load` cannot export a multi-arch manifest into an image store — but it is no
# longer forced to the HOST arch: an explicit single-platform PLATFORMS override
# selects which one. PLATFORMS otherwise behaves as before (an override applied to
# every component); with it unset, each component's COMPONENT_PLATFORMS entry wins.
#
# Why local mode had to grow a cross-arch case (issue #667): with it host-only, the
# ONLY way to obtain an arm64 leg was `buildx --push`, i.e. the first time anyone
# could inspect an arm64 artifact was after it was already on Docker Hub under a
# moving tag. That is precisely how #680 happened — a CPU-only arm64 backend shipped
# under the CUDA repo's :latest and was found by users, not by the pipeline. The lite
# image is the first component whose arm64 leg is load-bearing rather than incidental,
# so "validated after publish" is not good enough for it.
#
# ⚠️ Cross-arch here means the RIGHT BUILDER, not QEMU-by-accident. Two ways to get a
# real arm64 leg, both --load, neither pushing anything:
#   DOCKER_CONTEXT=remote-arm64 BUILD_MODE=local ./scripts/docker-build-push.sh lite
#   PLATFORMS=linux/arm64 BUILD_MODE=local USE_REMOTE_BUILDER=true ./scripts/…  lite
# The first loads into the native arm64 daemon, so the image can actually be RUN there;
# the second loads an arm64 image into the local amd64 daemon, which can be inspected
# (`docker image inspect --format '{{.Architecture}}'`) but not executed. Prefer the
# first — a build that exits 0 proves less than you think. BuildKit does NOT reject a
# wrong-arch base: a --platform linux/arm64 build over an amd64-only base exits 0 with
# only an InvalidBaseImagePlatform *warning* and COPY --from still succeeds, shipping an
# amd64 ELF in an "arm64" image that dies at runtime with `exec format error`. Verify the
# binary, never the exit code.
build_platforms() {
    local component="$1"
    if [ "${BUILD_MODE}" = "local" ]; then
        # An explicit override picks the arch; otherwise follow the daemon we are
        # pointed at, which is what makes DOCKER_CONTEXT=remote-arm64 Just Work.
        if [ -n "${PLATFORMS}" ]; then
            echo "${PLATFORMS}"
        else
            docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || echo "linux/amd64"
        fi
    elif [ -n "${PLATFORMS}" ]; then
        echo "${PLATFORMS}"
    else
        echo "${COMPONENT_PLATFORMS[${component}]:-}"
    fi
}

# BUILD_MODE=local + a comma-separated PLATFORMS is a misuse, not a build to attempt:
# `docker buildx --load` cannot export a multi-arch manifest, so buildx would fail deep
# into the run after paying for the builds. Checked once, up front, in main() — NOT
# inside build_platforms(), which is called from `$(...)` where a `return 1` only exits
# the subshell and leaves the caller with an empty platform string.
assert_local_mode_is_single_platform() {
    [ "${BUILD_MODE}" = "local" ] || return 0
    [[ "${PLATFORMS}" == *,* ]] || return 0
    print_error "BUILD_MODE=local with PLATFORMS='${PLATFORMS}' — --load cannot export a multi-arch manifest."
    print_info  "  Name exactly ONE platform and run once per architecture, e.g.:"
    print_info  "    PLATFORMS=linux/amd64 BUILD_MODE=local $0 ${BUILD_TARGET}"
    print_info  "    PLATFORMS=linux/arm64 BUILD_MODE=local USE_REMOTE_BUILDER=true $0 ${BUILD_TARGET}"
    exit 2
}

# One platform per element, for components that build a leg per architecture.
build_platform_list() {
    local component="$1"
    build_platforms "${component}" | tr ',' '\n'
}

# --load (keep it here) vs --push (send it to Docker Hub).
build_output_flag() {
    if [ "${BUILD_MODE}" = "local" ]; then
        echo "--load"
    else
        echo "--push"
    fi
}

# Tag arguments for a repo with NO per-arch capability legs (frontend, docs):
# always :vX.Y.Z, plus :latest unless the caller is going to move :latest by
# digest afterwards. This is the historical single multi-platform-build/single-tag
# shape, unchanged by the #680 capability-tag grammar.
build_tag_args() {
    local repo="$1"
    local args=("--tag" "${repo}:${VERSION_FULL}")
    if [ "${PUSH_LATEST}" = "true" ]; then
        args+=("--tag" "${repo}:latest")
    fi
    printf '%s\n' "${args[@]}"
}

# The single-arch LEG tag for a capability-bearing component (backend, lite,
# blackwell): repo:vX.Y.Z-<cap>-<arch>. Never carries :latest — :latest only
# ever moves on the ASSEMBLED INDEX (assemble_capability_index below), so it is
# always a multi-platform manifest, never accidentally a single-arch one.
build_leg_tag() {
    local repo="$1" cap="$2" arch="$3"
    echo "${repo}:${VERSION_FULL}-${cap}-${arch}"
}

# Assemble the vX.Y.Z (and, unless PUSH_LATEST=false, :latest) index for a
# capability-bearing repo from the leg tags that were just pushed, using
# `buildx imagetools create` — a manifest-list copy by digest, so the index
# provably contains exactly those legs and nothing rebuilt or re-pushed.
# Never call this with legs from more than one repo/capability: mixing a CUDA
# backend leg into the lite index (or vice versa) is exactly the class of bug
# issue #680 exists to prevent.
assemble_capability_index() {
    local repo="$1"
    shift
    local legs=("$@")

    if [ "${BUILD_MODE}" = "local" ]; then
        print_info "local mode — skipping index assembly for ${repo} (nothing was pushed to assemble from)"
        return 0
    fi
    if [ ${#legs[@]} -eq 0 ]; then
        print_error "assemble_capability_index: no legs given for ${repo}"
        return 1
    fi

    print_info "Assembling ${repo}:${VERSION_FULL} from: ${legs[*]}"
    docker buildx imagetools create --tag "${repo}:${VERSION_FULL}" "${legs[@]}"

    if [ "${PUSH_LATEST}" = "true" ]; then
        print_info "Assembling ${repo}:latest from: ${legs[*]}"
        docker buildx imagetools create --tag "${repo}:latest" "${legs[@]}"
    fi
}

# OCI provenance labels. Safe on every image — labels need no ARG declaration,
# unlike build args, which warn when the Dockerfile does not declare them.
# These are what makes `docker inspect` able to answer "what is this image?";
# nothing set them before, which is why opentranscribe.sh's label-based version
# fallback always came back empty.
build_identity_labels() {
    printf '%s\n' \
        "--label" "org.opencontainers.image.version=${VERSION_FULL}" \
        "--label" "org.opencontainers.image.revision=${GIT_SHA_FULL}" \
        "--label" "org.opencontainers.image.created=${BUILD_TIME}"
}

# Build args for the BACKEND only. backend/Dockerfile.prod declares all three;
# they are the sole source of build identity inside the image, because the build
# context is ./backend and the repo-root VERSION file is therefore absent.
#
# Deliberately NOT applied to the frontend: frontend/Dockerfile.prod declares no
# ARGs at all, so the --build-arg APP_VERSION this script used to pass it was a
# silent no-op. The frontend takes its version from frontend/package.json at
# build time via vite's __APP_VERSION__ define.
build_backend_identity_args() {
    printf '%s\n' \
        "--build-arg" "APP_VERSION=${VERSION_FULL}" \
        "--build-arg" "GIT_SHA=${COMMIT_SHA}" \
        "--build-arg" "BUILD_TIME=${BUILD_TIME}"
}

# Announce what a build is about to do, and short-circuit under DRY_RUN.
# Returns 1 when the caller should skip the actual build.
build_announce() {
    local what="$1" component="$2"
    print_info "${what}: mode=${BUILD_MODE} platforms=$(build_platforms "${component}") version=${VERSION_FULL}"
    if [ "${BUILD_MODE}" = "local" ]; then
        print_info "  local mode — loading into the local daemon, pushing NOTHING"
    fi
    if [ "${DRY_RUN}" = "true" ]; then
        print_warning "  DRY_RUN=true — not building"
        return 1
    fi
    return 0
}

# Build one architecture LEG of a capability-bearing component (backend, lite,
# blackwell) and tag it repo:vX.Y.Z-<cap>-<arch>. Returns the leg tag on stdout
# so callers can collect legs for assemble_capability_index. `local` mode
# --loads instead of --pushes, so imagetools (which reads from the registry)
# has nothing to assemble from — callers must skip assembly in that mode,
# which assemble_capability_index already does. Because that leaves NO
# repo:vX.Y.Z tag in the local daemon, local mode ALSO tags the single build
# repo:vX.Y.Z directly — this is what the release flow's 40-build.sh baked-
# version check (`docker run repo:VERSION`) depends on; local mode is single-
# platform by construction (build_platforms() collapses to the host arch), so
# there is exactly one leg and tagging it twice is unambiguous.
build_one_leg() {
    local component="$1" repo="$2" dockerfile_dir="$3" dockerfile="$4" arch="$5"
    shift 5
    local extra_build_args=("$@")

    local cap="${COMPONENT_CAPABILITY[${component}]}"
    local leg_tag
    leg_tag="$(build_leg_tag "${repo}" "${cap}" "${arch#linux/}")"

    local extra_tags=()
    if [ "${BUILD_MODE}" = "local" ]; then
        extra_tags=("--tag" "${repo}:${VERSION_FULL}")
    fi

    local identity_args
    mapfile -t identity_args < <(build_identity_labels)

    (
        cd "${dockerfile_dir}"
        docker buildx build \
            --platform "${arch}" \
            --file "${dockerfile}" \
            "${identity_args[@]}" \
            "${extra_build_args[@]}" \
            --tag "${leg_tag}" \
            "${extra_tags[@]}" \
            ${CACHE_FLAG} \
            "$(build_output_flag)" \
            .
    ) >&2

    echo "${leg_tag}"
}

# Function to build and push Blackwell backend image.
# Uses Dockerfile.blackwell with SM_121+ compatibility patches for DGX Spark / GB10 /
# RTX 50-series. ⚠️ Blackwell is a GPU GENERATION (compute capability sm_120/sm_121),
# NOT a host architecture — B200/GB200 are x86_64, GB10/DGX-Spark and RTX 50-series
# laptops/desktops are the arm64/amd64 split respectively. This repo only ships the
# arm64 leg (DGX Spark); an amd64 Blackwell leg is out of scope here (see backend/
# scripts/blackwell_patches.py, which gates on compute capability, never on arch).
# Built ONLY on request — never by `all`/`auto` — and never scanned by that path either.
build_backend_blackwell() {
    local component="blackwell"
    build_announce "Building Blackwell backend image" "${component}" || return 0

    local platform
    platform="$(build_platforms "${component}")"

    local identity_args
    mapfile -t identity_args < <(build_backend_identity_args; build_identity_labels)

    cd backend

    docker buildx build \
        --platform "${platform}" \
        --file Dockerfile.blackwell \
        "${identity_args[@]}" \
        --tag "${REPO_BACKEND}:blackwell" \
        --tag "${REPO_BACKEND}:${VERSION_FULL}-blackwell-${platform#linux/}" \
        ${CACHE_FLAG} \
        "$(build_output_flag)" \
        .

    cd ..

    BUILT_COMPONENTS+=("${component}")
    print_success "Blackwell backend image built (${BUILD_MODE} mode)"
    print_info "Tags:"
    print_info "  - ${REPO_BACKEND}:blackwell"
    print_info "  - ${REPO_BACKEND}:${VERSION_FULL}-blackwell-${platform#linux/}"
}

# Function to build and push the full/CUDA backend (no scan - scan runs separately).
# v0.5.0 ships this as amd64-only (see backend/Dockerfile.prod); the cuda-arm64 leg
# is reserved in COMPONENT_PLATFORMS/COMPONENT_CAPABILITY but not built.
build_backend() {
    local component="backend"
    build_announce "Building backend image" "${component}" || return 0

    local identity_build_args
    mapfile -t identity_build_args < <(build_backend_identity_args)

    local legs=()
    local arch
    while IFS= read -r arch; do
        [ -n "${arch}" ] || continue
        legs+=("$(build_one_leg "${component}" "${REPO_BACKEND}" "backend" "Dockerfile.prod" "${arch}" "${identity_build_args[@]}")")
    done < <(build_platform_list "${component}")

    assemble_capability_index "${REPO_BACKEND}" "${legs[@]}"

    print_success "Backend image built (${BUILD_MODE} mode)"
    printf '%s\n' "${legs[@]}" | sed 's/^/  - /'
}

# Function to build and push the lite/CPU-only backend (no scan - scan runs separately).
build_backend_lite() {
    local component="lite"
    build_announce "Building lite backend image" "${component}" || return 0

    local identity_build_args
    mapfile -t identity_build_args < <(build_backend_identity_args)

    local legs=()
    local arch
    while IFS= read -r arch; do
        [ -n "${arch}" ] || continue
        legs+=("$(build_one_leg "${component}" "${REPO_BACKEND_LITE}" "backend" "Dockerfile.lite" "${arch}" "${identity_build_args[@]}")")
    done < <(build_platform_list "${component}")

    assemble_capability_index "${REPO_BACKEND_LITE}" "${legs[@]}"

    print_success "Lite backend image built (${BUILD_MODE} mode)"
    printf '%s\n' "${legs[@]}" | sed 's/^/  - /'
}

# Function to build and push frontend (no scan - scan runs separately)
build_frontend() {
    local component="frontend"
    build_announce "Building frontend image" "${component}" || return 0

    local tag_args identity_args
    mapfile -t tag_args < <(build_tag_args "${REPO_FRONTEND}")
    mapfile -t identity_args < <(build_identity_labels)

    cd frontend

    docker buildx build \
        --platform "$(build_platforms "${component}")" \
        --file Dockerfile.prod \
        "${identity_args[@]}" \
        "${tag_args[@]}" \
        ${CACHE_FLAG} \
        "$(build_output_flag)" \
        .

    cd ..

    print_success "Frontend image built (${BUILD_MODE} mode)"
    printf '%s\n' "${tag_args[@]}" | grep -v '^--tag$' | sed 's/^/  - /'
}

# Function to build and push docs (nginx:alpine + Docusaurus static build)
build_docs() {
    local component="docs"
    build_announce "Building docs image" "${component}" || return 0

    local tag_args identity_args
    mapfile -t tag_args < <(build_tag_args "${REPO_DOCS}")
    mapfile -t identity_args < <(build_identity_labels)

    cd docs-site

    # OT_VERSION drives the homepage version badge. docs-site/Dockerfile has
    # declared this ARG all along and this script never passed it, so every
    # published opentranscribe-docs image rendered an empty badge — the badge only
    # ever worked for local `opentr.sh` builds, which do pass it.
    #
    # DOCS_BASE_URL keeps internal links correct when proxied at /docs/.
    docker buildx build \
        --platform "$(build_platforms "${component}")" \
        --file Dockerfile \
        --build-arg DOCS_BASE_URL=/docs/ \
        --build-arg OT_VERSION="${VERSION_FULL}" \
        "${identity_args[@]}" \
        "${tag_args[@]}" \
        ${CACHE_FLAG} \
        "$(build_output_flag)" \
        .

    cd ..

    print_success "Docs image built (${BUILD_MODE} mode)"
    printf '%s\n' "${tag_args[@]}" | grep -v '^--tag$' | sed 's/^/  - /'
}

# Turn the per-component status files into ONE verdict.
#
# Three outcomes, because there are three things that can happen (issue #681):
#   0   every component was scanned and came back acceptable
#   1   every component was SCANNED; at least one failed the caller's policy
#   2   at least one component was NOT SCANNED — no status file at all (the
#       subshell died before recording anything), or a could-not-scan status
#
# Split out of run_parallel_scans so it can be exercised directly: the
# missing-status-file path is otherwise only reachable by killing a subshell
# mid-run, and a branch that cannot be tested is a branch that quietly rots.
evaluate_scan_statuses() {
    local status_dir="$1"
    shift

    local verdict=0 component status
    for component in "$@"; do
        if [ ! -f "${status_dir}/${component}.status" ]; then
            print_error "${component}: no scan status recorded — NOT SCANNED"
            verdict="${SCAN_EXIT_COULD_NOT_SCAN}"
            continue
        fi
        status=$(cat "${status_dir}/${component}.status")
        case "${status}" in
            0)
                ;;
            "${SCAN_EXIT_FINDINGS}")
                if [ "${verdict}" -eq 0 ]; then
                    verdict="${SCAN_EXIT_FINDINGS}"
                fi
                ;;
            *)
                print_error "${component}: security scan could not run (status '${status}') — NOT SCANNED"
                verdict="${SCAN_EXIT_COULD_NOT_SCAN}"
                ;;
        esac
    done
    return "${verdict}"
}

# Function to run parallel security scans on the built images
run_parallel_scans() {
    local components=("$@")

    if [ "${SKIP_SECURITY_SCAN}" = "true" ]; then
        print_warning "Security scanning skipped (SKIP_SECURITY_SCAN=true)"
        return 0
    fi

    # Fail before pulling or scanning anything: a component the scanner has no
    # arm for can never produce a meaningful result, so there is no point
    # spending a registry pull to find that out per-component later.
    if ! assert_components_scannable "${components[@]}"; then
        print_error "Refusing to report a security result for a component that cannot be scanned"
        return 1
    fi

    print_info ""
    print_info "=========================================="
    print_info "Running security scans in parallel..."
    print_info "=========================================="

    # Update security tool databases first
    update_security_tools

    # SCAN_SOURCE=registry — discard the local image and pull :latest from Docker
    #   Hub, then scan that. The historical behaviour. Only meaningful AFTER a
    #   push, and only correct because the push happened first.
    # SCAN_SOURCE=local    — scan the image that was just built, without touching
    #   the registry. Required for a pre-push gate: registry mode would `docker
    #   rmi` the candidate and pull the PREVIOUS release in its place, so the
    #   scan would pass or fail on the wrong bytes entirely.
    local scan_source="${SCAN_SOURCE:-registry}"
    if [ "${BUILD_MODE}" = "local" ]; then
        scan_source="local"
    fi

    local pids=()

    if [ "${scan_source}" = "local" ]; then
        print_info "Scanning locally built images (no registry pull)"
        return_early_if_missing() {
            local image="$1"
            if ! docker image inspect "$image" >/dev/null 2>&1; then
                print_error "Image not present locally: $image"
                print_error "Build it first (BUILD_MODE=local) or use SCAN_SOURCE=registry"
                return 1
            fi
        }
        # A `*)` arm is mandatory here: an unhandled component would otherwise
        # skip the existence check entirely and be "scanned" against whatever
        # local image happens to carry that tag.
        for component in "${components[@]}"; do
            case "$component" in
                backend)  return_early_if_missing "${REPO_BACKEND}:${VERSION_FULL}" || return 1 ;;
                lite)     return_early_if_missing "${REPO_BACKEND_LITE}:${VERSION_FULL}" || return 1 ;;
                frontend) return_early_if_missing "${REPO_FRONTEND}:${VERSION_FULL}" || return 1 ;;
                docs)     return_early_if_missing "${REPO_DOCS}:${VERSION_FULL}" || return 1 ;;
                *)
                    print_error "No local-image rule for component '${component}' — cannot confirm an image exists to scan"
                    return 1
                    ;;
            esac
        done
    else
        # Pull images in parallel.
        #
        # This was an `if backend / elif frontend` with no default, so `docs`
        # was never pulled and the scan below ran against whatever local image
        # carried that tag — or nothing (issue #681). A `case` with a failing
        # `*)` makes the next component that forgets a branch loud.
        # Registry mode: security-scan.sh does the pulling now, ONE PULL PER ARCHITECTURE,
        # verifying each one is the platform it asked for.
        #
        # This block used to `docker pull --platform linux/amd64 "${pull_repo}:latest"` for
        # every component. For a multi-arch repo that is not a pre-fetch, it is a decision:
        # it primes the local tag with the amd64 leg, and the scan that followed examined
        # that leg no matter which architecture it claimed to cover. `opentranscribe-backend-lite`
        # publishes amd64 AND arm64, and arm64 hosts run the lite image exclusively — so the
        # arm64 leg would have shipped having never been scanned once, with an amd64 report
        # beside it looking like coverage (issue #667).
        #
        # The component list is still validated here (a component with no rule must be loud,
        # #681) — only the fetching moved, to the one place that knows the platform set.
        print_info "Registry mode — security-scan.sh will pull and verify each architecture leg"

        for component in "${components[@]}"; do
            case "$component" in
                backend|lite|frontend|docs) ;;
                *)
                    print_error "No registry-pull rule for component '${component}' — it would be scanned against a stale or absent local image"
                    return 1
                    ;;
            esac
        done
    fi

    # Wait for all pulls (no-op in local mode — nothing was pulled)
    if [ ${#pids[@]} -gt 0 ]; then
        for pid in "${pids[@]}"; do
            wait "$pid"
        done
        print_success "Images pulled for scanning"
    fi

    # Run scans in parallel
    print_info "Starting parallel security scans..."

    # Create temp files for status tracking
    local status_dir
    status_dir=$(mktemp -d)

    for component in "${components[@]}"; do
        (
            print_info "[${component^}] Starting security scan..."
            # `|| rc=$?` rather than `if ...; then`: the exact return code is
            # what distinguishes "scanned, findings" from "never scanned", and
            # collapsing it to a boolean here is what threw that away.
            rc=0
            run_security_scan "$component" || rc=$?
            echo "${rc}" > "${status_dir}/${component}.status"
            if [ "${rc}" -eq 0 ]; then
                print_success "[${component^}] Security scan completed"
            elif [ "${rc}" -ge "${SCAN_EXIT_COULD_NOT_SCAN}" ]; then
                print_error "[${component^}] Security scan could NOT RUN"
            else
                print_warning "[${component^}] Security scan had issues"
            fi
        ) 2>&1 | sed "s/^/[${component^}] /" &
    done

    # Wait for all scans to complete
    wait

    local verdict=0
    evaluate_scan_statuses "${status_dir}" "${components[@]}" || verdict=$?

    # Cleanup
    rm -rf "${status_dir}"

    if [ "${verdict}" -eq 0 ]; then
        print_success "All security scans completed successfully!"
        return 0
    fi

    if [ "${verdict}" -ge "${SCAN_EXIT_COULD_NOT_SCAN}" ]; then
        # The summary must never be able to describe an unscanned component as a
        # success, and "carry on quietly" is a form of describing it as one:
        # main() would print "All builds completed successfully!" right after.
        print_error "One or more components were NOT SCANNED — refusing to report success"
        print_error "Nothing in this run establishes those images are clean"
        return 1
    fi

    # Every component was scanned; at least one failed the caller's policy.
    # A status of 1 is only ever written when FAIL_ON_SECURITY_ISSUES=true, so
    # reaching here means the operator explicitly asked for this to be fatal.
    print_error "Security findings were not tolerated (FAIL_ON_SECURITY_ISSUES=true)"
    return 1
}

# Function to scan only (no build, pull latest and scan)
scan_only() {
    print_info "Running security scan only (no build)..."

    # Derived, not hardcoded. This used to read `backend frontend`, which meant
    # `$0 scan` silently never looked at the docs image even though `$0 all`
    # builds and publishes it (issue #681).
    local components=()
    if [ -f "./scripts/security-scan.sh" ]; then
        mapfile -t components < <(./scripts/security-scan.sh list-components 2>/dev/null)
    fi
    if [ ${#components[@]} -eq 0 ]; then
        print_error "Could not determine the scannable component list from security-scan.sh"
        return 1
    fi

    # Reuse run_parallel_scans which handles DB updates, parallel pulls, and parallel scans
    run_parallel_scans "${components[@]}"
}

# Function to delete old partial version tags from Docker Hub
cleanup_old_tags() {
    print_info "Cleaning up old partial version tags from Docker Hub..."
    print_info "This will delete vX and vX.X style tags (keeping latest and vX.Y.Z)"

    # Get Docker Hub token
    print_info "Authenticating with Docker Hub..."
    local token
    token=$(curl -s -X POST "https://hub.docker.com/v2/users/login/" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"${DOCKERHUB_USERNAME}\",\"password\":\"$(docker-credential-desktop get <<< 'https://index.docker.io/v1/' 2>/dev/null | jq -r .Secret 2>/dev/null || echo '')\"}" \
        2>/dev/null | jq -r .token 2>/dev/null)

    if [ -z "$token" ] || [ "$token" = "null" ]; then
        print_warning "Could not get Docker Hub token automatically."
        print_info "Please delete tags manually via Docker Hub web interface:"
        print_info "  Backend: https://hub.docker.com/r/${DOCKERHUB_USERNAME}/opentranscribe-backend/tags"
        print_info "  Frontend: https://hub.docker.com/r/${DOCKERHUB_USERNAME}/opentranscribe-frontend/tags"
        print_info ""
        print_info "Tags to delete (partial versions):"
        print_info "  - v0, v0.1, v0.2 (and similar)"
        print_info "  - Any commit SHA tags (e.g., 14accb6)"
        return 1
    fi

    # Function to delete a tag
    delete_tag() {
        local repo=$1
        local tag=$2
        print_info "Deleting ${repo}:${tag}..."
        local response
        response=$(curl -s -X DELETE \
            "https://hub.docker.com/v2/repositories/${DOCKERHUB_USERNAME}/${repo}/tags/${tag}/" \
            -H "Authorization: Bearer ${token}" \
            -w "%{http_code}")
        if [ "$response" = "204" ]; then
            print_success "Deleted ${repo}:${tag}"
        else
            print_warning "Failed to delete ${repo}:${tag} (may not exist)"
        fi
    }

    # List of partial version tags to delete
    local partial_tags=("v0" "v0.1" "v0.2")

    # Also find and delete commit SHA tags (7-8 character hex strings)
    print_info "Fetching existing tags..."
    for repo in "opentranscribe-backend" "opentranscribe-frontend"; do
        print_info "Processing ${repo}..."

        # Get all tags
        local tags_json
        tags_json=$(curl -s "https://hub.docker.com/v2/repositories/${DOCKERHUB_USERNAME}/${repo}/tags/?page_size=100" \
            -H "Authorization: Bearer ${token}" 2>/dev/null)

        # Delete partial version tags
        for tag in "${partial_tags[@]}"; do
            if echo "$tags_json" | jq -r '.results[].name' 2>/dev/null | grep -q "^${tag}$"; then
                delete_tag "$repo" "$tag"
            fi
        done

        # Delete commit SHA tags (7-8 hex characters)
        local sha_tags
        sha_tags=$(echo "$tags_json" | jq -r '.results[].name' 2>/dev/null | grep -E '^[a-f0-9]{7,8}$' || true)
        for tag in $sha_tags; do
            delete_tag "$repo" "$tag"
        done
    done

    print_success "Cleanup complete!"
    print_info "Remaining tags should be: latest, vX.Y.Z versions"
}

# Function to show usage
show_usage() {
    cat << EOF
Usage: $0 [OPTION]

Build and push Docker images to Docker Hub

Options:
    backend        Build and push only the full/CUDA backend image (amd64-only for now)
    lite           Build and push only the lite/CPU backend image (amd64 + arm64)
    frontend       Build and push only frontend image
    docs           Build and push only docs image (nginx:alpine + Docusaurus static)
    blackwell      Build and push Blackwell backend image (arm64, GPU-generation-gated,
                   never included by all/auto — see build_backend_blackwell)
    all            Build and push backend, lite, frontend, and docs (default)
    auto           Auto-detect changes and build only changed components
    scan           Security scan only (pull latest images, scan, push reports)
    cleanup        Delete old partial version tags (vX, vX.X) from Docker Hub
    list-platforms Print component<TAB>capability<TAB>platforms and exit (no Docker needed)
    help           Show this help message

Tag grammar (issue #680): capability lives in the REPOSITORY (backend=CUDA,
backend-lite=CPU), restated in the tag as a leg — vX.Y.Z-<cap>-<arch> — with
vX.Y.Z itself assembled as a multi-arch INDEX from that repo's own legs only.
:latest is a digest-copy of the index, never an independent build.

Environment Variables:
    VERSION                   Semantic version (e.g., v1.2.3) - overrides VERSION file
    DOCKERHUB_USERNAME        Docker Hub username (default: davidamacey)
    PLATFORMS                 Explicit OVERRIDE of the per-component platform table
                               (see `$0 list-platforms`) — unset by default, so each
                               component builds only its declared platforms.
    USE_REMOTE_BUILDER        Use remote ARM64 builder for faster builds (default: false)
    REMOTE_BUILDER_NAME       Remote builder name (default: opentranscribe-multiarch)
    NO_CACHE                  Build without cache (default: false)
    SKIP_SECURITY_SCAN        Skip security scanning (default: false)
    FAIL_ON_SECURITY_ISSUES   Fail build if security issues found (default: false)
    FAIL_ON_CRITICAL          Fail scan if CRITICAL vulnerabilities found (default: false)

Examples:
    $0              # Build and push both images with security scanning
    $0 backend      # Build and push only backend
    $0 auto         # Auto-detect and build changed components
    $0 scan         # Security scan only (no build, pulls latest images)

    # Specify version (creates tags: latest, v1.2.3)
    VERSION=v1.2.3 $0 all

    # Version from VERSION file (recommended for releases)
    echo "v1.2.3" > VERSION
    $0 all

    # Use remote ARM64 builder for 10-20x faster builds
    USE_REMOTE_BUILDER=true $0 all

    # Build without cache (fresh build)
    NO_CACHE=true $0 frontend

    # Build only for current platform (faster)
    PLATFORMS=linux/amd64 $0 backend

    # Skip security scanning for faster builds
    SKIP_SECURITY_SCAN=true $0 all

    # Fail build if security issues found (recommended for CI)
    FAIL_ON_SECURITY_ISSUES=true FAIL_ON_CRITICAL=true $0 all

Versioning:
    The script supports semantic versioning via VERSION file or environment variable:
    - Creates tags: latest, vX.Y.Z (full version only)
    - Version can be specified as v1.2.3 or 1.2.3 (v prefix added automatically)
    - Environment variable VERSION overrides VERSION file
    - If neither exists, defaults to v0.0.0 with a warning
    - Use 'cleanup' command to remove old partial version tags (vX, vX.X)

Remote Builder Setup:
    For dramatically faster ARM64 builds, set up a remote builder:
    1. Run: ./scripts/setup-remote-builder.sh setup
    2. Follow the interactive prompts to configure your Mac Studio
    3. Use: USE_REMOTE_BUILDER=true $0

    This uses native ARM64 compilation instead of QEMU emulation (10-20x faster!)

Security Scanning:
    After building, images are automatically scanned with:
    - Hadolint: Dockerfile linting
    - Dockle: CIS best practices
    - Trivy: Vulnerability scanning
    - Grype: Additional vulnerability scanning
    - Syft: SBOM generation

    Reports are saved to ./security-reports/

EOF
}

# Main script
main() {
    # list-platforms needs no Docker, no version, and no login — it just prints
    # the table, mirroring `security-scan.sh list-components`.
    if [ "${BUILD_TARGET}" = "list-platforms" ]; then
        list_platforms
        exit 0
    fi

    if ! assert_platform_table_matches_scan_components; then
        print_error "Refusing to continue: the platform table and the scan-component list disagree"
        exit 1
    fi

    # Version management - read from VERSION file or environment variable
    if [ -n "${VERSION}" ]; then
        # Use VERSION from environment variable
        SEMVER="${VERSION}"
    elif [ -f "VERSION" ]; then
        # Read VERSION from file
        SEMVER=$(cat VERSION | tr -d '[:space:]')
    else
        # Default version if neither exists
        SEMVER="v0.0.0"
    fi

    # Validate semantic version format (vX.Y.Z or X.Y.Z)
    if [[ ! "${SEMVER}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        print_error "Invalid semantic version format: ${SEMVER}"
        print_error "Expected format: v1.2.3 or 1.2.3"
        exit 1
    fi

    # Ensure version starts with 'v'
    if [[ ! "${SEMVER}" =~ ^v ]]; then
        SEMVER="v${SEMVER}"
    fi

    # Parse version for tagging (only full version used now)
    VERSION_FULL="${SEMVER}"  # e.g., v1.2.3

    print_info "OpenTranscribe Docker Build & Push Script"
    print_info "=========================================="
    print_info "Version: ${VERSION_FULL}"
    print_info "Commit:  ${COMMIT_SHA}"
    print_info "Branch:  ${BRANCH}"
    print_info ""

    # Warn if using default version
    if [ "${SEMVER}" = "v0.0.0" ]; then
        print_warning "No VERSION file or VERSION env var found, using default: ${SEMVER}"
        print_warning "Create a VERSION file or set VERSION environment variable for production builds"
        print_info ""
    fi

    # Cache control - set NO_CACHE=true to force rebuild without cache
    CACHE_FLAG=""
    if [ "${NO_CACHE}" = "true" ]; then
        CACHE_FLAG="--no-cache"
        print_info "Building without cache (NO_CACHE=true)"
    fi

    # Check prerequisites
    assert_local_mode_is_single_platform
    check_docker
    check_docker_login

    # Select and configure builder based on USE_REMOTE_BUILDER setting
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
        docker buildx use "${REMOTE_BUILDER_NAME}"
        docker buildx inspect --bootstrap
    else
        # Use local builder with QEMU emulation (slower but works without setup)
        if ! docker buildx inspect "${DEFAULT_BUILDER_NAME}" > /dev/null 2>&1; then
            print_info "Creating local buildx builder (with QEMU emulation)..."
            docker buildx create --name "${DEFAULT_BUILDER_NAME}" --use
            docker buildx inspect --bootstrap
        else
            print_info "Using existing local buildx builder (with QEMU emulation)"
            docker buildx use "${DEFAULT_BUILDER_NAME}"
        fi
        if [[ "$(effective_platforms_for_target "${BUILD_TARGET}")" == *"arm64"* ]]; then
            print_warning "Building ARM64 with QEMU emulation (slow)"
            print_info "For faster builds, set up remote builder: ./scripts/setup-remote-builder.sh"
            print_info "Then use: USE_REMOTE_BUILDER=true $0"
        fi
    fi

    # Track which components were built for parallel scanning
    BUILT_COMPONENTS=()

    case "${BUILD_TARGET}" in
        backend)
            print_info "Building backend only..."
            build_backend
            BUILT_COMPONENTS+=("backend")
            ;;
        lite)
            print_info "Building lite backend only..."
            build_backend_lite
            BUILT_COMPONENTS+=("lite")
            ;;
        frontend)
            print_info "Building frontend only..."
            build_frontend
            BUILT_COMPONENTS+=("frontend")
            ;;
        docs)
            print_info "Building docs only..."
            build_docs
            BUILT_COMPONENTS+=("docs")
            ;;
        blackwell)
            # build_backend_blackwell appends to BUILT_COMPONENTS itself and is
            # NEVER reached from all/auto — built and scanned only on request.
            print_info "Building Blackwell backend only (arm64, GPU-generation-gated)..."
            build_backend_blackwell
            ;;
        all)
            print_info "Building backend, lite, frontend, and docs..."
            build_backend
            build_backend_lite
            build_frontend
            build_docs
            BUILT_COMPONENTS+=("backend" "lite" "frontend" "docs")
            ;;
        auto)
            print_info "Auto-detecting changes..."

            if detect_changes "backend"; then
                build_backend
                build_backend_lite
                BUILT_COMPONENTS+=("backend" "lite")
            fi

            if detect_changes "frontend"; then
                build_frontend
                BUILT_COMPONENTS+=("frontend")
            fi

            if detect_changes "docs-site"; then
                build_docs
                BUILT_COMPONENTS+=("docs")
            fi

            if [ ${#BUILT_COMPONENTS[@]} -eq 0 ]; then
                print_warning "No changes detected in backend, frontend, or docs-site"
                print_info "Use '$0 all' to force build all images"
                exit 0
            fi
            ;;
        scan)
            print_info "Security scan only mode..."
            scan_only

            # Opt-in, same reasoning as the main path: this git-commits and
            # pushes security-reports/ onto the current branch.
            if [ "${PUSH_SECURITY_REPORTS:-false}" = "true" ]; then
                print_info ""
                print_info "📋 Pushing security reports..."
                SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
                if [ -f "${SCRIPT_DIR}/push-security-reports.sh" ]; then
                    VERSION="${VERSION_FULL}" "${SCRIPT_DIR}/push-security-reports.sh" || {
                        print_warning "⚠️  Security reports push had issues (see above for details)"
                    }
                fi
            else
                print_info ""
                print_info "📋 Security reports left uncommitted (PUSH_SECURITY_REPORTS=true to commit+push)"
            fi
            exit 0
            ;;
        cleanup)
            print_info "Cleanup mode - removing old partial version tags..."
            cleanup_old_tags
            exit 0
            ;;
        help|--help|-h)
            show_usage
            exit 0
            ;;
        *)
            print_error "Invalid option: ${BUILD_TARGET}"
            show_usage
            exit 1
            ;;
    esac

    # Run parallel security scans on all built components
    if [ ${#BUILT_COMPONENTS[@]} -gt 0 ]; then
        run_parallel_scans "${BUILT_COMPONENTS[@]}"
    fi

    print_success "All builds completed successfully!"
    print_info ""

    # Report what actually happened. This summary used to say "Images pushed to
    # Docker Hub" and list :latest unconditionally, which in local mode (or with
    # PUSH_LATEST=false) described a push that never occurred.
    local _dest _tags
    if [ "${BUILD_MODE}" = "local" ]; then
        _dest="loaded into the LOCAL Docker daemon (nothing pushed)"
    else
        _dest="pushed to Docker Hub"
    fi
    print_info "Images ${_dest} with version ${VERSION_FULL}:"

    _tags() {
        local repo="$1"
        print_info "  - ${repo}:${VERSION_FULL}"
        [ "${PUSH_LATEST}" = "true" ] && print_info "  - ${repo}:latest"
        return 0
    }
    if [ "${BUILD_TARGET}" = "backend" ] || [ "${BUILD_TARGET}" = "all" ] || [ "${BUILD_TARGET}" = "auto" ]; then
        print_info "Backend:"
        _tags "${REPO_BACKEND}"
    fi
    if [ "${BUILD_TARGET}" = "lite" ] || [ "${BUILD_TARGET}" = "all" ] || [ "${BUILD_TARGET}" = "auto" ]; then
        print_info "Lite backend:"
        _tags "${REPO_BACKEND_LITE}"
    fi
    if [ "${BUILD_TARGET}" = "frontend" ] || [ "${BUILD_TARGET}" = "all" ] || [ "${BUILD_TARGET}" = "auto" ]; then
        print_info "Frontend:"
        _tags "${REPO_FRONTEND}"
    fi

    if [ "${BUILD_MODE}" != "local" ]; then
        print_info ""
        print_info "To pull:"
        print_info "  docker pull ${REPO_BACKEND}:${VERSION_FULL}   # Specific version"
        [ "${PUSH_LATEST}" = "true" ] && \
            print_info "  docker pull ${REPO_BACKEND}:latest      # Always latest"
    fi
    if [ "${PUSH_LATEST}" != "true" ] && [ "${BUILD_MODE}" != "local" ]; then
        print_info ""
        print_info "NOTE: :latest was NOT moved. Promote it by digest once validated:"
        print_info "  docker buildx imagetools create -t ${REPO_BACKEND}:latest ${REPO_BACKEND}:${VERSION_FULL}"
    fi

    # CRITICAL: Switch back to default builder to prevent interference with local dev builds
    print_info ""
    print_info "🔧 Switching back to default Docker builder..."
    docker buildx use default
    print_success "✅ Default builder restored. Local development builds will work normally."

    # Show build performance info if using emulation. Local mode builds host-arch
    # only, so QEMU is never involved there regardless of the effective platform set.
    if [ "${BUILD_MODE}" != "local" ] && [ "${USE_REMOTE_BUILDER}" = "false" ] && [[ "$(effective_platforms_for_target "${BUILD_TARGET}")" == *"arm64"* ]]; then
        print_info ""
        print_info "⚡ Performance Tip:"
        print_info "You used QEMU emulation for ARM64 builds (10-20x slower than native)"
        print_info "To speed up future builds, set up a remote ARM64 builder:"
        print_info "  1. Run: ./scripts/setup-remote-builder.sh setup"
        print_info "  2. Then: USE_REMOTE_BUILDER=true $0"
    fi

    # Commit and push security reports — OPT-IN.
    #
    # push-security-reports.sh performs a `git commit` + `git push` of
    # security-reports/ onto WHATEVER BRANCH IS CHECKED OUT. Running that
    # automatically from a build is surprising in normal use and actively unsafe
    # during a release: it races the release commit, can land scan output on a
    # release tag's branch, and mutates the working tree in the middle of a
    # multi-stage pipeline.
    #
    # The release flow invokes it explicitly at the end, on master, after tagging.
    if [ "${PUSH_SECURITY_REPORTS:-false}" != "true" ]; then
        print_info ""
        print_info "📋 Security reports left uncommitted (PUSH_SECURITY_REPORTS=true to commit+push)"
        return 0
    fi

    print_info ""
    print_info "📋 Pushing security reports..."

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "${SCRIPT_DIR}/push-security-reports.sh" ]; then
        VERSION="${VERSION_FULL}" "${SCRIPT_DIR}/push-security-reports.sh" || {
            print_warning "⚠️  Security reports push had issues (see above for details)"
            print_info "You can manually run: ./scripts/push-security-reports.sh"
        }
    else
        print_warning "push-security-reports.sh not found, skipping auto-push"
    fi
}

# Run main function — but only when EXECUTED, not when sourced.
#
# Sourcing is how scripts/tests/test-scan-not-a-pass.sh exercises
# run_security_scan / evaluate_scan_statuses / run_parallel_scans directly. The
# alternative — driving those branches through a real build — needs Docker Hub
# credentials and multi-gigabyte images, i.e. they would not be tested at all.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main
fi
