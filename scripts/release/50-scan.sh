#!/bin/bash
# Scan the LOCALLY BUILT images. Never the registry.
#
# SCAN_SOURCE=local matters: run_parallel_scans' default path does `docker rmi`
# then `docker pull` of :latest, i.e. it scans the PREVIOUS release. That is
# correct only after a push and actively wrong as a pre-push gate.
#
# Exit: 0 clean · 1 blocking findings OR could-not-scan
#
# security-scan.sh distinguishes those two (1 = scanned with findings, 2 = never
# scanned; issue #681). This stage deliberately maps both onto 1, because the
# RELEASE runner's exit codes mean something else entirely — 2 there is "misuse"
# and 3 is "precondition unmet". Both scan outcomes are gate failures, so they
# get the gate-failure code, with the distinction carried in the message.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
OUT_DIR="${OT_SCAN_DIR:-./security-reports}"
BLUE='\033[0;34m'; NC='\033[0m'

: "${VERSION:?50-scan.sh needs a version}"

RED='\033[0;31m'; GREEN='\033[0;32m'

# Severities from release-criteria.yaml; outcomes from here. Bidirectional — see
# criteria-lib.sh. Exit 2 from the library means the pipeline's own wiring disagrees with
# itself, which is distinct from (and does not disturb) this stage's 0/1/3 gate contract.
# Exported: the consumer of this variable now lives in criteria-lib.sh, across a file
# boundary, so a bare assignment would be invisible to both shellcheck and a reader.
export STAGE_ID=scan
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

# The images must exist locally at THIS tag before we scan.
#
# security-scan.sh reads IMAGE_TAG and defaults it to `latest`. This stage used
# to export VERSION= instead -- a name that script never reads -- so every scan
# silently fell back to :latest, i.e. the PREVIOUS release. The v0.5.0 gate was
# reporting on the v0.4.1 image, built four months earlier, and its 35 CRITICALs
# were v0.4.1's.
#
# So the tag is asserted rather than assumed: a missing image fails here instead
# of quietly redirecting the scan to whatever :latest happens to point at.
#
# Components/repos are DERIVED from security-scan.sh's own list-repos command
# (issue #680), not hardcoded — `backend frontend docs` used to be the whole
# list here, which silently never checked (or scanned) `lite`/`blackwell` once
# those existed. `blackwell` is deliberately excluded: it is never built by
# `all`/`auto` and is not part of this stage's local-build precondition.
declare -A REPO_FOR_COMPONENT=()
while IFS=$'\t' read -r component repo; do
    [[ "$component" == "blackwell" ]] && continue
    REPO_FOR_COMPONENT["$component"]="$repo"
done < <(./scripts/security-scan.sh list-repos)

missing=()
for repo in "${REPO_FOR_COMPONENT[@]}"; do
    docker image inspect "${repo}:${VERSION}" >/dev/null 2>&1 || missing+=("${repo}:${VERSION}")
