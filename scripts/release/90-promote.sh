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
# No USER_NS here any more: the repo namespace comes from security-scan.sh list-repos, which
# reads DOCKERHUB_USERNAME itself, so the two cannot resolve to different namespaces.
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
: "${VERSION:?90-promote.sh needs a version}"

digest_of() {
    docker buildx imagetools inspect "$1" --format '{{println .Manifest.Digest}}' 2>/dev/null | head -1
}

# Derived from security-scan.sh's component table, never listed here. This loop used to be a
# literal `for repo in backend backend-lite frontend docs`, which disagreed with 95-finish.sh's
# literal `backend frontend` — see scripts/release/published-repos.sh.
# shellcheck source=scripts/release/published-repos.sh
source "$SCRIPT_DIR/published-repos.sh"

# Captured into a variable, NOT piped in via `< <(...)` — see the note in 95-finish.sh: the
# helper's `exit 3` inside a process substitution would end only that subshell, leaving this
# loop to iterate zero times and report success.
repos_tsv="$(release_published_repos_or_die)" || exit $?

fail=0
while IFS=$'\t' read -r component img; do
    [[ -n "$img" ]] || continue
    repo="${img##*/}"
    # A missing :$VERSION is a FAILURE, not a SKIP. Promote's whole job is moving :latest onto
    # the validated digest; a repo with nothing to promote means the publish stage did not do
    # what the pipeline believes it did, and skipping it silently let :latest keep pointing at
    # the PREVIOUS release for that component while the release completed green. That is how an
    # unpublished lite image could ride all the way through to `finish` unnoticed.
    docker manifest inspect "${img}:${VERSION}" >/dev/null 2>&1 || {
        echo -e "${RED}FAIL  ${component}: no ${img}:${VERSION} published — cannot promote${NC}" >&2
        fail=1; continue; }

    echo -e "${YELLOW}promoting ${img}:${VERSION} -> :latest${NC}" >&2
    docker buildx imagetools create -t "${img}:latest" "${img}:${VERSION}" || { fail=1; continue; }

    src=$(digest_of "${img}:${VERSION}"); dst=$(digest_of "${img}:latest")
    if [[ -n "$src" && "$src" == "$dst" ]]; then
        echo -e "${GREEN}PASS  ${repo}: :latest and :${VERSION} are the same digest${NC}" >&2
    else
        echo -e "${RED}FAIL  ${repo}: digests differ (${src:0:19} vs ${dst:0:19})${NC}" >&2
        fail=1
    fi
done <<< "$repos_tsv"

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"promote","version":"%s","status":"%s","next":%s}\n' \
        "$VERSION" "$([[ $fail -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $fail -eq 0 ]] && echo '["finish"]' || echo '["investigate before publishing the GitHub release"]')"
fi
exit $fail
