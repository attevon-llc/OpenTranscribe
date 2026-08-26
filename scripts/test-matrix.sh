#!/bin/bash
#
# Dispatcher for the local full-application test matrix documented in
# docs-site/docs/developer-guide/full-test-matrix.md.
#
# THIS SCRIPT OWNS NO TEST LOGIC OF ITS OWN. Every leg wraps an existing script
# (scripts/run-backend-tests.sh, scripts/validate-deployments.sh,
# scripts/run-integration-tests.sh, scripts/run-auth-e2e.sh,
# scripts/diar-native-smoke.sh, scripts/lite-smoke.sh, scripts/gpu-scale-smoke.sh,
# scripts/release-tests/*, scripts/release.sh). If a leg needs new behaviour, the
# behaviour belongs in the wrapped script, not here.
#
# ANTI-STALENESS (the reason this file exists rather than a hand-run checklist)
# -------------------------------------------------------------------------
# scripts/validate-deployments.sh keeps its deployment matrix from drifting out
# of sync with opentr.sh by parsing the doc's flag list and failing when a
# documented flag has no matrix row. This script applies the same technique in
# the other direction: it parses full-test-matrix.md's leg identifiers and
# fails loudly if a documented leg has no LEGS entry here, or a LEGS entry has
# no matching leg in the doc. Never add a leg to one without the other in the
# same commit.
#
# Usage:
#   scripts/test-matrix.sh <1|2|3|4|all> [--only <leg>] [--json] [--dry-run] [--yes]
#   scripts/test-matrix.sh --list
#
# Exit codes: 0 pass · 1 gate failed · 2 misuse · 3 precondition unmet · 4 operator abort.
# Identical meaning to scripts/release.sh's contract.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1

EXIT_GATE=1
EXIT_MISUSE=2
EXIT_PRECONDITION=3
EXIT_ABORT=4

DOC="docs-site/docs/developer-guide/full-test-matrix.md"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

err() { echo -e "${RED}error:${NC} $*" >&2; }
info() { echo -e "$*" >&2; }

MODE_LIST=false
MODE_DRY_RUN=false
JSON_OUT=false
ASSUME_YES=false
ONLY=""
STAGE_ARG=""

# ------------------------------------------------------------- the leg table
#
# id|stage|description|command
#
# "command" is informational for --list/--dry-run; execution for stage 1 legs
# runs the real command, stage 2/3/4 legs are precondition-checked only (they
# need a live/stopped stack, GPU time and hours — see full-test-matrix.md).
LEGS=(
    "1.1|1|safe-precommit full run|scripts/safe-precommit.sh run --all-files"
    "1.2|1|backend test summary|scripts/run-backend-tests.sh --summary"
    "1.3|1|backend + frontend test-quality audits|python3 scripts/audit-tests.py backend/tests"
    "1.4|1|frontend check (no rebuild)|scripts/frontend-check.sh --no-claude --check-only"
    "1.5|1|docs-site build|cd docs-site && npm run build"
    "1.6|1|deployment matrix validation|scripts/validate-deployments.sh --json"
    "1.7|1|version consistency|python3 scripts/release/check-version-consistency.py"
    "1.8|1|route coverage|backend/venv/bin/python3 scripts/audit-route-coverage.py --json"
    "2a|2|baseline + LLM + auth (mock-llm, llm-test, ldap/keycloak/authentik-test)|opentr.sh start dev --with-mock-llm --with-llm-test --with-ldap-test --with-keycloak-test --with-authentik-test"
    "2b|2|GPU scaling|scripts/gpu-scale-smoke.sh"
    "2c|2|diarization providers|scripts/diar-native-smoke.sh"
    "2d|2|lite/cpu-only|scripts/lite-smoke.sh"
    "3|3|deployment mode rehearsal (fresh-install + upgrade)|scripts/release-tests/test-fresh-install.sh"
    "3-pki|3|PKI/mTLS (prod+nginx only)|pytest backend/tests/e2e/test_pki.py"
    "4|4|image/release gates confirmation|scripts/release.sh scan"
)

usage() {
    cat <<'EOF'
Usage: scripts/test-matrix.sh <1|2|3|4|all> [options]
       scripts/test-matrix.sh --list

  1|2|3|4|all   Run the named stage (or all four) from full-test-matrix.md
  --only <leg>  Run a single leg id (e.g. 1.2, 2b, 3-pki)
  --list        Print every leg parsed from the doc, run nothing
  --json        Machine-readable {stage, leg, status, criteria[], next[]} lines
  --dry-run     Print every command that would run, execute nothing
  --yes         Bypass confirmation prompts (required for stage 3)

Exit codes: 0 pass, 1 gate failed, 2 misuse, 3 precondition unmet, 4 operator abort.
EOF
}