done
if (( ${#missing[@]} )); then
    echo -e "${RED}not built locally at ${VERSION}:${NC}" >&2
    printf '  %s\n' "${missing[@]}" >&2
    echo "run the build stage first: ./scripts/release.sh build ${VERSION}" >&2
    exit 3
fi

echo -e "${BLUE}Scanning locally built ${VERSION} images${NC}" >&2

# security-scan.sh's own `all` target scans EVERY scannable component,
# including `blackwell` — which this stage's build precondition above never
# checks for, because `blackwell` is not part of `docker-build-push.sh all`/
# `auto` (built and scanned only on request, see docker-build-push.sh's
# build_backend_blackwell). Scanning explicitly by the components this stage
# actually has local images for avoids a false "could not scan" on an image
# nothing here ever built.
rc=0
components_attempted=0
could_not_scan=()      # components whose scanner reported exit >= 2 (never looked)
findings_in=()         # components scanned that reported findings (exit 1)
for component in "${!REPO_FOR_COMPONENT[@]}"; do
    comp_rc=0
    SCAN_SOURCE=local \
    FAIL_ON_CRITICAL="${FAIL_ON_CRITICAL:-true}" \
    OUTPUT_DIR="$OUT_DIR" \
    IMAGE_TAG="$VERSION" \
        ./scripts/security-scan.sh "$component" || comp_rc=$?
    components_attempted=$((components_attempted + 1))
    if (( comp_rc >= 2 )); then
        could_not_scan+=("$component")
    elif (( comp_rc == 1 )); then
        findings_in+=("$component")
    fi
    (( comp_rc > rc )) && rc=$comp_rc
done

# Recorded BEFORE the flattening below, which is the only place the two outcomes are still
# distinguishable. `could-not-scan` and `findings-present` become the same exit code here on
# purpose (both are gate failures, and 2/3 mean something else to the release runner), so
# without capturing it now the criteria could not tell a release that found CVEs from a
# release that never looked — which is the whole of issue #681.
if (( ${#could_not_scan[@]} > 0 )); then
    record could-not-scan-is-not-a-pass fail \
        "never scanned: ${could_not_scan[*]}" \
        "build the missing image/leg, then re-run: ./scripts/release.sh scan $VERSION"
else
    record could-not-scan-is-not-a-pass pass \
        "every component was examined; ${#findings_in[@]} reported findings"
fi

if (( components_attempted > 0 && ${#could_not_scan[@]} == 0 )); then
    record every-published-repo-scanned pass
elif (( components_attempted == 0 )); then
    record every-published-repo-scanned not-measured \
        "no components derived from security-scan.sh list-repos" \
        "./scripts/security-scan.sh list-repos"
else
    record every-published-repo-scanned fail "unscanned: ${could_not_scan[*]}"
fi

if (( rc >= 2 )); then
    echo -e "${RED}scan could NOT RUN (security-scan.sh exit ${rc}) — this is not 'no findings'${NC}" >&2
    rc=1
fi

# Assert the reports describe the image we meant to scan — ONE REPORT PER ARCHITECTURE LEG.
# Reading the tag back out of the artifact is the only thing that would have caught the
# wrong-release bug above; requiring a report per leg is what stops a missing architecture
# from looking like a passing one (issue #667).
#
# ⚠️ A MISSING report is now a FAILURE. This loop used to read
#
#     report="$OUT_DIR/${comp}-trivy.json"
#     [[ -f "$report" ]] || continue
#
# which is fail-OPEN: if the scan never produced a report the verification silently skipped
# itself and the stage passed. Per-arch naming would have made that `continue` fire for every
# component at once — the entire assertion quietly deleted, with a green stage.
legs_verified=0
legs_expected=0
legs_missing=()
legs_wrong_version=()
while IFS=$'\t' read -r comp _capability platforms; do
    [[ "$comp" == "blackwell" ]] && continue
    [[ -n "${REPO_FOR_COMPONENT[$comp]:-}" ]] || continue
    IFS=',' read -r -a plats <<< "$platforms"
    for platform in "${plats[@]}"; do
        [[ -n "$platform" ]] || continue
        legs_expected=$((legs_expected + 1))
        label="${comp}-${platform#linux/}"
        report="$OUT_DIR/${label}-trivy.json"
        if [[ ! -f "$report" ]]; then
            echo -e "${RED}no scan report for ${label} (${report}) — that leg was NOT SCANNED${NC}" >&2
            legs_missing+=("$label")
            rc=1
            continue
        fi
        scanned=$(python3 -c "
import json
try: print(json.load(open('$report')).get('ArtifactName',''))
except Exception: print('')
" 2>/dev/null)
        # The scanner scans an arch-qualified local re-tag (repo:VERSION-scanleg-<arch>), so
        # match the version prefix rather than requiring the bare tag to be the suffix.
        if [[ "$scanned" != *":${VERSION}"* ]]; then
            echo -e "${RED}report mismatch: ${label}-trivy.json describes '${scanned}', not :${VERSION}${NC}" >&2
            legs_wrong_version+=("$label")
            rc=1
        else
            echo -e "${GREEN}  ${label}: scanned ${scanned}${NC}" >&2
            legs_verified=$((legs_verified + 1))
        fi
    done
done < <(./scripts/docker-build-push.sh list-platforms)

# Zero verified legs is COULD NOT CHECK, never "nothing to check" — the same rule the
# scanner's own empty-platform-list branch applies.
if (( legs_verified == 0 )); then
    echo -e "${RED}verified 0 architecture legs — refusing to report a clean scan${NC}" >&2
    rc=1
    record every-arch-leg-scanned not-measured \
        "no leg reports found at all (expected ${legs_expected})" \
        "./scripts/release.sh build $VERSION   # builds every declared leg"
elif (( ${#legs_missing[@]} > 0 )); then
    record every-arch-leg-scanned fail "unscanned legs: ${legs_missing[*]}"
else
    echo -e "${GREEN}verified ${legs_verified} architecture leg report(s)${NC}" >&2
    record every-arch-leg-scanned pass
fi

if (( legs_verified == 0 )); then
    record reports-describe-this-version not-measured "no reports to read a version out of"
elif (( ${#legs_wrong_version[@]} > 0 )); then
    record reports-describe-this-version fail \
        "reports describing another version: ${legs_wrong_version[*]}"
else
    record reports-describe-this-version pass
fi

# Both halves of the contract.
criteria_assert_all_checked

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"scan","version":"%s","status":"%s","reports":"%s","criteria":[%s],"next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" "$OUT_DIR" "$(criteria_json)" \
        "$([[ $rc -eq 0 ]] && echo '["rehearse"]' || echo '["review the findings, or --force-scan with a recorded reason"]')"
fi
exit $rc
