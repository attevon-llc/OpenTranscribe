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

# Severities from release-criteria.yaml; outcomes from here. Bidirectional — see
# criteria-lib.sh. Exported because the consumer lives across a file boundary.
export STAGE_ID=tag
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

# Every gate below is a hard early exit, and that is deliberately preserved: this stage pushes
# a tag, so continuing past a failed check to collect a fuller criteria[] would be strictly
# worse. `fail_out` therefore emits the criteria recorded SO FAR and exits the ORIGINAL code.
#
# It must NOT call criteria_assert_all_checked: on an early exit the later criteria are
# genuinely unchecked, and the library exits 2 for that — which would turn "gate failed" (1)
# into "pipeline misuse" (2) and break the stable exit-code contract.
fail_out() {
    local rc="$1"
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"tag","version":"%s","status":"fail","criteria":[%s],"next":["fix the finding, then re-run: ./scripts/release.sh tag %s --yes"]}\n' \
            "$VERSION" "$(criteria_json)" "$VERSION"
    fi
    exit "$rc"
}

if [[ -n "$(git status --porcelain)" ]]; then
    record clean-worktree fail "uncommitted changes present" "git status --porcelain"
    echo -e "${RED}worktree is dirty — a tag must be reproducible${NC}" >&2
    fail_out 1
fi
record clean-worktree pass

if git rev-parse "$VERSION" >/dev/null 2>&1; then
    record tag-absent fail "$VERSION already exists" \
        "git tag -d $VERSION && git push origin :refs/tags/$VERSION   # if it was never released"
    echo -e "${RED}$VERSION already exists${NC}" >&2
    echo "  git tag -d $VERSION && git push origin :refs/tags/$VERSION   # if it was never released" >&2
    fail_out 1
fi
record tag-absent pass

if ! python3 scripts/release/check-version-consistency.py --mode pre-tag; then
    record version-consistency-pre-tag fail "check-version-consistency.py --mode pre-tag refused" \
        "python3 scripts/release/check-version-consistency.py --mode pre-tag"
    fail_out 1
fi
record version-consistency-pre-tag pass

semver="${VERSION#v}"
notes=$(awk -v v="$semver" '
    $0 ~ "^## \\["v"\\]" {p=1; next}
    p && /^## \[/ {exit}
    p {print}
' CHANGELOG.md | head -60)
if [[ -z "$notes" ]]; then
    record changelog-section fail "no ## [$semver] section in CHANGELOG.md" \
        "add a '## [$semver]' section — it becomes the tag's annotation body"
    echo -e "${RED}no CHANGELOG section for $semver${NC}" >&2
    fail_out 1
fi
record changelog-section pass

echo -e "${YELLOW}About to create and PUSH annotated tag $VERSION${NC}" >&2
git tag -a "$VERSION" -m "$VERSION

$(printf '%s' "$notes" | head -30)"
git push origin "$VERSION"

echo -e "${GREEN}tagged and pushed $VERSION${NC}" >&2

# Both halves of the contract: every criterion release-criteria.yaml declares for `tag` was
# recorded above. Only reachable on the success path, which is the only path where "all
# criteria were checked" is a meaningful claim.
criteria_assert_all_checked

[[ "$JSON_OUT" == "true" ]] && printf '{"stage":"tag","version":"%s","status":"pass","criteria":[%s],"next":["publish"]}\n' "$VERSION" "$(criteria_json)"
exit 0
