#!/bin/bash
# Move :latest to the validated digest. A COPY, never a rebuild.
#
# `buildx imagetools create` copies the manifest, so :latest and :vX.Y.Z resolve
# to the SAME digest. Rebuilding for :latest would produce a second artifact that
# nothing validated and that can differ from the one that was tested.
#
# Exit: 0 promoted · 1 failed or digests disagree

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
USER_NS="${DOCKERHUB_USERNAME:-davidamacey}"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
: "${VERSION:?90-promote.sh needs a version}"

digest_of() {
    docker buildx imagetools inspect "$1" --format '{{println .Manifest.Digest}}' 2>/dev/null | head -1
}

fail=0
for repo in backend frontend docs; do
    img="${USER_NS}/opentranscribe-${repo}"
    docker manifest inspect "${img}:${VERSION}" >/dev/null 2>&1 || {
        echo -e "${YELLOW}SKIP  ${repo}: no :${VERSION} published${NC}" >&2; continue; }

    echo -e "${YELLOW}promoting ${img}:${VERSION} -> :latest${NC}" >&2
    docker buildx imagetools create -t "${img}:latest" "${img}:${VERSION}" || { fail=1; continue; }

    src=$(digest_of "${img}:${VERSION}"); dst=$(digest_of "${img}:latest")
    if [[ -n "$src" && "$src" == "$dst" ]]; then
        echo -e "${GREEN}PASS  ${repo}: :latest and :${VERSION} are the same digest${NC}" >&2
    else
        echo -e "${RED}FAIL  ${repo}: digests differ (${src:0:19} vs ${dst:0:19})${NC}" >&2
        fail=1
    fi
done

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"promote","version":"%s","status":"%s","next":%s}\n' \
        "$VERSION" "$([[ $fail -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $fail -eq 0 ]] && echo '["finish"]' || echo '["investigate before publishing the GitHub release"]')"
fi
exit $fail
