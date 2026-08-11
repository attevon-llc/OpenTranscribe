#!/bin/bash
# Verify — the fast gate. Everything checkable without Docker images or a build.
#
# Safe to run with the live stack up, and fast enough (well under a minute) to
# run repeatedly while preparing a release. Its job is to make the slow stages
# rare: nothing here should ever be discovered at hour three.
#
# Exit: 0 pass · 1 a blocking check failed

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0; RESULTS=()

record() {
    local id="$1" status="$2" detail="${3:-}" fix="${4:-}"
    case "$status" in
        pass) PASS=$((PASS+1)); echo -e "  ${GREEN}PASS${NC}  $id" >&2 ;;
        warn) WARN=$((WARN+1)); echo -e "  ${YELLOW}WARN${NC}  $id  $detail" >&2 ;;
        fail) FAIL=$((FAIL+1)); echo -e "  ${RED}FAIL${NC}  $id  $detail" >&2
              [[ -n "$fix" ]] && echo -e "        fix: $fix" >&2 ;;
    esac
    RESULTS+=("$(printf '{"id":"%s","status":"%s","detail":"%s","fix":"%s"}' \
        "$id" "$status" "${detail//\"/\'}" "${fix//\"/\'}")")
}

echo "Verify ${VERSION:-<version from VERSION file>}" >&2

# Version sources, Alembic single head, Dockerfile build-arg contract, blog slugs
if python3 scripts/release/check-version-consistency.py --mode pre-tag >/dev/null 2>&1; then
    record version-consistency pass
else
    record version-consistency fail "see the checker output" \
        "python3 scripts/release/check-version-consistency.py --mode pre-tag"
fi

# Every documented deployment permutation
if ./scripts/validate-deployments.sh >/dev/null 2>&1; then
    record deployment-matrix pass
else
    record deployment-matrix fail "a permutation is invalid" "./scripts/validate-deployments.sh"
fi

# Release-artifact manifest: every listed path must exist, or a pinned install
# 404s for every user on that tag.
if [[ -f release-manifest.txt ]]; then
    missing=()
    while IFS= read -r line; do
        case "$line" in ''|'#'*) continue ;; esac
        path="$(printf '%s' "$line" | cut -f1 | tr -d '[:space:]')"
        [[ -n "$path" && ! -e "$path" ]] && missing+=("$path")
    done < release-manifest.txt
    if [[ ${#missing[@]} -eq 0 ]]; then
        record release-manifest pass
    else
        record release-manifest fail "missing: ${missing[*]}" "fix the paths in release-manifest.txt"
    fi
else
    record release-manifest fail "release-manifest.txt not found"
fi

# Structural gates that need no service
if backend/venv/bin/pytest -o addopts="" -q \
        backend/tests/unit/test_alembic_chain.py \
        backend/tests/unit/test_model_registration.py \
        backend/tests/unit/test_release_manifest.py \
        backend/tests/unit/test_install_upgrade_scripts.py \
        backend/tests/unit/test_env_example_coverage.py >/dev/null 2>&1; then
    record structural-tests pass
else
    record structural-tests fail "alembic chain / models / manifest / install+upgrade scripts / env coverage" \
        "backend/venv/bin/pytest -o addopts='' backend/tests/unit/test_alembic_chain.py backend/tests/unit/test_model_registration.py backend/tests/unit/test_release_manifest.py backend/tests/unit/test_env_example_coverage.py"
fi

# Docs build — the only thing that catches a duplicate blog slug or an undefined
# author/tag, and deploy-docs.yml does NOT run on tag pushes.
if [[ "${SKIP_DOCS_BUILD:-false}" == "true" ]]; then
    record docs-build warn "skipped (SKIP_DOCS_BUILD=true)"
elif [[ -d docs-site/node_modules ]]; then
    if (cd docs-site && npm run build >/dev/null 2>&1); then
        record docs-build pass
    else
        record docs-build fail "docs-site build failed" "cd docs-site && npm run build"
    fi
else
    record docs-build warn "docs-site/node_modules absent" "cd docs-site && npm ci"
fi

if [[ "$JSON_OUT" == "true" ]]; then
    joined=$(IFS=,; echo "${RESULTS[*]}")
    next='["proceed to test/build"]'
    [[ $FAIL -gt 0 ]] && next='["fix the failing criteria, then re-run verify"]'
    printf '{"stage":"verify","version":"%s","status":"%s","criteria":[%s],"next":%s}\n' \
        "${VERSION:-unknown}" "$([[ $FAIL -eq 0 ]] && echo pass || echo fail)" "$joined" "$next"
fi

echo >&2
echo "—— $PASS passed, $FAIL failed, $WARN warnings" >&2
[[ $FAIL -eq 0 ]] || exit 1
exit 0
