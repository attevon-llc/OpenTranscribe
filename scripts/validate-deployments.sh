#!/bin/bash
# Validate that every documented deployment permutation produces a compose
# configuration Docker will actually accept.
#
# WHY
#
# There are ~20 supported deployment shapes and nothing checked them. The last
# audit was done by hand and found four genuinely broken ones (CHANGELOG:420):
# gpu-split missing image/build in both overlays, offline and bench both missing
# celery-redaction, lite duplicating ~30 env vars behind a non-existent external
# network, and pki-dev clashing with Vite on a host port. Those had all shipped.
#
# HOW
#
# The overlay chain is NOT re-declared here. Each permutation is resolved by
# asking opentr.sh itself via `--dry-run` and parsing the chain it prints, so the
# matrix cannot drift from the launcher. That is also why opentr.sh's dry-run had
# to be made side-effect free first — it previously ran create_required_dirs,
# fix_model_cache_permissions and ensure_opensearch_models before the
# short-circuit, and the last will `docker pull` a multi-GB image on a cold cache.
#
# ANTI-STALENESS
#
# A matrix that silently stops covering things is the problem it was meant to
# solve. This parses the deployment table in the docs and FAILS when a documented
# deployment flag has no matrix entry here.
#
# Fast by construction: `config -q` only resolves and validates YAML. No images
# are pulled and no containers start, so the whole matrix runs in well under a
# minute and belongs in CI and pre-commit rather than the multi-hour release gate.
#
# Usage:
#   ./scripts/validate-deployments.sh              # all permutations
#   ./scripts/validate-deployments.sh --list       # show the matrix, run nothing
#   ./scripts/validate-deployments.sh --only dev   # substring filter on the name
#   ./scripts/validate-deployments.sh --json       # machine-readable summary

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=release-tests/lib/assertions.sh
source "$SCRIPT_DIR/release-tests/lib/assertions.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

DOC_TABLE="docs-site/docs/operations/deployment-configuration.md"
FRESH_NAME="validate"

MODE_LIST=false
MODE_JSON=false
FILTER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --list) MODE_LIST=true; shift ;;
        --json) MODE_JSON=true; shift ;;
        --only) FILTER="$2"; shift 2 ;;
        -h|--help) sed -n '2,35p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------- the matrix
#
# name|opentr.sh arguments
#
# Includes combinations that have historically broken, not just the single-flag
# rows from the docs — a chain is only as valid as the way people actually
# compose it.
DEPLOYMENTS=(
    "dev|start dev"
    "dev-cpu|start dev --cpu"
    "dev-lite|start dev --lite"
    "dev-no-nas|start dev --no-nas"
    "dev-gpu-scale|start dev --gpu-scale"
    "dev-gpu-split|start dev --with-gpu-split"
    "dev-diar-native|start dev --with-diar-native"
    "dev-monitoring|start dev --with-monitoring"
    "dev-watch|start dev --with-watch"
    "dev-backup|start dev --with-backup"
    "dev-ldap-test|start dev --with-ldap-test"
    "dev-keycloak-test|start dev --with-keycloak-test"
    "dev-authentik-test|start dev --with-authentik-test"
    "dev-smb-test|start dev --with-smb-test"
    "dev-mock-llm|start dev --with-mock-llm"
    "dev-fresh|start dev --fresh ${FRESH_NAME} --port-offset 40"
    "dev-lite-monitoring|start dev --lite --with-monitoring"
    "prod|start prod"
    "prod-pki|start prod --with-pki"
    "prod-nginx-pki|start prod --with-pki --no-nas"
    "prod-gpu-split|start prod --with-gpu-split"
)

# Documented deployments that cannot be validated through `opentr.sh start`, with
# the reason. These have no flag in the doc table's command column, so they are
# not reachable by the flag-based coverage check below; recorded here so the
# omission is a decision rather than an oversight.
#
#   Offline / air-gapped — built by scripts/build-offline-package.sh; its compose
#     file references images that only exist inside the package tarball.
#   Benchmark — the otbench project is orchestrated by scripts/run_benchmark.py,
#     not by `opentr.sh start`.
#   NAS / NVMe storage — covered implicitly: the NAS overlay is auto-loaded in
#     every row that does not pass --no-nas.