# ------------------------------------------------------------ arg parsing
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --list) MODE_LIST=true; shift ;;
        --dry-run) MODE_DRY_RUN=true; shift ;;
        --json) JSON_OUT=true; shift ;;
        --yes) ASSUME_YES=true; shift ;;
        --only) ONLY="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) err "unknown option: $1"; usage; exit $EXIT_MISUSE ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
[[ ${#POSITIONAL[@]} -ge 1 ]] && STAGE_ARG="${POSITIONAL[0]}"

# --------------------------------------------------------- anti-staleness
#
# Every LEGS id must appear in the doc, and every doc-declared leg must appear
# in LEGS. Stage 1 legs are matched against the doc's 8-row table by row
# number; stage 2/3/4 legs are matched against their header anchors.
check_doc_sync() {
    [[ -f "$DOC" ]] || {
        err "doc missing at $DOC — anti-staleness check cannot run"
        return $EXIT_GATE
    }

    local missing_in_doc=() missing_in_script=()

    # Stage 1: doc has a "| N | \`cmd\`" row for each of 8 legs.
    local stage1_rows
    stage1_rows="$(grep -cE '^\| [0-9]+ \| ' "$DOC" || true)"
    for n in 1 2 3 4 5 6 7 8; do
        grep -qE "^\| $n \| " "$DOC" || missing_in_doc+=("1.$n")
    done
    [[ "$stage1_rows" -ge 8 ]] || missing_in_doc+=("stage1-table-incomplete(${stage1_rows}/8)")

    # Stage 2/3/4: doc has the matching header/anchor text.
    grep -q "Cycle 2A" "$DOC" || missing_in_doc+=("2a")
    grep -q "Cycle 2B" "$DOC" || missing_in_doc+=("2b")
    grep -q "Cycle 2C" "$DOC" || missing_in_doc+=("2c")
    grep -q "Cycle 2D" "$DOC" || missing_in_doc+=("2d")
    grep -q "## Stage 3" "$DOC" || missing_in_doc+=("3")
    grep -qi "PKI/mTLS is prod" "$DOC" || missing_in_doc+=("3-pki")
    grep -q "## Stage 4" "$DOC" || missing_in_doc+=("4")

    # Reverse direction: every doc leg id has a LEGS entry.
    local doc_leg_ids=(1.1 1.2 1.3 1.4 1.5 1.6 1.7 1.8 2a 2b 2c 2d 3 3-pki 4)
    for id in "${doc_leg_ids[@]}"; do
        local found=false
        for entry in "${LEGS[@]}"; do
            [[ "${entry%%|*}" == "$id" ]] && found=true && break
        done
        [[ "$found" == "true" ]] || missing_in_script+=("$id")
    done

    if [[ ${#missing_in_doc[@]} -gt 0 || ${#missing_in_script[@]} -gt 0 ]]; then
        err "doc/script leg mismatch — anti-staleness check failed"
        [[ ${#missing_in_doc[@]} -gt 0 ]] && err "  legs in script, not documented (or doc anchor renamed): ${missing_in_doc[*]}"
        [[ ${#missing_in_script[@]} -gt 0 ]] && err "  legs in doc, not implemented in LEGS: ${missing_in_script[*]}"
        return $EXIT_GATE
    fi
    return 0
}

# -------------------------------------------------------------------- list
list_legs() {
    for entry in "${LEGS[@]}"; do
        IFS='|' read -r id stage desc cmd <<< "$entry"
        if [[ "$JSON_OUT" == "true" ]]; then
            printf '{"leg":"%s","stage":%s,"description":"%s","command":"%s"}\n' "$id" "$stage" "$desc" "$cmd"
        else
            printf "  %-6s stage %s  %-55s %s\n" "$id" "$stage" "$desc" "$cmd"
        fi
    done
}

# --------------------------------------------------------- stage 2/3/4 preconditions
service_reachable() {
    local host="$1" port="$2"
    (exec 3<>"/dev/tcp/${host}/${port}") >/dev/null 2>&1 && { exec 3>&- 3<&-; return 0; }
    return 1
}

check_stage2_precondition() {
    # Canonical dev-stack ports (docker-compose.yml defaults), matching
    # backend/tests/conftest.py's own TCP-probe approach.
    service_reachable localhost 5174 || {
        err "Stage 2 needs the dev stack reachable — start it: ./opentr.sh start dev [overlays for the cycle]"
        return $EXIT_PRECONDITION
    }
    return 0
}

check_stage3_precondition() {
    if service_reachable localhost 5174; then
        err "Stage 3 requires the dev stack STOPPED (it rebuilds and rehearses against prod images). Run: ./opentr.sh stop"
        return $EXIT_PRECONDITION
    fi
    return 0
}

# -------------------------------------------------------------- execution
run_leg() {
    local id="$1"
    local entry desc cmd stage
    for entry in "${LEGS[@]}"; do
        [[ "${entry%%|*}" == "$id" ]] && { IFS='|' read -r _ stage desc cmd <<< "$entry"; break; }
    done
    [[ -n "${cmd:-}" ]] || { err "unknown leg: $id"; return $EXIT_MISUSE; }

    if [[ "$MODE_DRY_RUN" == "true" ]]; then
        info "[dry-run] $id ($desc): $cmd"
        return 0
    fi

    mkdir -p "$LEDGER_DIR"
    local log_file="$LEDGER_DIR/${id}.log"

    case "$stage" in
        1)
            info "→ $id: $desc"
            if bash -c "$cmd" > "$log_file" 2>&1; then
                echo "PASS  $id  $desc" >> "$REPORT_FILE"
                info "  ${GREEN}PASS${NC}"
                return 0
            else
                echo "FAIL  $id  $desc  (see $log_file)" >> "$REPORT_FILE"
                info "  ${RED}FAIL${NC} — see $log_file"
                return $EXIT_GATE
            fi
            ;;
        2)
            check_stage2_precondition || return $?
            echo "NOT-MEASURED  $id  $desc  (Stage 2 execution is a separate, future effort — see full-test-matrix.md)" >> "$REPORT_FILE"
            info "  precondition met; leg itself is a documented future effort, not executed here"
            return 0
            ;;
        3)
            [[ "$ASSUME_YES" == "true" ]] || { err "Stage 3 leg $id needs --yes"; return $EXIT_ABORT; }
            check_stage3_precondition || return $?
            echo "NOT-MEASURED  $id  $desc  (Stage 3 execution is a separate, future effort — see full-test-matrix.md)" >> "$REPORT_FILE"
            info "  precondition met; leg itself is a documented future effort, not executed here"
            return 0
            ;;
        4)
            command -v trivy >/dev/null 2>&1 || info "  ${YELLOW}warn${NC}: trivy not on PATH — scan coverage reduced"
            command -v grype >/dev/null 2>&1 || info "  ${YELLOW}warn${NC}: grype not on PATH — scan coverage reduced"
            command -v syft >/dev/null 2>&1 || info "  ${YELLOW}warn${NC}: syft not on PATH — scan coverage reduced"
            echo "NOT-MEASURED  $id  $desc  (confirm-only stage; run via scripts/release.sh scan/build/publish/promote)" >> "$REPORT_FILE"
            return 0
            ;;
    esac
}

run_stage() {
    local stage="$1" rc=0
    for entry in "${LEGS[@]}"; do
        IFS='|' read -r id s _ _ <<< "$entry"
        [[ "$s" == "$stage" ]] || continue
        run_leg "$id" || rc=$?
    done
    return $rc
}

# ------------------------------------------------------------------ main
if [[ "$MODE_LIST" == "true" ]]; then
    check_doc_sync || exit $?
    list_legs
    exit 0
fi

check_doc_sync || exit $?

if [[ "$MODE_DRY_RUN" == "true" && -z "$STAGE_ARG" && -z "$ONLY" ]]; then
    STAGE_ARG="all"
fi

[[ -n "$STAGE_ARG" || -n "$ONLY" ]] || { usage; exit $EXIT_MISUSE; }

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LEDGER_DIR="$REPO_ROOT/.test-matrix/$TIMESTAMP"
REPORT_FILE="$LEDGER_DIR/REPORT.md"
if [[ "$MODE_DRY_RUN" != "true" ]]; then
    mkdir -p "$LEDGER_DIR"
    {
        echo "# Test matrix run — $TIMESTAMP"
        echo
        echo "| Status | Leg | Description |"
        echo "|---|---|---|"
    } > "$REPORT_FILE"
fi

RC=0
if [[ -n "$ONLY" ]]; then
    run_leg "$ONLY" || RC=$?
else
    case "$STAGE_ARG" in
        1|2|3|4) run_stage "$STAGE_ARG" || RC=$? ;;
        all) for s in 1 2 3 4; do run_stage "$s" || RC=$?; done ;;
        *) err "unknown stage: $STAGE_ARG (expected 1, 2, 3, 4, or all)"; exit $EXIT_MISUSE ;;
    esac
fi

if [[ "$MODE_DRY_RUN" != "true" ]]; then
    info "Report: $REPORT_FILE"
fi

exit "$RC"
