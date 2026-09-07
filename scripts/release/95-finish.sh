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

# Severities from release-criteria.yaml; outcomes from here. Bidirectional — see
# criteria-lib.sh. Exported because the consumer lives across a file boundary.
export STAGE_ID=finish
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

# This stage publishes the GitHub Release, so every gate below stays a hard early exit rather
# than accumulating: continuing past a failed check to collect a fuller criteria[] would mean
# running closer to the irreversible step. `fail_out` emits what was recorded SO FAR and exits
# the ORIGINAL code. It deliberately does not call criteria_assert_all_checked — on an early
# exit the later criteria really are unchecked, and the library exits 2 for that, which would
# turn a gate failure (1) into a pipeline-misuse code.
fail_out() {
    local rc="$1"; shift
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"finish","version":"%s","status":"fail","criteria":[%s],"next":[%s]}\n' \
            "$VERSION" "$(criteria_json)" "${1:-\"read the finding above\"}"
    fi
    exit "$rc"
}

if ! command -v gh >/dev/null 2>&1; then
    record gh-cli-available fail "gh is not installed" "https://cli.github.com/"
    echo -e "${RED}gh CLI required${NC}" >&2
    fail_out 1 '"install the gh CLI"'
fi
record gh-cli-available pass

if ! git rev-parse "$VERSION" >/dev/null 2>&1; then
    record tag-exists fail "$VERSION is not tagged in this repo" \
        "./scripts/release.sh tag $VERSION --yes"
    echo -e "${RED}$VERSION is not tagged${NC}" >&2
    fail_out 1 "\"./scripts/release.sh tag $VERSION --yes\""
fi
record tag-exists pass

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
list_rc=0
repos_tsv="$(release_published_repos_or_die)" || list_rc=$?
if (( list_rc != 0 )); then
    record all-images-published not-measured \
        "security-scan.sh list-repos yielded nothing (rc=$list_rc)" \
        "./scripts/security-scan.sh list-repos"
    fail_out "$list_rc" '"./scripts/security-scan.sh list-repos"'
fi

missing=()
repos_seen=0
while IFS=$'\t' read -r component repo; do
    [[ -n "$repo" ]] || continue
    repos_seen=$((repos_seen + 1))
    docker manifest inspect "${repo}:${VERSION}" >/dev/null 2>&1 \
        || missing+=("${component} (${repo}:${VERSION})")
done <<< "$repos_tsv"

if (( repos_seen == 0 )); then
    # Non-empty TSV made entirely of blank lines. COULD NOT CHECK, never "all published".
    record all-images-published not-measured "no repo lines to check"
    echo -e "${RED}derived zero repos to check — refusing to publish a release${NC}" >&2
    fail_out 1 '"./scripts/security-scan.sh list-repos"'