# ------------------------------------------------------------------ env file
#
# Prefer the real .env so we validate what this machine actually runs. Fall back
# to .env.example (CI has no .env).
#
# `--env-file` alone is NOT enough, which is why every permutation failed in CI
# while passing locally. It sets the file used for ${VAR} interpolation, but the
# compose files ALSO declare a per-service `env_file: .env` (10+ times in
# docker-compose.yml), and that path is resolved relative to the project
# directory regardless of --env-file. With no ./.env, compose rejects every
# service before validating anything:
#
#   env file /home/runner/work/OpenTranscribe/.env not found
#
# So a repo-local .env has to exist for the duration of the run. It is created
# ONLY when absent — there is nothing to clobber in that case — and removed
# again on exit. A developer's real .env is never touched or read past this
# point.
ENV_FILE=""
TMP_ENV=""
CREATED_REPO_ENV=false
if [[ -f .env ]]; then
    ENV_FILE=".env"
else
    TMP_ENV="$(mktemp /tmp/ot-validate-env-XXXXXX)"
    cp .env.example "$TMP_ENV"
    ENV_FILE="$TMP_ENV"
    cp .env.example .env
    CREATED_REPO_ENV=true
    echo -e "${YELLOW}No .env — validating against .env.example${NC}" >&2
    echo -e "${YELLOW}  (a temporary ./.env was created for compose's per-service env_file; removed on exit)${NC}" >&2
fi

cleanup() {
    [[ -n "$TMP_ENV" ]] && rm -f "$TMP_ENV"
    # Only ever removes a .env this script created itself.
    [[ "$CREATED_REPO_ENV" == "true" ]] && rm -f .env
    # --dry-run creates only the generated fresh overlay, never containers.
    rm -f ".fresh/${FRESH_NAME}.yml" ".fresh/${FRESH_NAME}.offset" ".fresh/${FRESH_NAME}.aux"
    return 0
}
trap cleanup EXIT

# ------------------------------------------------------------------ resolving

# Ask opentr.sh for the compose chain it WOULD use. Parses the block:
#
#    Compose files:
#      - docker-compose.yml
#      - docker-compose.override.yml
resolve_chain() {
    local args="$1" out
    # shellcheck disable=SC2086  # deliberate word-splitting of the argument string
    if ! out="$(./opentr.sh $args --dry-run 2>&1)"; then
        echo "DRY_RUN_FAILED"
        printf '%s\n' "$out" >&2
        return 1
    fi
    printf '%s\n' "$out" \
        | sed -n '/Compose files:/,/Command that WOULD run/p' \
        | sed -n 's/^[[:space:]]*-[[:space:]]*\(.*\)$/\1/p'
}

# --------------------------------------------------------------------- checks

