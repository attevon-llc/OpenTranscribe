#!/bin/bash
# OpenTranscribe release orchestrator.
#
# Replaces a 168-line markdown checklist that no machine could execute and that
# had already drifted from the two other checklists describing the same process.
# Everything mechanical lives in scripts/release/NN-<stage>.sh; this file owns
# argument parsing, the ledger, and dispatch — nothing else.
#
# DESIGN RULES
#
#   * Every stage is independently runnable, skippable, and resumable. A release
#     that dies at hour three must not restart from zero.
#   * Nothing reaches the outside world implicitly. tag / publish / promote /
#     finish are the only stages that do, they are listed in .claude/settings.json
#     under `ask`, and each announces itself first.
#   * Gates are overridable, never silently. --force-<stage> proceeds but records
#     the operator and a required reason in the ledger.
#   * Every stage emits --json with a stable shape so an agent can drive this
#     without parsing prose. Logs go to stderr, JSON to stdout, never interleaved.
#
# EXIT CODES (stable — agents branch on these)
#   0  stage passed
#   1  a gate failed (fix and re-run)
#   2  misuse: bad arguments
#   3  precondition unmet (live stack up, builder unreachable, dirty worktree)
#   4  aborted by the operator
#
# Usage:
#   ./scripts/release.sh status
#   ./scripts/release.sh explain publish
#   ./scripts/release.sh preflight 0.5.0
#   ./scripts/release.sh run 0.5.0 --skip scan,rehearse
#   ./scripts/release.sh run 0.5.0 --from build --dry-run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=release-tests/lib/versions.sh
source "$SCRIPT_DIR/release-tests/lib/versions.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

# Logs to stderr so --json output on stdout stays machine-parseable.
log()  { echo -e "${BLUE}[release]${NC} $*" >&2; }
ok()   { echo -e "${GREEN}[release] ✓${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[release] ⚠${NC} $*" >&2; }
err()  { echo -e "${RED}${BOLD}[release] ✗${NC} $*" >&2; }

EXIT_GATE=1
EXIT_MISUSE=2
EXIT_PRECONDITION=3
EXIT_ABORT=4

# Stage order. Mirrors `order:` in release-criteria.yaml; the YAML is the
# documentation, this array is what runs.
STAGES=(preflight bump verify test build scan rehearse tag publish smoke promote finish)

# Stages that touch the outside world. Each requires explicit intent even with
# --yes, and each has an `ask` rule in .claude/settings.json.
EXTERNAL_STAGES="tag publish promote finish"

usage() { sed -n '2,32p' "$0" | sed 's/^# \?//'; }

# ───────────────────────────────────────────────────────────── ledger

ledger_dir() { echo "$REPO_ROOT/.release/${1:?version}"; }

ledger_record() {
    local version="$1" stage="$2" status="$3" detail="${4:-}"
    local dir; dir="$(ledger_dir "$version")/steps"
    mkdir -p "$dir"
    {
        echo "status=$status"
        echo "when=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "operator=${USER:-unknown}"
        echo "sha=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
        [[ -n "$detail" ]] && echo "detail=$detail"
    } > "$dir/$stage"
}

ledger_status() {
    local version="$1" stage="$2"
    local f; f="$(ledger_dir "$version")/steps/$stage"
    [[ -f "$f" ]] && grep -m1 '^status=' "$f" | cut -d= -f2 || echo "pending"
}

# ───────────────────────────────────────────────────────────── commands

cmd_status() {
    local version="${1:-}"
    [[ -n "$version" ]] || version="$(ver_to_version)"
    version="$(ver_normalize "$version")"

    if [[ "${JSON_OUT:-false}" == "true" ]]; then
        local first=true
        printf '{"stage":"status","version":"%s","steps":{' "$version"
        for stage in "${STAGES[@]}"; do
            $first || printf ','
            first=false
            printf '"%s":"%s"' "$stage" "$(ledger_status "$version" "$stage")"
        done
        printf '}}\n'
        return 0
    fi

    echo -e "${BOLD}Release $version${NC}"
    echo
    ver_summary
    echo
    printf '  %-12s %s\n' "STAGE" "STATUS"
    for stage in "${STAGES[@]}"; do
        local st; st="$(ledger_status "$version" "$stage")"
        local mark="  "
        case "$st" in
            done)    mark="✓ " ;;
            failed)  mark="✗ " ;;
            skipped) mark="- " ;;
        esac
        printf '  %s%-12s %s\n' "$mark" "$stage" "$st"
    done
}

cmd_explain() {
    local stage="${1:-}"
    [[ -n "$stage" ]] || { err "explain needs a stage name"; return $EXIT_MISUSE; }
    local script
    script="$SCRIPT_DIR/release/$(stage_script "$stage")"
    if [[ ! -f "$script" ]]; then
        err "no script for stage '$stage'"
        return $EXIT_MISUSE
    fi
    echo -e "${BOLD}Stage: $stage${NC}"
    # The stage script's own header comment IS the explanation — one source.
    sed -n '2,/^$/p' "$script" | sed 's/^# \?//'
    if [[ " $EXTERNAL_STAGES " == *" $stage "* ]]; then
        echo
        echo -e "${YELLOW}This stage changes state OUTSIDE this repository.${NC}"
    fi
}

