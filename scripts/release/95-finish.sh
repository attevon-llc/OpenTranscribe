#!/bin/bash
# Publish the GitHub Release. THE LAST STAGE, deliberately.
#
# The installer resolves "latest" from the GitHub Release, so this is the switch
# that points new users at the version. Publishing it before the images are up
# and promoted would hand people a version whose images do not exist — which is
# also why release-validate.yml does NOT create the release.
#
# Refuses until CI is green for the tag's SHA.
#
# Exit: 0 released · 1 refused or failed

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
# No USER_NS here any more: the repo namespace comes from security-scan.sh list-repos, which
# reads DOCKERHUB_USERNAME itself, so the two cannot resolve to different namespaces.
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
: "${VERSION:?95-finish.sh needs a version}"

command -v gh >/dev/null 2>&1 || { echo -e "${RED}gh CLI required${NC}" >&2; exit 1; }
git rev-parse "$VERSION" >/dev/null 2>&1 || { echo -e "${RED}$VERSION is not tagged${NC}" >&2; exit 1; }

# The images MUST exist before the release names the version.
#
# Derived from security-scan.sh's component table, never listed here — this loop used to read
# `for repo in backend frontend`, so a release could be published --latest with the lite and
# docs images entirely absent from Docker Hub. See scripts/release/published-repos.sh.
# shellcheck source=scripts/release/published-repos.sh
source "$SCRIPT_DIR/published-repos.sh"

# Capture into a variable FIRST, and propagate the exit. Feeding the function straight into
# the loop as `done < <(release_published_repos_or_die)` looks equivalent and is not: process
# substitution runs in a SUBSHELL, so the helper's `exit 3` would end that subshell only. The
# loop would then read zero lines, `missing` would stay empty, and the stage would PASS — the
# precise silent-zero-iteration failure the helper exists to prevent, reintroduced at its own
# call site. `$( )` is a subshell too, but its status lands in $? where `|| exit` can see it.
repos_tsv="$(release_published_repos_or_die)" || exit $?

missing=()
while IFS=$'\t' read -r component repo; do
    [[ -n "$repo" ]] || continue
    docker manifest inspect "${repo}:${VERSION}" >/dev/null 2>&1 \
        || missing+=("${component} (${repo}:${VERSION})")
done <<< "$repos_tsv"

if [[ ${#missing[@]} -gt 0 ]]; then
    echo -e "${RED}not on Docker Hub — publish before finishing:${NC}" >&2
    printf '  %s\n' "${missing[@]}" >&2
    exit 1
fi

# CI must be green for this tag.
if [[ "${SKIP_CI_CHECK:-false}" != "true" ]]; then
    sha=$(git rev-list -n 1 "$VERSION")
    concl=$(gh run list --workflow release-validate.yml --limit 20 \
              --json headSha,conclusion,status \
              --jq ".[] | select(.headSha==\"$sha\") | .conclusion" 2>/dev/null | head -1)
    if [[ "$concl" != "success" ]]; then
        echo -e "${RED}release-validate.yml is not green for $VERSION (${concl:-no run found})${NC}" >&2
        echo "  gh run list --workflow release-validate.yml" >&2
        echo "  SKIP_CI_CHECK=true to override, with a reason recorded in the ledger" >&2
        exit 1
    fi
    echo -e "${GREEN}CI green for $VERSION${NC}" >&2
fi

semver="${VERSION#v}"
notes=$(awk -v v="$semver" '
    $0 ~ "^## \\["v"\\]" {p=1; next}
    p && /^## \[/ {exit}
    p {print}
' CHANGELOG.md)
[[ -n "$notes" ]] || { echo -e "${RED}no CHANGELOG section for $semver${NC}" >&2; exit 1; }

# Release assets, when the build produced them.
assets=()
for f in dist/opentranscribe-offline-*.tar.gz dist/opentranscribe-windows-*.zip security-reports/*-sbom.json; do
    [[ -f "$f" ]] && assets+=("$f")
done

echo -e "${YELLOW}Creating the GitHub Release for $VERSION (published, not draft)${NC}" >&2
gh release create "$VERSION" \
    --title "$VERSION" \
    --latest \
    --notes "$notes" \
    "${assets[@]}" || exit 1

echo -e "${GREEN}released $VERSION${NC}" >&2
[[ ${#assets[@]} -gt 0 ]] && echo "  assets: ${assets[*]}" >&2

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"finish","version":"%s","status":"pass","assets":%d,"next":["verify-published"]}\n' \
        "$VERSION" "${#assets[@]}"
fi
exit 0
