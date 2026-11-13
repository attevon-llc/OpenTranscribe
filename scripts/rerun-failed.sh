#!/usr/bin/env bash
# Re-run ONLY the failed tests of a gate run, for the fix loop. NOT EVIDENCE: the release path
# never uses this (backend/tests/unit/test_release_path_never_selects.py), the full stage confirms.
#
#   scripts/rerun-failed.sh <artifact-dir>     # e.g. /tmp/ot-integration-gate
#
# Needs run-integration-tests.sh's per-phase <junit>.xml.argv files, and for stack-bound phases
# the *_PORT vars of the kept stack exported (RELEASE_TEST_KEEP=1). Phases with no .argv refuse.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
dir="${1:-}"
[ -d "$dir" ] || { echo "usage: $0 <artifact-dir>" >&2; exit 2; }
PY="${RERUN_PYTHON:-$REPO_ROOT/backend/venv/bin/python}"
banner() { echo "NOT EVIDENCE: confirm with the full stage"; }
banner
rc=0; any=0
shopt -s nullglob
for xml in "$dir"/*.xml; do
    mapfile -t ids < <("$PY" "$SCRIPT_DIR/lib/junit_failed.py" "$xml" "$REPO_ROOT/backend")
    [ "${#ids[@]}" -gt 0 ] || continue
    any=1
    if [ ! -f "$xml.argv" ]; then
        echo "REFUSED: $xml has failures but no $xml.argv to replay (run-integration-tests.sh writes it)" >&2
        rc=2; continue
    fi
    cmd=(); placed=0
    while IFS= read -r w; do
        if [[ "$w" =~ ^([A-Za-z_][A-Za-z0-9_]*)=@ENV$ ]]; then
            v="${BASH_REMATCH[1]}"
            [ -n "${!v:-}" ] || { echo "REFUSED: $v is not set in the environment" >&2; rc=2; continue 2; }
            w="$v=${!v}"
        elif [[ "$w" == --junitxml=* ]]; then
            w="--junitxml=$xml.rerun.xml"
        elif [[ "$w" == tests/* && "$w" != --* ]]; then
            [ "$placed" = 1 ] && continue
            cmd+=("${ids[@]}"); placed=1; continue
        fi
        cmd+=("$w")
    done <"$xml.argv"
    echo "--- rerun $(basename "$xml"): ${#ids[@]} failed id(s) ---"
    (cd "$REPO_ROOT/backend" && "${cmd[@]}") || rc=$?
done
[ "$any" = 1 ] || echo "no failures in $dir"
banner
exit "$rc"
