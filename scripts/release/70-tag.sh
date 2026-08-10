#!/bin/bash
# Create and push the annotated release tag. FIRST STAGE THAT LEAVES THIS MACHINE.
#
# Before publish, deliberately: the tag fires release-validate.yml, so CI checks
# the metadata while the 13.8 GB multi-arch build runs. Annotated, never
# lightweight — the repo treats tags as release artifacts.
#
# Exit: 0 tagged · 1 refused by a gate · 4 aborted

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
: "${VERSION:?70-tag.sh needs a version}"

[[ -z "$(git status --porcelain)" ]] || { echo -e "${RED}worktree is dirty — a tag must be reproducible${NC}" >&2; exit 1; }

if git rev-parse "$VERSION" >/dev/null 2>&1; then
    echo -e "${RED}$VERSION already exists${NC}" >&2
    echo "  git tag -d $VERSION && git push origin :refs/tags/$VERSION   # if it was never released" >&2
    exit 1
fi

python3 scripts/release/check-version-consistency.py --mode pre-tag || exit 1

semver="${VERSION#v}"
notes=$(awk -v v="$semver" '
    $0 ~ "^## \\["v"\\]" {p=1; next}
    p && /^## \[/ {exit}
    p {print}
' CHANGELOG.md | head -60)
[[ -n "$notes" ]] || { echo -e "${RED}no CHANGELOG section for $semver${NC}" >&2; exit 1; }

echo -e "${YELLOW}About to create and PUSH annotated tag $VERSION${NC}" >&2
git tag -a "$VERSION" -m "$VERSION

$(printf '%s' "$notes" | head -30)"
git push origin "$VERSION"

echo -e "${GREEN}tagged and pushed $VERSION${NC}" >&2
[[ "$JSON_OUT" == "true" ]] && printf '{"stage":"tag","version":"%s","status":"pass","next":["publish"]}\n' "$VERSION"
exit 0
