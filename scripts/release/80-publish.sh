#!/bin/bash
# Multi-arch build and push of :vX.Y.Z ONLY. Never :latest.
#
# :latest moves later, by digest copy (90-promote.sh), so :latest and :vX.Y.Z are
# provably the same bytes rather than two builds that happen to share a tree.
#
# ARM64 goes to the remote builder: the backend image is ~13.8 GB and QEMU
# emulation turns 20 minutes into 2-3 hours.
#
# Exit: 0 published · 1 failed · 3 builder unreachable

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
BUILDER="${REMOTE_BUILDER_NAME:-opentranscribe-multiarch}"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
: "${VERSION:?80-publish.sh needs a version}"

if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    echo -e "${RED}buildx builder '$BUILDER' missing — cannot publish multi-arch${NC}" >&2
    echo "  ./scripts/setup-remote-builder.sh setup" >&2
    exit 3
fi
if docker buildx inspect "$BUILDER" 2>/dev/null | grep -qi 'error'; then
    echo -e "${RED}'$BUILDER' has a node in error (the remote host moved?)${NC}" >&2
    echo "  ./scripts/setup-remote-builder.sh --host user@<current-ip>" >&2
    exit 3
fi

echo -e "${YELLOW}PUBLISHING ${VERSION} to Docker Hub (:${VERSION} only, not :latest)${NC}" >&2
rc=0
USE_REMOTE_BUILDER=true BUILD_MODE=push PUSH_LATEST=false VERSION="$VERSION" \
    ./scripts/docker-build-push.sh all || rc=$?
[[ $rc -eq 0 ]] || { echo -e "${RED}publish failed${NC}" >&2; exit 1; }

# Both architectures must actually be in the manifest.
missing=()
for repo in backend frontend; do
    img="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-${repo}:${VERSION}"
    for arch in amd64 arm64; do
        docker manifest inspect "$img" 2>/dev/null | grep -q "\"architecture\": \"$arch\"" \
            || missing+=("$repo/$arch")
    done
done
if [[ ${#missing[@]} -gt 0 ]]; then
    echo -e "${RED}published manifest is missing: ${missing[*]}${NC}" >&2
    exit 1
fi

echo -e "${GREEN}published ${VERSION} (amd64 + arm64)${NC}" >&2
[[ "$JSON_OUT" == "true" ]] && printf '{"stage":"publish","version":"%s","status":"pass","next":["smoke"]}\n' "$VERSION"
exit 0
