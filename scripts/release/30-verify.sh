#!/bin/bash
# Verify — the fast gate. Everything checkable without Docker images or a build.
#
# Safe to run with the live stack up, and fast enough (well under a minute) to
# run repeatedly while preparing a release. Its job is to make the slow stages
# rare: nothing here should ever be discovered at hour three.
#
# Exit: 0 pass · 1 a blocking check failed · 2 this script and
#       release-criteria.yaml disagree about which criteria exist · 3
#       precondition unmet

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0; RESULTS=()

# ── Criteria ───────────────────────────────────────────────────────────────
# Severity comes from release-criteria.yaml; this script only decides whether
# each check passed. See 10-preflight.sh for the reasoning — and for the drift
# that made it necessary (`docs-build` was `blocking` there and `warn` here).
STAGE_ID=verify
CRITERIA_YAML=scripts/release/release-criteria.yaml
CRITERIA_PY=python3
[[ -x backend/venv/bin/python ]] && CRITERIA_PY=backend/venv/bin/python

declare -A SEVERITY=()
declare -A RECORDED=()

while IFS='|' read -r _id _sev; do
    [[ -n "$_id" ]] && SEVERITY["$_id"]="$_sev"
done < <("$CRITERIA_PY" - "$CRITERIA_YAML" "$STAGE_ID" <<'PY'
import sys

try:
    import yaml
except ModuleNotFoundError:
    sys.stderr.write("PyYAML is needed to read the release criteria\n")
    raise SystemExit(1)

path, stage = sys.argv[1], sys.argv[2]
with open(path) as handle:
    doc = yaml.safe_load(handle)
try:
    criteria = doc["stages"][stage]["criteria"]
except (KeyError, TypeError):
    sys.stderr.write(f"{path} defines no criteria for stage '{stage}'\n")
    raise SystemExit(1)
for criterion in criteria:
    print(f"{criterion['id']}|{criterion.get('severity', 'blocking')}")
PY
)

if [[ ${#SEVERITY[@]} -eq 0 ]]; then
    echo -e "${RED}cannot read $CRITERIA_YAML — it defines the severity of every check below.${NC}" >&2
    echo "  fix: $CRITERIA_PY -c 'import yaml'   # install PyYAML, or repair the file" >&2
    exit 3
fi

# record ID OUTCOME [DETAIL] [FIX] [waived]  — see 10-preflight.sh.
record() {
    local id="$1" outcome="$2" detail="${3:-}" fix="${4:-}" waived="${5:-}"
    local severity="${SEVERITY[$id]:-}"

    # The vocabulary is exactly three words. `warn` is NOT one of them: whether a
    # failure warns or blocks is the criteria file's decision, not the caller's,
    # and letting a caller say `warn` is how the two drifted apart before.
    case "$outcome" in
        pass|fail|not-measured) ;;
        *) echo -e "${RED}record: '$outcome' is not a valid outcome for '$id' (pass|fail|not-measured)${NC}" >&2
           exit 2 ;;
    esac

    if [[ -z "$severity" ]]; then
        echo -e "${RED}drift: '$id' is not a criterion of stage '$STAGE_ID' in $CRITERIA_YAML${NC}" >&2
        exit 2
    fi
    if [[ "$waived" == "waived" ]]; then severity=warn; fi
    RECORDED["$id"]=1

    local status
    case "$outcome" in
        pass) status=pass ;;
        *)    [[ "$severity" == "blocking" ]] && status=fail || status=warn ;;
    esac

    case "$outcome:$status" in
        pass:*)
            PASS=$((PASS+1)); echo -e "  ${GREEN}PASS${NC}  $id" >&2 ;;
        not-measured:warn)
            WARN=$((WARN+1))
            echo -e "  ${YELLOW}⊘ NOT MEASURED${NC}  $id  $detail (non-blocking)" >&2 ;;
        not-measured:fail)
            FAIL=$((FAIL+1))
            echo -e "  ${RED}⊘ NOT MEASURED${NC}  $id  $detail — proves nothing, not counted as a pass" >&2
            [[ -n "$fix" ]] && echo -e "        fix: $fix" >&2 ;;
        *:warn)
            WARN=$((WARN+1)); echo -e "  ${YELLOW}WARN${NC}  $id  $detail" >&2 ;;
        *)
            FAIL=$((FAIL+1)); echo -e "  ${RED}FAIL${NC}  $id  $detail" >&2
            [[ -n "$fix" ]] && echo -e "        fix: $fix" >&2 ;;
    esac

    RESULTS+=("$(printf '{"id":"%s","status":"%s","outcome":"%s","severity":"%s","detail":"%s","fix":"%s"}' \
        "$id" "$status" "$outcome" "$severity" "${detail//\"/\'}" "${fix//\"/\'}")")
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
#
# `docs-site/node_modules` is gitignored, so "absent" is the state of EVERY
# fresh checkout and every worktree. Recording that as `warn` meant the check
# this comment calls "the only thing that catches a duplicate blog slug" was
# skipped by default and verify still exited 0 — a pass that measured nothing.
# It is now NOT MEASURED against a blocking criterion, which stops the stage.
# Two ways past it, both deliberate: install the deps, or opt out explicitly
# with SKIP_DOCS_BUILD=true (recorded as an opt-out, still never a pass).
if [[ "${SKIP_DOCS_BUILD:-false}" == "true" ]]; then
    record docs-build not-measured "SKIP_DOCS_BUILD=true — the operator opted out" \
        "unset SKIP_DOCS_BUILD to measure it" waived
elif [[ ! -d docs-site/node_modules ]]; then
    record docs-build not-measured "docs-site/node_modules absent, so the docs were never built" \
        "npm --prefix docs-site ci   (or SKIP_DOCS_BUILD=true to opt out on the record)"
elif (cd docs-site && npm run build >/dev/null 2>&1); then
    record docs-build pass
else
    record docs-build fail "docs-site build failed" "cd docs-site && npm run build"
fi

# ── The criteria file and this script must agree ───────────────────────────
unchecked=()
for id in "${!SEVERITY[@]}"; do
    [[ -n "${RECORDED[$id]:-}" ]] || unchecked+=("$id")
done
if [[ ${#unchecked[@]} -gt 0 ]]; then
    echo -e "${RED}drift: $CRITERIA_YAML defines these '$STAGE_ID' criteria, which this script never checked:${NC}" >&2
    printf '  %s\n' "${unchecked[@]}" >&2
    echo "  implement them here, or remove them from $CRITERIA_YAML" >&2
    exit 2
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