fi
if [[ ${#missing[@]} -gt 0 ]]; then
    record all-images-published fail "absent from Docker Hub: ${missing[*]}" \
        "./scripts/release.sh publish $VERSION --yes"
    echo -e "${RED}not on Docker Hub — publish before finishing:${NC}" >&2
    printf '  %s\n' "${missing[@]}" >&2
    fail_out 1 "\"./scripts/release.sh publish $VERSION --yes\""
fi
record all-images-published pass

# CI must be green for this tag.
if [[ "${SKIP_CI_CHECK:-false}" != "true" ]]; then
    sha=$(git rev-list -n 1 "$VERSION")
    concl=$(gh run list --workflow release-validate.yml --limit 20 \
              --json headSha,conclusion,status \
              --jq ".[] | select(.headSha==\"$sha\") | .conclusion" 2>/dev/null | head -1)
    if [[ "$concl" != "success" ]]; then
        record ci-green-for-tag fail "release-validate.yml conclusion: ${concl:-no run found}" \
            "gh run list --workflow release-validate.yml"
        echo -e "${RED}release-validate.yml is not green for $VERSION (${concl:-no run found})${NC}" >&2
        echo "  gh run list --workflow release-validate.yml" >&2
        echo "  SKIP_CI_CHECK=true to override, with a reason recorded in the ledger" >&2
        fail_out 1 '"gh run list --workflow release-validate.yml"'
    fi
    record ci-green-for-tag pass
    echo -e "${GREEN}CI green for $VERSION${NC}" >&2
else
    # NOT a pass. SKIP_CI_CHECK is an operator opt-out, and recording it as a pass would put
    # "CI was green" in the ledger for a release where CI was never consulted — the same class
    # of drift as reporting could-not-scan as no-findings (issue #681).
    record ci-green-for-tag not-measured "SKIP_CI_CHECK=true — operator waived the CI gate" \
        "gh run list --workflow release-validate.yml"
    echo -e "${YELLOW}SKIP_CI_CHECK=true — the CI gate was WAIVED, not satisfied${NC}" >&2
fi

semver="${VERSION#v}"
notes=$(awk -v v="$semver" '
    $0 ~ "^## \\["v"\\]" {p=1; next}
    p && /^## \[/ {exit}
    p {print}
' CHANGELOG.md)
if [[ -z "$notes" ]]; then
    record changelog-section fail "no ## [$semver] section in CHANGELOG.md" \
        "add a '## [$semver]' section — it becomes the release notes"
    echo -e "${RED}no CHANGELOG section for $semver${NC}" >&2
    fail_out 1 '"add the CHANGELOG section for this version"'
fi
record changelog-section pass

# --- BEGIN release-assets (issue #781) ---
# Release assets: an SBOM per published leg, each with a published checksum. Derived and
# gated, never a bare existence glob (issue #781) — see release-assets.sh's header for the
# full history of why "attach whatever is there" shipped releases with zero, or three STALE,
# SBOMs and nobody noticed either way.
#
# These BEGIN/END markers bound the block backend/tests/unit/test_release_finish_assets.py
# extracts to drive against a scratch scan dir — see that file's header for why extraction
# beats running the whole stage (gh-cli/tag/Docker-Hub/CI preconditions this block does not
# need). Keep the block self-contained between the markers: it may assume $VERSION,
# $SCRIPT_DIR, $repos_tsv and the record/fail_out contract are already in scope, and must not
# spill state the markers don't capture.
# shellcheck source=scripts/release/release-assets.sh
source "$SCRIPT_DIR/release-assets.sh"

scan_dir="$(release_assets_resolve_scan_dir "$VERSION")"

expected_sboms_output="$(release_assets_expected_sboms "$repos_tsv")"
expected_sboms_rc=$?
mapfile -t expected_sboms <<< "$expected_sboms_output"
if [[ ${#expected_sboms[@]} -eq 1 && -z "${expected_sboms[0]}" ]]; then
    expected_sboms=()
fi

if (( expected_sboms_rc != 0 )) || (( ${#expected_sboms[@]} == 0 )); then
    # Could not derive the expected set at all — COULD NOT CHECK, never "nothing to ship".
    # Same rule 50-scan.sh's own legs_expected==0 branch applies to the identical question.
    record sbom-per-published-leg not-measured \
        "could not derive the expected SBOM set from docker-build-push.sh list-platforms" \
        "./scripts/docker-build-push.sh list-platforms"
    record sbom-describes-this-version not-measured "no legs to check"
    record asset-checksums not-measured "no assets derived"
    echo -e "${RED}derived zero expected SBOMs — refusing to publish a release with no evidence${NC}" >&2
    fail_out 1 '"./scripts/docker-build-push.sh list-platforms"'
fi

missing_sboms=()
present_sboms=()
for name in "${expected_sboms[@]}"; do
    sbom_path="${scan_dir}/${name}"
    if [[ -f "$sbom_path" ]]; then
        present_sboms+=("$sbom_path")
    else
        missing_sboms+=("$name")
    fi
done

if (( ${#missing_sboms[@]} > 0 )); then
    record sbom-per-published-leg fail \
        "missing in ${scan_dir}: ${missing_sboms[*]}" \
        "./scripts/release.sh scan $VERSION"
    echo -e "${RED}missing SBOM(s) in ${scan_dir}: ${missing_sboms[*]}${NC}" >&2
    fail_out 1 "\"./scripts/release.sh scan $VERSION\""
fi
record sbom-per-published-leg pass

wrong_version_sboms=()
for sbom_path in "${present_sboms[@]}"; do
    release_assets_sbom_matches_version "$sbom_path" "$VERSION" \
        || wrong_version_sboms+=("$(basename "$sbom_path")")
done

if (( ${#wrong_version_sboms[@]} > 0 )); then
    # Presence is not evidence (P6): an unreadable, corrupt, or stale-version SBOM sitting in
    # scan_dir must fail exactly like a missing one — never pass because a file happened to
    # exist there.
    record sbom-describes-this-version fail \
        "describing another version (or unreadable): ${wrong_version_sboms[*]}" \
        "./scripts/release.sh scan $VERSION"
    echo -e "${RED}SBOM(s) do not describe ${VERSION}: ${wrong_version_sboms[*]}${NC}" >&2
    fail_out 1 "\"./scripts/release.sh scan $VERSION\""
fi
record sbom-describes-this-version pass

# NOT `$(...)`-captured: release_assets_checksum_dir sets RELEASE_ASSETS_CHECKSUM_DIR itself
# rather than printing it, because its EXIT trap must govern THIS script's exit, not a
# command-substitution subshell's (see the function's own header for the reasoning).
release_assets_checksum_dir "${present_sboms[@]}"
checksum_dir="$RELEASE_ASSETS_CHECKSUM_DIR"
checksum_assets=()
for sbom_path in "${present_sboms[@]}"; do
    sidecar="${checksum_dir}/$(basename "$sbom_path").sha256"
    if [[ ! -f "$sidecar" ]]; then
        record asset-checksums fail "no checksum produced for $(basename "$sbom_path")"
        echo -e "${RED}checksum generation failed for $(basename "$sbom_path")${NC}" >&2
        fail_out 1
    fi
    checksum_assets+=("$sidecar")
done
record asset-checksums pass

# The offline/Windows packages are a standing, explicit project decision (releasing.md), not
# a run-time operator opt-out: they are developer-built and never distributed via the GitHub
# Release, so there is nothing here to attach and nothing to fail. `waived` (rather than a
# bare `not-measured`) is belt-and-braces, matching the `warn` severity already declared for
# both in release-criteria.yaml.
record offline-package-attached not-measured \
    "offline package is developer-built and not distributed via the GitHub Release (releasing.md)" \
    "" waived
record windows-package-attached not-measured \
    "Windows installer needs Inno Setup on Windows and is developer-built, not distributed via the GitHub Release" \
    "" waived

assets=("${present_sboms[@]}" "${checksum_assets[@]}")
[[ -f "${checksum_dir}/SHA256SUMS" ]] && assets+=("${checksum_dir}/SHA256SUMS")
# --- END release-assets ---

echo -e "${YELLOW}Creating the GitHub Release for $VERSION (published, not draft)${NC}" >&2
if ! gh release create "$VERSION" \
    --title "$VERSION" \
    --latest \
    --notes "$notes" \
    "${assets[@]}"; then
    record github-release-created fail "gh release create failed" \
        "gh release view $VERSION   # it may exist already"
    fail_out 1 "\"gh release view $VERSION\""
fi
record github-release-created pass

echo -e "${GREEN}released $VERSION${NC}" >&2
[[ ${#assets[@]} -gt 0 ]] && echo "  assets: ${assets[*]}" >&2

# Both halves of the contract, on the only path where every criterion was reached.
criteria_assert_all_checked

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"finish","version":"%s","status":"pass","assets":%d,"criteria":[%s],"next":["verify-published"]}\n' \
        "$VERSION" "${#assets[@]}" "$(criteria_json)"
fi
exit 0
