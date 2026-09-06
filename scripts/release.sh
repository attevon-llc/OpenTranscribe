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
#   ./scripts/release.sh reset 0.5.0            # clear the ledger, keep artifacts
#   ./scripts/release.sh explain publish
#   ./scripts/release.sh preflight 0.5.0
#   ./scripts/release.sh run 0.5.0 --skip scan,rehearse
#   ./scripts/release.sh scan 0.5.0 --force-scan "reason recorded in the ledger"
#   ./scripts/release.sh run 0.5.0 --from build --dry-run
#   ./scripts/release.sh run 0.5.1 --patch      # from release/0.5 — see releasing.md's
#                                                # "Cutting a patch release"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=release-tests/lib/versions.sh
source "$SCRIPT_DIR/release-tests/lib/versions.sh"
# shellcheck source=release/patch-lib.sh
source "$SCRIPT_DIR/release/patch-lib.sh"

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
        # `[[ -n "$detail" ]] && echo ...` would make this group return 1 when
        # detail is empty (the common case), and under `set -e` that aborted the
        # whole run immediately after a SUCCESSFUL stage — the success line never
        # printed and release.sh exited 1 on a fully passing preflight.
        if [[ -n "$detail" ]]; then echo "detail=$detail"; fi
    } > "$dir/$stage"
    return 0
}

ledger_status() {
    local version="$1" stage="$2"
    local f; f="$(ledger_dir "$version")/steps/$stage"
    [[ -f "$f" ]] && grep -m1 '^status=' "$f" | cut -d= -f2 || echo "pending"
}

# ───────────────────────────────────────────────────────────── commands

# Clear a version's ledger so the next run starts from a known state.
#
# Needed because a ledger accumulates rehearsal history: stages that failed for
# reasons since fixed, stages recorded "skipped — not implemented" before they
# existed, and stages still "pending" whose work was done by hand. A status
# table that reports any of those as current is worse than no table, and this
# repository has spent enough effort on records that quietly went stale.
#
# Only ever touches .release/<version>/ — never an artifact, image, or tag.
cmd_reset() {
    local version="${1:-}"
    [[ -n "$version" ]] || version="$(ver_to_version)"
    version="$(ver_normalize "$version")"
    local dir; dir="$(ledger_dir "$version")"

    if [[ ! -d "$dir" ]]; then
        ok "no ledger for $version — nothing to reset"
        return 0
    fi

    log "this clears the release ledger for $version:"
    local s
    for s in "${STAGES[@]}"; do
        printf '    %-10s %s\n' "$s" "$(ledger_status "$version" "$s")" >&2
    done
    warn "artifacts, images and tags are NOT touched — only the record"

    if [[ "${ASSUME_YES:-false}" != "true" ]]; then
        read -r -p "Clear the ledger for $version? [y/N] " reply
        [[ "$reply" == "y" || "$reply" == "Y" ]] || { err "aborted"; return $EXIT_ABORT; }
    fi

    rm -rf "${dir:?}/steps"
    ok "ledger cleared for $version"
}

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
            done)       mark="✓ " ;;
            failed)     mark="✗ " ;;
            aborted)    mark="⊘ " ;;
            overridden) mark="! " ;;
            skipped)    mark="- " ;;
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
        build)     echo "40-build.sh" ;;
        scan)      echo "50-scan.sh" ;;
        test)      echo "60-test.sh" ;;
        rehearse)  echo "65-rehearse.sh" ;;
        tag)       echo "70-tag.sh" ;;
        publish)   echo "80-publish.sh" ;;
        smoke)     echo "85-smoke.sh" ;;
        promote)   echo "90-promote.sh" ;;
        finish)    echo "95-finish.sh" ;;
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
            # A patch's --patch waived the rehearsal scenarios rather than
            # actually running them — record why, distinct from `--skip`
            # (detail=--skip) and `--force-rehearse` (status=overridden). Only
            # `rehearse` can carry this: OT_PATCH_SKIP_REASON is set by
            # patch_prepare() specifically to control 65-rehearse.sh's own
            # scenario guard, and is empty for every other stage.
            local done_detail=""
            if [[ "$stage" == "rehearse" && -n "${OT_PATCH_SKIP_REASON:-}" ]]; then
                done_detail="patch-rehearsal-waived: ${OT_PATCH_SKIP_REASON}"
            fi
            ledger_record "$version" "$stage" "done" "$done_detail"; ok "$stage" ;;
        "$EXIT_MISUSE")
            ledger_record "$version" "$stage" "failed" "misuse"
            err "$stage: bad invocation" ;;
        "$EXIT_PRECONDITION")
            ledger_record "$version" "$stage" "failed" "precondition"
            err "$stage: a precondition is unmet (see above) — this is not a gate failure" ;;
        "$EXIT_ABORT")
            # An abort means the operator declined the stage's own confirmation
            # prompt (e.g. rehearse's `I UNDERSTAND` gate) — nothing ran, so there
            # is nothing to accept or override. That is a different fact from a
            # gate that ran and found a real regression (the `*` branch below),
            # and the ledger must say so: `status=aborted`, never `status=failed`,
            # or a declined prompt reads identically to a broken release.
            #
            # Deliberately, `--force-<stage>` does NOT apply here. Forcing past a
            # FAILURE means "a human reviewed the regression and accepts the
            # risk" — that is a real decision to record. Forcing past an ABORT
            # would only mean "pretend the operator answered a prompt they in
            # fact declined", which is not a decision, it's a fiction. The
            # correct recovery for an abort is simply to run the stage again and
            # answer the prompt (or pass --yes upstream) — never to force it.
            if [[ -n "${FORCE_REASON[$stage]:-}" ]]; then
                warn "$stage was ABORTED (operator declined a confirmation prompt), not failed"
                warn "  --force-$stage does not apply to an abort — only to a real failure"
                warn "  re-run '$stage' and answer the prompt (or pass --yes) instead of forcing"
            fi
            ledger_record "$version" "$stage" "aborted" "exit=$rc; operator=${USER:-unknown}"
            err "$stage aborted by operator (exit $rc) — nothing ran" ;;
        *)
            # An overridden gate still FAILED. The distinction the ledger records
            # is that a named operator accepted the failure and said why -- the
            # Fortune-100 posture is not "no exceptions", it is "no UNDOCUMENTED
            # exceptions". A reason is mandatory, so an override cannot be a
            # reflex; there is no bare --force.
            if [[ -n "${FORCE_REASON[$stage]:-}" ]]; then
                ledger_record "$version" "$stage" "overridden" \
                    "exit=$rc; operator=${USER:-unknown}; reason=${FORCE_REASON[$stage]}"
                warn "$stage FAILED (exit $rc) and was overridden by ${USER:-unknown}"
                warn "  reason: ${FORCE_REASON[$stage]}"
                warn "  this is recorded in the ledger and the release readiness report"
                rc=0
            else
                ledger_record "$version" "$stage" "failed" "exit=$rc"
                err "$stage failed (exit $rc)"
                rc=$EXIT_GATE
            fi ;;
    esac
    return $rc
}

