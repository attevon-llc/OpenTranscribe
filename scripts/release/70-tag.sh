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
# ⚠️ Both of these were UNCHECKED (issue #784 finding N4) — this file is
# `set -uo pipefail` with no `-e`, so a failed `git tag`/`git push` fell
# through to `record ... pass` and `exit 0`. Adding the branch cut below on top
# of that unchecked pair would have doubled the hole, so it is closed first.
if ! git tag -a "$VERSION" -m "$VERSION

$(printf '%s' "$notes" | head -30)"; then
    record tag-pushed fail "git tag -a failed" "git tag -d $VERSION   # then investigate"
    record release-branch-tracks-tag not-measured "no tag to branch from"
    fail_out 1
fi
if ! git push origin "$VERSION"; then
    record tag-pushed fail "git push origin $VERSION failed" "check credentials, then retry"
    record release-branch-tracks-tag not-measured "the tag never reached origin"
    fail_out 1
fi
record tag-pushed pass

echo -e "${GREEN}tagged and pushed $VERSION${NC}" >&2

# Cut (or, on the patch path, confirm) release/<major>.<minor> FROM THE TAG WE
# JUST PUSHED — never from master (issue #784). This is the branch every future
# hotfix for this minor cherry-picks onto, so "cut from somewhere else" must be
# caught here, at tag time, not discovered by the first hotfix operator.
#
# Derived, never spelled: v0.5.1 -> release/0.5. `semver` was already computed
# above for the CHANGELOG lookup.
RELEASE_BRANCH="release/${semver%.*}"

if git ls-remote --exit-code --heads origin "$RELEASE_BRANCH" >/dev/null 2>&1; then
    # Idempotent: on the patch path the branch already exists. Skip creation
    # and assert the STRONGER property below instead of re-creating it.
    echo -e "${YELLOW}${RELEASE_BRANCH} already exists on origin — this is a patch, not the first cut${NC}" >&2
else
    echo -e "${YELLOW}cutting ${RELEASE_BRANCH} from ${VERSION}${NC}" >&2
    # Push the tag's own COMMIT straight to a new branch ref on origin. No
    # local branch object is created or needed — this cannot collide with a
    # stale local branch left over from an earlier rehearsal.
    #
    # `^{}` peels the annotated tag object down to the commit it points at.
    # Without it, refs/tags/$VERSION names the TAG OBJECT (this pipeline tags
    # with `git tag -a`, never `-a`-less), and a branch ref must point at a
    # commit — the daemon refuses with "trying to write non-commit object ...
    # to branch", so an unpeeled push would fail every single first cut.
    if ! git push origin "refs/tags/${VERSION}^{}:refs/heads/${RELEASE_BRANCH}"; then
        record release-branch-tracks-tag fail \
            "git push origin refs/tags/${VERSION}^{}:refs/heads/${RELEASE_BRANCH} failed" \
            "git push origin refs/tags/${VERSION}^{}:refs/heads/${RELEASE_BRANCH}"
        fail_out 1
    fi
fi

# Assert the PROPERTY, not the action: ask ORIGIN — what a hotfix operator will
# actually clone — never the local ref. A tag accidentally cut from somewhere
# other than this branch (e.g. master) fails here, which is the guard #784
# asks for.
git fetch --quiet origin "$RELEASE_BRANCH"
if git merge-base --is-ancestor "$VERSION" "origin/${RELEASE_BRANCH}"; then
    record release-branch-tracks-tag pass "$VERSION is an ancestor of origin/${RELEASE_BRANCH}"
else
    record release-branch-tracks-tag fail \
        "$VERSION is NOT an ancestor of origin/${RELEASE_BRANCH} — this tag was cut from somewhere else" \
        "git log --oneline origin/${RELEASE_BRANCH}..${VERSION}"
    fail_out 1
fi

# Both halves of the contract: every criterion release-criteria.yaml declares for `tag` was
# recorded above. Only reachable on the success path, which is the only path where "all
# criteria were checked" is a meaningful claim.
criteria_assert_all_checked

[[ "$JSON_OUT" == "true" ]] && printf '{"stage":"tag","version":"%s","status":"pass","criteria":[%s],"next":["publish"]}\n' "$VERSION" "$(criteria_json)"
exit 0
