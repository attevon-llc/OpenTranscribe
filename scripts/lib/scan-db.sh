#!/bin/bash
# Sourced by scripts/security-scan.sh: scan timing stamps and the one-time vulnerability DB pin.
# Needs print_info/print_error and EXIT_COULD_NOT_SCAN from the caller.

# One start/end line per tool per leg (R9): makes every duration in a scan measurable from its log.
scan_stamp() {
    printf '[scan-time] %s %s %s %s\n' "$1" "$2" "$3" "$(date -u +%FT%TZ)" >&2
}

# timed <label> <tool> <fn...>: run fn between a start and an end stamp, keeping its status.
timed() {
    local label=$1 tool=$2 r=0
    shift 2
    scan_stamp "${label}" "${tool}" start
    "$@" || r=$?
    scan_stamp "${label}" "${tool}" end
    return "${r}"
}

# Fetch both vulnerability DBs ONCE, then pin every leg to that snapshot (R5).
# Trivy: `trivy image --download-db-only`, then --skip-db-update (env TRIVY_SKIP_DB_UPDATE).
# Grype: `grype db update`, then GRYPE_DB_AUTO_UPDATE=false.
# Docs: https://trivy.dev/docs/latest/guide/configuration/db/ and
# https://github.com/anchore/grype#grype-db-update (db auto-update setting).
# Exported SCAN_DB_PINNED lets a caller that already pinned (50-scan.sh runs one
# security-scan.sh per component) make the children skip the fetch. A failed fetch is COULD NOT
# SCAN: judging an image against an unknown DB is not a scan. SCAN_DB_UPDATE=false opts out
# (offline host; the DB already on disk is used and its version is still recorded).
pin_vuln_dbs() {
    [ "${SCAN_DB_PINNED:-}" = "1" ] && return 0
    if [ "${SCAN_DB_UPDATE:-true}" = "true" ]; then
        print_info "Fetching vulnerability databases once for every leg..."
        trivy image --download-db-only --quiet || {
            print_error "trivy DB download failed - COULD NOT SCAN (set SCAN_DB_UPDATE=false to use the DB on disk)"
            return "${EXIT_COULD_NOT_SCAN}"
        }
        grype db update || {
            print_error "grype DB update failed - COULD NOT SCAN (set SCAN_DB_UPDATE=false to use the DB on disk)"
            return "${EXIT_COULD_NOT_SCAN}"
        }
    fi
    export TRIVY_SKIP_DB_UPDATE=true GRYPE_DB_AUTO_UPDATE=false SCAN_DB_PINNED=1
}

# Vulnerability DB versions, as JSON fields for the verdict. Empty string when unreadable
# (recorded as such rather than failing a finished scan).
trivy_db_version() {
    trivy --version --format json 2>/dev/null | jq -r '.VulnerabilityDB.UpdatedAt // empty' 2>/dev/null || true
}
grype_db_version() {
    grype db status -o json 2>/dev/null | jq -r '.built // empty' 2>/dev/null || true
}