stage_script() {
    case "$1" in
        preflight) echo "10-preflight.sh" ;;
        bump)      echo "20-bump.sh" ;;
        verify)    echo "30-verify.sh" ;;
        *)         echo "" ;;
    esac
}

run_stage() {
    local version="$1" stage="$2"
    local script_name; script_name="$(stage_script "$stage")"

    if [[ -z "$script_name" ]]; then
        warn "stage '$stage' is not implemented yet — skipping"
        ledger_record "$version" "$stage" "skipped" "not implemented"
        return 0
    fi

    local script="$SCRIPT_DIR/release/$script_name"
    [[ -x "$script" ]] || { err "missing or non-executable: $script"; return $EXIT_MISUSE; }

    if [[ " $EXTERNAL_STAGES " == *" $stage "* ]]; then
        warn "stage '$stage' publishes outside this repository"
        if [[ "${ASSUME_YES:-false}" != "true" ]]; then
            err "refusing without --yes (and its .claude/settings.json ask rule)"
            return $EXIT_ABORT
        fi
    fi

    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        log "DRY RUN would execute: $script $version"
        return 0
    fi

    log "── $stage ──"
    local rc=0
    RELEASE_VERSION="$version" JSON_OUT="${JSON_OUT:-false}" "$script" "$version" || rc=$?

    case $rc in
        0)
            ledger_record "$version" "$stage" "done"; ok "$stage" ;;
        "$EXIT_MISUSE")
            ledger_record "$version" "$stage" "failed" "misuse"
            err "$stage: bad invocation" ;;
        "$EXIT_PRECONDITION")
            ledger_record "$version" "$stage" "failed" "precondition"
            err "$stage: a precondition is unmet (see above) — this is not a gate failure" ;;
        *)
            ledger_record "$version" "$stage" "failed" "exit=$rc"
            err "$stage failed (exit $rc)"
            rc=$EXIT_GATE ;;
    esac
    return $rc
}

cmd_run() {
    local version="$1"; shift
    local -a to_run=()
    local started=false

    for stage in "${STAGES[@]}"; do
        [[ -n "$FROM_STAGE" && "$started" == false && "$stage" != "$FROM_STAGE" ]] && continue
        started=true
        [[ ",$SKIP_STAGES," == *",$stage,"* ]] && {
            log "skipping $stage (--skip)"
            ledger_record "$version" "$stage" "skipped" "--skip"
            continue
        }
        [[ -n "$ONLY_STAGES" && ",$ONLY_STAGES," != *",$stage,"* ]] && continue
        to_run+=("$stage")
    done

    [[ ${#to_run[@]} -gt 0 ]] || { err "no stages selected"; return $EXIT_MISUSE; }

    log "stages: ${to_run[*]}"
    for stage in "${to_run[@]}"; do
        run_stage "$version" "$stage" || return $?
    done
    ok "all selected stages complete"
}

# ───────────────────────────────────────────────────────────── arg parsing

COMMAND="${1:-}"; shift || true

SKIP_STAGES=""; ONLY_STAGES=""; FROM_STAGE=""
DRY_RUN=false; JSON_OUT=false; ASSUME_YES=false
POSITIONAL=()

while (( $# > 0 )); do
    case "$1" in
        --skip)    SKIP_STAGES="$2"; shift 2 ;;
        --only)    ONLY_STAGES="$2"; shift 2 ;;
        --from)    FROM_STAGE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        --json)    JSON_OUT=true; shift ;;
        --yes)     ASSUME_YES=true; shift ;;
        -h|--help) usage; exit 0 ;;
        -*)        err "unknown option: $1"; exit $EXIT_MISUSE ;;
        *)         POSITIONAL+=("$1"); shift ;;
    esac
done
export DRY_RUN JSON_OUT ASSUME_YES

case "$COMMAND" in
    ""|help|-h|--help) usage; exit 0 ;;
    status)  cmd_status "${POSITIONAL[0]:-}" ;;
    explain) cmd_explain "${POSITIONAL[0]:-}" ;;
    run)
        [[ ${#POSITIONAL[@]} -ge 1 ]] || { err "run needs a version, e.g. run 0.5.0"; exit $EXIT_MISUSE; }
        cmd_run "$(ver_normalize "${POSITIONAL[0]}")"
        ;;
    preflight|bump|verify|test|build|scan|rehearse|tag|publish|smoke|promote|finish)
        version="${POSITIONAL[0]:-$(ver_to_version)}"
        run_stage "$(ver_normalize "$version")" "$COMMAND"
        ;;
    *)
        err "unknown command: $COMMAND"
        usage
        exit $EXIT_MISUSE
        ;;
esac