# Coverage is checked on FLAGS, not on the human-readable deployment names.
#
# Names are prose ("Production", "GPU scale (dual-GPU)") and fuzzy-matching them
# against matrix row names produces false alarms. The flag in the documented
# command IS the deployment shape — `--with-pki` selects an overlay, "PKI / mTLS"
# does not — so an undocumented-but-validated flag is fine, while a
# documented-but-unvalidated flag is exactly the gap worth failing on.
check_documented_coverage() {
    # A MISSING INPUT IS NOT A PASS. This recorded PASS with the reason "coverage check
    # skipped", so deleting, moving or renaming the doc table silently disabled the only
    # check that catches a documented deployment flag nobody validates — and the run stayed
    # green. That is the same shape as the mutation ratchet exiting 0 with no logs, and as
    # 30-verify.sh degrading to `warn` when a gitignored node_modules was absent. The file is
    # tracked in git, so its absence is a repo defect, not an environment quirk: fail.
    [[ -f "$DOC_TABLE" ]] || {
        as_record FAIL "doc table missing at $DOC_TABLE" \
            "coverage cannot be checked; restore the file or update DOC_TABLE"
        return
    }

    local matrix_args=""
    for entry in "${DEPLOYMENTS[@]}"; do
        matrix_args+=" ${entry#*|}"
    done

    # Flags appearing in the doc table's `./opentr.sh ...` command cells.
    local doc_flags uncovered=()
    doc_flags="$(grep -o '`\./opentr\.sh[^`]*`' "$DOC_TABLE" \
        | grep -o -- '--[a-z][a-z0-9-]*' \
        | sort -u)"

    local flag
    while IFS= read -r flag; do
        [[ -n "$flag" ]] || continue
        # --build affects how images are produced, not which overlays load, so it
        # cannot change the resolved chain. Same for the --fresh companion flags,
        # which are exercised by the dev-fresh row.
        case "$flag" in --build|--port-offset|--no-nas|--nas) continue ;; esac
        [[ "$matrix_args" == *"$flag"* ]] || uncovered+=("$flag")
    done <<< "$doc_flags"

    # Both base modes must be represented.
    [[ "$matrix_args" == *"start dev"* ]]  || uncovered+=("start dev")
    [[ "$matrix_args" == *"start prod"* ]] || uncovered+=("start prod")

    if [[ ${#uncovered[@]} -eq 0 ]]; then
        as_record PASS "every documented deployment flag is validated"
    else
        as_record FAIL "documented deployment flags with no matrix coverage" \
            "${uncovered[*]} — add a DEPLOYMENTS row (or document why it cannot be validated)"
    fi
}

validate_one() {
    local name="$1" args="$2"
    local chain=() file compose_args=()

    if ! mapfile -t chain < <(resolve_chain "$args"); then
        as_record FAIL "$name" "opentr.sh --dry-run failed"
        return 1
    fi
    if [[ ${#chain[@]} -eq 0 ]]; then
        as_record FAIL "$name" "resolved an empty compose chain"
        return 1
    fi

    for file in "${chain[@]}"; do
        [[ -n "$file" ]] || continue
        if [[ ! -f "$file" ]]; then
            as_record FAIL "$name" "chain references a missing file: $file"
            return 1
        fi
        compose_args+=(-f "$file")
    done

    local err
    if err="$(docker compose "${compose_args[@]}" --env-file "$ENV_FILE" config -q 2>&1)"; then
        as_record PASS "$name (${#chain[@]} files)"
        return 0
    fi
    as_record FAIL "$name" "$(printf '%s' "$err" | head -3 | tr '\n' ' ')"
    return 1
}

# ----------------------------------------------------------------------- main

if $MODE_LIST; then
    printf '%-24s %s\n' "NAME" "ARGS"
    for entry in "${DEPLOYMENTS[@]}"; do
        printf '%-24s %s\n' "${entry%%|*}" "${entry#*|}"
    done
    exit 0
fi

echo -e "${BLUE}Validating ${#DEPLOYMENTS[@]} deployment permutations (env: $ENV_FILE)${NC}"
echo

failed=0
for entry in "${DEPLOYMENTS[@]}"; do
    name="${entry%%|*}"
    args="${entry#*|}"
    [[ -n "$FILTER" && "$name" != *"$FILTER"* ]] && continue
    validate_one "$name" "$args" || failed=$((failed + 1))
done

check_documented_coverage

echo
if $MODE_JSON; then
    # as_pass / as_fail are counters owned by lib/assertions.sh (sourced above).
    # shellcheck disable=SC2154
    printf '{"stage":"validate-deployments","status":"%s","passed":%d,"failed":%d}\n' \
        "$([[ $as_fail -eq 0 ]] && echo pass || echo fail)" "$as_pass" "$as_fail"
fi

if as_summary; then
    echo -e "${GREEN}All deployment permutations produce a valid compose config.${NC}"
    exit 0
else
    echo -e "${RED}One or more deployment permutations are broken.${NC}"
    exit 1
fi
