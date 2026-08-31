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
# id|stage|description|command|exit-contract
#
# EVERY leg's "command" is a REAL, runnable command line, and run_leg() executes
# it for every stage. Stages 2/3/4 used to be precondition-checked only: they
# verified the stack was up/down and then wrote `NOT-MEASURED ... execution is a
# separate, future effort` to the report and returned 0. So `test-matrix.sh all`
# exited 0 having proven the eight fast static checks and the leg table's own
# doc-sync — and nothing about GPU scaling, diarization, lite mode, auth, PKI,
# fresh install or upgrade, despite listing all of them. A placeholder that
# reads like coverage is worse than no leg at all.
#
# THIS FILE STILL OWNS NO TEST LOGIC. Every command below is an existing script
# invoked with the flags full-test-matrix.md already documents for that leg.
# Where a leg needs a sequence, it calls the script that already owns that
# sequence (stage 3 -> scripts/release/65-rehearse.sh, which is the same thing
# `scripts/release.sh rehearse` runs; stage 4 -> scripts/release/50-scan.sh).
# Those stage scripts are pure: only release.sh writes the .release/<v>/ ledger,
# so running them from here cannot corrupt a real release's recorded state.
#
# exit-contract selects how the leg's exit code is READ, because this repo has
# two conventions and conflating them would misreport results:
#   standard  0 pass · non-zero fail            (release.sh / this script's own)
#   smoke     0 pass · 1 fail · 4 NOT MEASURED  (gpu-scale/diar-native/lite-smoke)
# Note 4 means "operator abort" in the standard contract and "not measured" in
# the smoke one. That divergence is real and pre-existing; declaring it per leg
# is how this script reads each verdict correctly instead of calling a smoke
# script's honest "I could not measure this" an operator abort.
LEGS=(
    "1.1|1|safe-precommit full run|scripts/safe-precommit.sh run --all-files|standard"
    "1.2|1|backend test summary|scripts/run-backend-tests.sh --summary|standard"
    "1.3|1|backend + frontend test-quality audits|python3 scripts/audit-tests.py backend/tests|standard"
    "1.4|1|frontend check (no rebuild)|scripts/frontend-check.sh --no-claude --check-only|standard"
    "1.5|1|docs-site build|cd docs-site && npm run build|standard"
    "1.6|1|deployment matrix validation|scripts/validate-deployments.sh --json|standard"
    "1.7|1|version consistency|python3 scripts/release/check-version-consistency.py|standard"
    "1.8|1|route coverage|backend/venv/bin/python3 scripts/audit-route-coverage.py --json|standard"
    "2a|2|Cycle 2A — integration gate + full e2e + frontend + auth e2e (real vLLM leg excluded, see doc)|scripts/run-dev-tests.sh --full --all-overlays --search-quality && scripts/run-auth-e2e.sh --cleanup --skip-pki|standard"
    "2b|2|Cycle 2B — GPU scaling|scripts/gpu-scale-smoke.sh|smoke"
    "2c|2|Cycle 2C — diarization providers|scripts/diar-native-smoke.sh|smoke"
    "2d|2|Cycle 2D — lite/cpu-only topology|scripts/lite-smoke.sh|smoke"
    "3|3|deployment mode rehearsal (fresh-install + upgrade)|scripts/release/65-rehearse.sh \"\$(tr -d '[:space:]' < VERSION)\"|standard"
    "3-lite|3|lite-mode full pipeline rehearsal (mocked cloud ASR + mocked LLM)|scripts/release-tests/test-lite-mode.sh --yes|standard"
    "3-pki|3|PKI/mTLS (prod+nginx only)|scripts/pki/run-pki-e2e-leg.sh --yes|standard"
    "4|4|image/release gates confirmation|scripts/release/50-scan.sh \"\$(tr -d '[:space:]' < VERSION)\"|standard"
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
    grep -q "### Stage 3 — lite-mode full rehearsal" "$DOC" || missing_in_doc+=("3-lite")
    grep -qi "PKI/mTLS is prod" "$DOC" || missing_in_doc+=("3-pki")
    grep -q "## Stage 4" "$DOC" || missing_in_doc+=("4")

    # Reverse direction: every doc leg id has a LEGS entry.
    local doc_leg_ids=(1.1 1.2 1.3 1.4 1.5 1.6 1.7 1.8 2a 2b 2c 2d 3 3-lite 3-pki 4)
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
        IFS='|' read -r id stage desc cmd contract <<< "$entry"
        contract="${contract:-standard}"
        if [[ "$JSON_OUT" == "true" ]]; then
            printf '{"leg":"%s","stage":%s,"description":"%s","command":"%s","exit_contract":"%s"}\n' \
                "$id" "$stage" "$desc" "$cmd" "$contract"
        else
            printf "  %-6s stage %s  [%-8s] %-60s %s\n" "$id" "$stage" "$contract" "$desc" "$cmd"
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

check_stage4_tooling() {
    # Warnings, not a precondition: 50-scan.sh degrades per missing scanner and reports what it
    # actually ran. Saying so up front keeps a thin scan from reading as a thorough one.
    local missing=()
    command -v trivy >/dev/null 2>&1 || missing+=(trivy)
    command -v grype >/dev/null 2>&1 || missing+=(grype)
    command -v syft  >/dev/null 2>&1 || missing+=(syft)
    if (( ${#missing[@]} > 0 )); then
        info "  ${YELLOW}warn${NC}: not on PATH — scan coverage reduced: ${missing[*]}"
    fi
    return 0
}

# ------------------------------------------------------------ leg outcomes
#
# Three outcomes, and the third is deliberately NOT a generic placeholder:
#   PASS  the leg ran and its own criteria held
#   FAIL  the leg ran and did not pass (log path recorded)
#   SKIP  the leg ran and reported, in its own words, that it COULD NOT MEASURE this here
#         (smoke-contract exit 4) — always accompanied by that script's stated reason
#
# A SKIP does not fail the run, but it is counted and printed loudly at the end, the same
# discipline scripts/audit-tests.py uses for its DEFERRED count: a green matrix must never be
# mistaken for a fully measured one.
SKIP_COUNT=0
declare -a SKIPPED_LEGS=()

# not_measured_reason LOG_FILE
#   Pull the wrapped script's own "NOT MEASURED" explanation out of its log, so the report says
#   WHY rather than repeating a generic sentence. Falls back to the last non-empty line.
not_measured_reason() {
    local log_file="$1" line
    line="$(grep -m1 -iE 'not measured' "$log_file" 2>/dev/null | sed 's/^[[:space:]]*//')"
    [[ -n "$line" ]] || line="$(grep -v '^[[:space:]]*$' "$log_file" 2>/dev/null | tail -1)"
    [[ -n "$line" ]] || line="no reason reported by the leg"
    echo "$line"
}

# -------------------------------------------------------------- execution
#
# ONE execution path for every stage. Stage-specific work happens BEFORE it (which precondition,
# whether --yes is required); the run itself, the exit-code reading and the report line are
# identical everywhere. Stages 2/3/4 used to stop after the precondition and write a
# NOT-MEASURED placeholder — see the LEGS header for why that had to go.
run_leg() {
    local id="$1"
    local entry desc cmd stage contract
    for entry in "${LEGS[@]}"; do
        [[ "${entry%%|*}" == "$id" ]] && { IFS='|' read -r _ stage desc cmd contract <<< "$entry"; break; }
    done
    [[ -n "${cmd:-}" ]] || { err "unknown leg: $id"; return $EXIT_MISUSE; }
    contract="${contract:-standard}"

    if [[ "$MODE_DRY_RUN" == "true" ]]; then
        info "[dry-run] $id ($desc) [$contract]: $cmd"
        return 0
    fi

    case "$stage" in
        1) ;;
        2) check_stage2_precondition || return $? ;;
        3)
            [[ "$ASSUME_YES" == "true" ]] || { err "Stage 3 leg $id needs --yes (it rebuilds images and rehearses real deployments for hours)"; return $EXIT_ABORT; }
            check_stage3_precondition || return $?
            ;;
        4) check_stage4_tooling ;;
        *) err "leg $id has an unknown stage: $stage"; return $EXIT_MISUSE ;;
    esac

    mkdir -p "$LEDGER_DIR"
    local log_file="$LEDGER_DIR/${id}.log"
    local started elapsed leg_rc=0

    info "→ $id: $desc"
    started=$(date +%s)
    bash -c "$cmd" > "$log_file" 2>&1 || leg_rc=$?
    elapsed=$(( $(date +%s) - started ))

    if [[ $leg_rc -eq 0 ]]; then
        echo "PASS  $id  $desc  (${elapsed}s)" >> "$REPORT_FILE"
        info "  ${GREEN}PASS${NC} (${elapsed}s)"
        return 0
    fi

    # Exit 4 means two different things in this repo (see the LEGS header): "operator abort"
    # under the standard contract, "NOT MEASURED" under the smoke one. Read it per the leg's
    # declared contract rather than guessing.
    if [[ "$contract" == "smoke" && $leg_rc -eq 4 ]]; then
        local reason
        reason="$(not_measured_reason "$log_file")"
        echo "SKIP  $id  $desc  — NOT MEASURED: $reason  (see $log_file)" >> "$REPORT_FILE"
        info "  ${YELLOW}SKIP${NC} — NOT MEASURED: $reason"
        SKIP_COUNT=$((SKIP_COUNT + 1))
        SKIPPED_LEGS+=("$id: $reason")
        return 0
    fi

    echo "FAIL  $id  $desc  (exit $leg_rc, ${elapsed}s — see $log_file)" >> "$REPORT_FILE"
    info "  ${RED}FAIL${NC} exit $leg_rc (${elapsed}s) — see $log_file"
    return $EXIT_GATE
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
    # A run with skipped legs is NOT a fully measured matrix, and must not read like one.
    # Same discipline as scripts/audit-tests.py's DEFERRED count.
    if (( SKIP_COUNT > 0 )); then
        info ""
        info "${YELLOW}${SKIP_COUNT} leg(s) reported NOT MEASURED — this run did not cover them:${NC}"
        for s in "${SKIPPED_LEGS[@]}"; do info "  ⊘ $s"; done
        info "${YELLOW}A green matrix with skips is not a fully measured one.${NC}"
    fi
    info "Report: $REPORT_FILE"
fi

exit "$RC"
