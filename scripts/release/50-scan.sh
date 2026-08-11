#!/bin/bash
# Scan the LOCALLY BUILT images. Never the registry.
#
# SCAN_SOURCE=local matters: run_parallel_scans' default path does `docker rmi`
# then `docker pull` of :latest, i.e. it scans the PREVIOUS release. That is
# correct only after a push and actively wrong as a pre-push gate.
#
# Exit: 0 clean · 1 blocking findings

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
HUB="${DOCKERHUB_USERNAME:-davidamacey}"

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
missing=()
for repo in "$HUB/opentranscribe-backend" "$HUB/opentranscribe-frontend" "$HUB/opentranscribe-docs"; do
    docker image inspect "${repo}:${VERSION}" >/dev/null 2>&1 || missing+=("${repo}:${VERSION}")
done
if (( ${#missing[@]} )); then
    echo -e "${RED}not built locally at ${VERSION}:${NC}" >&2
    printf '  %s\n' "${missing[@]}" >&2
    echo "run the build stage first: ./scripts/release.sh build ${VERSION}" >&2
    exit 3
fi

echo -e "${BLUE}Scanning locally built ${VERSION} images${NC}" >&2

rc=0
SCAN_SOURCE=local \
FAIL_ON_CRITICAL="${FAIL_ON_CRITICAL:-true}" \
OUTPUT_DIR="$OUT_DIR" \
IMAGE_TAG="$VERSION" \
    ./scripts/security-scan.sh all || rc=$?

# Assert the reports describe the image we meant to scan. Reading the tag back
# out of the artifact is the only thing that would have caught the bug above --
# the run looked completely normal while measuring the wrong release.
for comp in backend frontend docs; do
    report="$OUT_DIR/${comp}-trivy.json"
    [[ -f "$report" ]] || continue
    scanned=$(python3 -c "
import json,sys
try: print(json.load(open('$report')).get('ArtifactName',''))
except Exception: print('')
" 2>/dev/null)
    if [[ "$scanned" != *":${VERSION}" ]]; then
        echo -e "${RED}report mismatch: ${comp}-trivy.json describes '${scanned}', not :${VERSION}${NC}" >&2
        rc=1
    else
        echo -e "${GREEN}  ${comp}: scanned ${scanned}${NC}" >&2
    fi
done

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"scan","version":"%s","status":"%s","reports":"%s","next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" "$OUT_DIR" \
        "$([[ $rc -eq 0 ]] && echo '["rehearse"]' || echo '["review the findings, or --force-scan with a recorded reason"]')"
fi
exit $rc