# Resolves --patch exactly once, for whichever arm invoked it (`run`, which
# loops every stage itself, or a single-stage command that calls run_stage
# directly) — see scripts/release/patch-lib.sh for why "is this a patch" has
# to be one answer rather than three.
#
# Exit MISUSE (2) when the delta is not a patch at all: nothing about the
# release was evaluated yet, so `--patch` on e.g. a minor bump is a wrong
# invocation, not a gate that ran and failed.
#
# A patch delta whose diff does NOT satisfy the (widened) waiver trigger set is
# NOT a misuse — OT_PATCH_SKIP_REASON simply stays unset and `rehearse` runs in
# full, identical to not passing --patch at all.
patch_prepare() {
    local version="$1"
    [[ "$PATCH_MODE" == "true" ]] || return 0

    local base kind
    base="$(patch_base_tag "$version")" || base=""
    kind="$(patch_release_kind "$version" "$base")"
    if [[ "$kind" != "patch" ]]; then
        err "--patch: $version is a '$kind' release relative to ${base:-<no base tag found>} — not a patch"
        exit $EXIT_MISUSE
    fi

    local reason
    if reason="$(patch_rehearsal_waivable "$version")"; then
        log "--patch: rehearsal waivable — $reason"
        export OT_PATCH_SKIP_REASON="$reason"
    else
        warn "--patch: rehearsal NOT waivable for $version vs $base — it will run in full"
    fi
}

cmd_run() {
    local version="$1"; shift
    patch_prepare "$version"
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
DRY_RUN=false; JSON_OUT=false; ASSUME_YES=false; PATCH_MODE=false
POSITIONAL=()
# stage -> the reason its failure was accepted. Consulted by run_stage.
declare -A FORCE_REASON=()

while (( $# > 0 )); do
    case "$1" in
        --skip)    SKIP_STAGES="$2"; shift 2 ;;
        --only)    ONLY_STAGES="$2"; shift 2 ;;
        --from)    FROM_STAGE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        --json)    JSON_OUT=true; shift ;;
        --yes)     ASSUME_YES=true; shift ;;
        --patch)   PATCH_MODE=true; shift ;;
        # --force-<stage> "reason". The reason is REQUIRED: an override with no
        # recorded justification is the thing this whole mechanism exists to
        # prevent, so there is deliberately no bare --force.
        --force-*)
            _fstage="${1#--force-}"
            if [[ -z "$(stage_script "$_fstage")" ]]; then
                err "--force-${_fstage}: '${_fstage}' is not a stage"
                exit $EXIT_MISUSE
            fi
            if [[ $# -lt 2 || -z "${2:-}" || "${2:0:1}" == "-" ]]; then
                err "--force-${_fstage} requires a reason: --force-${_fstage} \"why this is acceptable\""
                exit $EXIT_MISUSE
            fi
            FORCE_REASON["$_fstage"]="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*)        err "unknown option: $1"; exit $EXIT_MISUSE ;;
        *)         POSITIONAL+=("$1"); shift ;;
    esac
done
export DRY_RUN JSON_OUT ASSUME_YES

case "$COMMAND" in
    ""|help|-h|--help) usage; exit 0 ;;
    status)  cmd_status "${POSITIONAL[0]:-}" ;;
    reset)   cmd_reset "${POSITIONAL[0]:-}" ;;
    explain) cmd_explain "${POSITIONAL[0]:-}" ;;
    run)
        [[ ${#POSITIONAL[@]} -ge 1 ]] || { err "run needs a version, e.g. run 0.5.0"; exit $EXIT_MISUSE; }
        cmd_run "$(ver_normalize "${POSITIONAL[0]}")"
        ;;
    preflight|bump|verify|test|build|scan|rehearse|tag|publish|smoke|promote|finish)
        version="${POSITIONAL[0]:-$(ver_to_version)}"
        version="$(ver_normalize "$version")"
        patch_prepare "$version"
        run_stage "$version" "$COMMAND"
        ;;
    *)
        err "unknown command: $COMMAND"
        usage
        exit $EXIT_MISUSE
        ;;
esac
