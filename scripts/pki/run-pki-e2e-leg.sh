#!/bin/bash
#
# scripts/pki/run-pki-e2e-leg.sh — the PKI/mTLS leg of the full test matrix (leg `3-pki`),
# executed end to end.
#
# It exists so scripts/test-matrix.sh can RUN that leg instead of printing it. The matrix
# dispatcher owns no test logic by design, and this leg is the one that needs real
# orchestration: three commands from full-test-matrix.md's "PKI/mTLS is prod+nginx ONLY"
# section, around a stack that is neither the dev stack nor a release-test stack.
#
#   ./scripts/pki/setup-test-pki.sh
#   ./opentr.sh start prod --build --with-pki
#   RUN_PKI_E2E=true pytest backend/tests/e2e/test_pki.py -v
#
# WHY PROD: Vite cannot terminate mTLS, so there is no dev-mode PKI variant and none should
# be invented (that sentence is in the doc verbatim). nginx does the client-cert termination,
# which only the prod+nginx+pki overlay chain provides.
#
# Exit codes — the STANDARD contract shared with scripts/release.sh and scripts/test-matrix.sh:
#   0 pass · 1 gate failed · 2 misuse · 3 precondition unmet · 4 operator abort
#
# Usage: scripts/pki/run-pki-e2e-leg.sh [--yes] [--keep-stack]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

EXIT_GATE=1
EXIT_MISUSE=2
EXIT_PRECONDITION=3
EXIT_ABORT=4

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "$*" >&2; }
err()  { echo -e "${RED}error:${NC} $*" >&2; }

ASSUME_YES=false
KEEP_STACK=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes)        ASSUME_YES=true; shift ;;
        --keep-stack) KEEP_STACK=true; shift ;;
        -h|--help)    sed -n '3,22p' "$0"; exit 0 ;;
        *) err "unknown option: $1"; exit $EXIT_MISUSE ;;
    esac
done

VENV_PY="$REPO_ROOT/backend/venv/bin/python"
[[ -x "$VENV_PY" ]] || {
    err "backend/venv is missing — this leg runs pytest from it (see backend/CLAUDE.md)"
    exit $EXIT_PRECONDITION
}

# This leg starts a PROD stack under the stock opentranscribe-* names, so it needs the field
# clear. Any release-test scenario run earlier in the same stage leaves ITS stack up on purpose
# (run standalone, you want to poke at what you just installed), so tear those down first — the
# same reasoning scripts/release/65-rehearse.sh uses to justify tearing Scenario A down before
# Scenario B: these are containers this stage created minutes ago, not an operator's deployment.
#
# `--cleanup` only removes resources carrying the com.opentranscribe.release-test label and
# re-checks lib/guardrails.sh's path allowlist before removing anything, so it cannot reach the
# production volumes.
if docker ps -a --format '{{.Names}}' | grep -q '^opentranscribe-'; then
    if [[ "$ASSUME_YES" != "true" ]]; then
        err "opentranscribe-* containers exist and --yes was not given; refusing to tear them down"
        info "  Inspect them, then re-run with --yes, or clear them yourself:"
        info "    ./scripts/release-tests/test-fresh-install.sh --cleanup --yes"
        info "    ./scripts/release-tests/test-upgrade.sh --cleanup --yes"
        exit $EXIT_ABORT
    fi
    info "${YELLOW}==>${NC} clearing release-test stacks so the PKI prod stack can bind the stock names/ports"
    ./scripts/release-tests/test-fresh-install.sh --cleanup --yes >/dev/null 2>&1 || true
    ./scripts/release-tests/test-upgrade.sh --cleanup --yes >/dev/null 2>&1 || true
    ./scripts/release-tests/test-lite-mode.sh --cleanup --yes >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do
        docker ps -a --format '{{.Names}}' | grep -q '^opentranscribe-' || break
        sleep 2
    done
    if docker ps -a --format '{{.Names}}' | grep -q '^opentranscribe-'; then
        err "opentranscribe-* containers remain after cleanup; the PKI stack cannot start"
        docker ps -a --format '  {{.Names}}\t{{.Status}}' | grep '^  opentranscribe-' >&2
        exit $EXIT_PRECONDITION
    fi
fi

rc=0

info "${BLUE}==>${NC} generating the test PKI (CA + client certs)"
if ! ./scripts/pki/setup-test-pki.sh; then
    err "setup-test-pki.sh failed — no CA/client certs to authenticate with"
    exit $EXIT_GATE
fi

info "${BLUE}==>${NC} starting the prod+nginx+PKI stack (builds prod images; several minutes)"
if ! ./opentr.sh start prod --build --with-pki; then
    err "./opentr.sh start prod --build --with-pki failed"
    rc=$EXIT_GATE
fi

if [[ $rc -eq 0 ]]; then
    # nginx terminates mTLS on 5182; wait for it rather than racing the build's tail.
    info "${BLUE}==>${NC} waiting for the mTLS listener on :5182"
    deadline=$(( $(date +%s) + 600 ))
    until curl -sk --max-time 5 "https://localhost:5182/" >/dev/null 2>&1; do
        if [[ $(date +%s) -ge $deadline ]]; then
            err "https://localhost:5182 never answered within 10 min"
            rc=$EXIT_GATE
            break
        fi
        sleep 5
    done
fi

if [[ $rc -eq 0 ]]; then
    info "${BLUE}==>${NC} running the PKI e2e suite"
    # RUN_PKI_E2E is the module-level gate on backend/tests/e2e/test_pki.py. Without it the
    # file skips wholesale and the leg would report a pass having run nothing.
    if RUN_PKI_E2E=true "$VENV_PY" -m pytest backend/tests/e2e/test_pki.py -v; then
        info "${GREEN}==>${NC} PKI e2e passed"
    else
        err "PKI e2e failed"
        rc=$EXIT_GATE
    fi
fi

if [[ "$KEEP_STACK" != "true" ]]; then
    info "${YELLOW}==>${NC} stopping the PKI prod stack"
    ./opentr.sh stop >/dev/null 2>&1 || true
else
    info "${YELLOW}==>${NC} --keep-stack: leaving the PKI prod stack up (stop it with ./opentr.sh stop)"
fi

exit $rc
