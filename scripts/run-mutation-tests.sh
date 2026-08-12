#!/bin/bash
# Mutation-test the security-critical modules — OPT-IN, never part of any gate.
#
# WHY
#
# Coverage tells you a line RAN. It cannot tell you the suite would NOTICE if the line
# were wrong. Mutation testing answers that directly: it edits the source (flips `<` to
# `<=`, drops an `and` clause, changes a constant, returns None) and re-runs the tests.
# A mutant that dies proves a test was really checking that behaviour. A mutant that
# SURVIVES is a line the suite executes without asserting anything about — and for an
# auth predicate that means the control could be deleted with the suite still green.
#
# Scoped to a small, high-value set (`[tool.mutmut] only_mutate` in
# backend/pyproject.toml — this script reads it, so there is one source of truth). A
# whole-codebase run is hours and is not the point.
#
# EXPECTED RUNTIME — read this before starting one
#
#   redaction/spans.py      ~1-3 min    pure functions, no DB, ~200 lines
#   auth/password_policy.py ~5-15 min   pure predicates over DB-backed settings
#   core/security.py        ~10-30 min  bcrypt/JWT — each mutant pays a KDF round
#   auth/dependencies.py    ~20-60 min  every mutant boots the FastAPI test client
#   auth/lockout.py         ~30-90 min  1,068 lines, Redis-backed
#   auth/session.py         ~15-45 min
#   ALL of them             hours       do not do this casually
#
# Always start with ONE module (`--module spans`), and start with `spans` — it is the
# fastest and its tests are the only fully hermetic ones in the set.
#
# SAFETY
#
# - Requires the dev stack ONLY for Postgres (5176) and Redis (5177); it never uploads,
#   never touches MinIO or OpenSearch, and every DB write goes through the suite's
#   savepoint rollback.
# - It does NOT touch the GPU. It is CPU-only and will happily saturate every core, so
#   do not start one while a transcription benchmark is running.
# - It writes only to backend/mutants/ (mutmut's working copy, gitignored) and to
#   $OT_MUTATION_OUT_DIR (default /tmp/ot-mutation).
# - backend/mutants/ is left in place on purpose: --results and --show read it, and
#   mutmut reuses it as a cache across runs. It is ~330k lines of DELIBERATELY
#   CORRUPTED source, so nothing may scan it. bandit is the one that bit us — it walks
#   the filesystem, not the git index, so gitignoring was not enough and pre-commit
#   failed on a finding inside a mutant. It is excluded in backend/pyproject.toml
#   ([tool.bandit] exclude_dirs). Add any new repo-wide scanner to that list too, and
#   use --clean when you are done with a run.
#
# Usage:
#   ./scripts/run-mutation-tests.sh --check              # preconditions only, mutate nothing
#   ./scripts/run-mutation-tests.sh --list               # the configured targets + their tests
#   ./scripts/run-mutation-tests.sh --module spans       # ONE module (start here)
#   ./scripts/run-mutation-tests.sh --module spans --dry-run   # print the commands, run nothing
#   ./scripts/run-mutation-tests.sh --all                # every configured module (hours)
#   ./scripts/run-mutation-tests.sh --results            # re-report the last run, no re-run
#   ./scripts/run-mutation-tests.sh --show <mutant-id>   # the diff for one surviving mutant

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND="$REPO_ROOT/backend"
VENV_BIN="$BACKEND/venv/bin"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

OUT_DIR="${OT_MUTATION_OUT_DIR:-/tmp/ot-mutation}"
mkdir -p "$OUT_DIR"

# ---------------------------------------------------------------------------
# Module -> the tests that actually exercise it.
#
# Narrowing is not just an optimisation. mutmut re-runs the selected tests once per
# mutant, so pointing it at all ~5,300 tests would multiply a 4-minute suite by the
# mutant count. The selection has to be honest in the other direction too: a test that
# SKIPS kills no mutant, so an under-selected (or gate-disabled) set reports false
# survivors that look exactly like real findings. That is why the RUN_* gates are
# exported below.
# ---------------------------------------------------------------------------
declare -A MODULE_PATH=(
    [spans]="app/services/redaction/spans.py"
    [password_policy]="app/auth/password_policy.py"
    [security]="app/core/security.py"
    [dependencies]="app/api/endpoints/auth/dependencies.py"
    [lockout]="app/auth/lockout.py"
    [session]="app/auth/session.py"
)

declare -A MODULE_TESTS=(
    [spans]="tests/redaction/test_apply_redactions.py tests/redaction/test_span_merge_boundaries.py tests/redaction/test_word_offset_alignment.py"
    [password_policy]="tests/unit/test_auth_config_behaviour.py tests/test_fedramp_compliance.py"
    [security]="tests/api/endpoints/test_auth_comprehensive.py tests/unit/test_token_type_binding.py tests/test_fips_140_3.py"
    [dependencies]="tests/unit/test_route_privilege_tiers.py tests/unit/test_account_lifecycle.py tests/unit/test_account_approval.py tests/unit/test_mfa_enforcement.py tests/unit/test_flower_access.py tests/unit/test_banner_acknowledgment.py tests/unit/test_token_type_binding.py"
    [lockout]="tests/unit/test_lockout_identifier_canonical.py tests/unit/test_auth_state_degradation.py tests/test_fedramp_controls.py"
    [session]="tests/unit/test_session_lifetime.py tests/unit/test_auth_state_degradation.py"
)

# Same set scripts/run-backend-tests.sh --gated enables. Mandatory here: three of the
# test files above are behind a module-level skipif, and a skipped test cannot kill a
# mutant — an ungated run would report their mutants as survivors.
GATES=(RUN_PKI_TESTS=true RUN_MFA_TESTS=true RUN_LLM_TESTS=true
       RUN_FEDRAMP_TESTS=true RUN_FIPS_TESTS=true
       RUN_AUTH_CONFIG_TESTS=true RUN_ADVANCED_ADMIN_TESTS=true)

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
MODE=""
MODULE=""
DRY_RUN=false
SHOW_ID=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)   MODE=check ;;
        --list)    MODE=list ;;
        --all)     MODE=run; MODULE=ALL ;;
        --results) MODE=results ;;
        --clean)   MODE=clean ;;
        --show)    MODE=show; SHOW_ID="${2:-}"; shift ;;
        --module)  MODE=run; MODULE="${2:-}"; shift ;;
        --dry-run) DRY_RUN=true ;;
        -h|--help) sed -n '2,50p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo -e "${RED}Unknown option: $1${NC}" >&2; exit 2 ;;
    esac
    shift
done

if [[ -z "$MODE" ]]; then
    echo -e "${YELLOW}Mutation testing is opt-in and slow. Pick a mode:${NC}" >&2
    sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \?//' >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
port_open() { (exec 3<>"/dev/tcp/localhost/$1") 2>/dev/null && exec 3>&- && return 0 || return 1; }

check_preconditions() {
    local ok=true

    if [[ ! -x "$VENV_BIN/python" ]]; then
        echo -e "${RED}✗ backend/venv missing — create it per CLAUDE.md${NC}"; ok=false
    else
        echo -e "${GREEN}✓ backend/venv${NC}"
    fi

    if [[ ! -x "$VENV_BIN/mutmut" ]]; then
        echo -e "${RED}✗ mutmut not installed${NC}"
        echo -e "  ${YELLOW}$VENV_BIN/pip install 'mutmut>=3.2.0'${NC}  (it is in backend/requirements-dev.txt)"
        ok=false
    else
        echo -e "${GREEN}✓ mutmut $("$VENV_BIN/mutmut" version 2>/dev/null || echo '(version unknown)')${NC}"
    fi

    # Postgres is the only hard service dependency: the root conftest points the suite at
    # localhost:5176 and the auth modules' tests need real rows. MinIO/OpenSearch tests
    # auto-skip, which is fine — none of the target modules touch them.
    if port_open 5176; then
        echo -e "${GREEN}✓ Postgres up (5176)${NC}"
    else
        echo -e "${RED}✗ Postgres not reachable on 5176 — ./opentr.sh start dev${NC}"; ok=false
    fi
    if port_open 5177; then
        echo -e "${GREEN}✓ Redis up (5177) — lockout/session mutants need it${NC}"
    else
        echo -e "${YELLOW}! Redis not reachable on 5177 — the lockout/session modules will${NC}"
        echo -e "${YELLOW}  report false survivors. Fine for --module spans.${NC}"
    fi

    # A mutation run must be SERIAL. pyproject's addopts carry `-n auto`; every mutant
    # would otherwise fork one xdist worker per core against a single shared Postgres,
    # which is both slower and the known deadlock shape (issues #389, #431).
    echo -e "${BLUE}  serialisation: PYTEST_ADDOPTS clears addopts entirely ('-o addopts=') -- mutmut runs pytest in-process${NC}"

    $ok
}

# ---------------------------------------------------------------------------
# Modes that do not mutate anything
# ---------------------------------------------------------------------------
if [[ "$MODE" == check ]]; then
    echo -e "${BLUE}Mutation-test preconditions${NC}"
    check_preconditions || exit 1
    echo
    echo -e "${BLUE}Verify the pytest selection resolves (collection only — runs no test):${NC}"
    echo "  cd $BACKEND && PYTEST_ADDOPTS=\"-n0 ${MODULE_TESTS[spans]}\" venv/bin/pytest --collect-only -q | tail -3"
    exit 0
fi

if [[ "$MODE" == list ]]; then
    echo -e "${BLUE}Configured mutation targets ([tool.mutmut] in backend/pyproject.toml)${NC}"
    "$VENV_BIN/python" - "$BACKEND/pyproject.toml" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    cfg = tomllib.load(fh)["tool"]["mutmut"]
for path in cfg["only_mutate"]:
    print(f"  {path}")
print(f"\n  runner: {cfg['runner']}")
PY
    echo
    echo -e "${BLUE}Module aliases and the tests each one runs${NC}"
    for key in spans password_policy security dependencies lockout session; do
        printf "  %-16s %s\n" "$key" "${MODULE_PATH[$key]}"
        printf "  %-16s   tests: %s\n" "" "${MODULE_TESTS[$key]}"
    done
    exit 0
fi

cd "$BACKEND" || exit 2

if [[ "$MODE" == results ]]; then
    [[ -x "$VENV_BIN/mutmut" ]] || { echo -e "${RED}mutmut not installed${NC}" >&2; exit 1; }
    "$VENV_BIN/mutmut" results
    rc=$?
    echo
    echo -e "${YELLOW}A SURVIVED mutant is a finding. See its diff with:${NC}"
    echo -e "  $0 --show <mutant-id>"
    exit $rc
fi

if [[ "$MODE" == show ]]; then
    [[ -n "$SHOW_ID" ]] || { echo -e "${RED}--show needs a mutant id (see --results)${NC}" >&2; exit 2; }
    exec "$VENV_BIN/mutmut" show "$SHOW_ID"
fi

if [[ "$MODE" == clean ]]; then
    # Deletes ONLY backend/mutants/ and the mutmut cache, both generated and
    # gitignored. Deliberately not automatic after a run: --results and --show read
    # that tree, and mutmut reuses it as a cache. Run this when you are done, so
    # ~330k lines of deliberately corrupted source are not left for the next
    # filesystem-walking tool to trip over.
    for target in "$BACKEND/mutants" "$BACKEND/.mutmut-cache"; do
        if [[ -e "$target" ]]; then
            rm -rf "$target"
            echo -e "${GREEN}✓ removed $target${NC}"
        else
            echo -e "${BLUE}  already absent: $target${NC}"
        fi
    done
    exit 0
fi

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if [[ "$MODULE" == ALL ]]; then
    MODULES=(spans password_policy security dependencies lockout session)
    echo -e "${YELLOW}--all mutates every configured module. Expect HOURS.${NC}"
    echo -e "${YELLOW}Prefer one module at a time (--module spans).${NC}"
    echo
else
    [[ -n "${MODULE_PATH[$MODULE]:-}" ]] || {
        echo -e "${RED}Unknown module '$MODULE'. Known: ${!MODULE_PATH[*]}${NC}" >&2
        exit 2
    }
    MODULES=("$MODULE")
fi

$DRY_RUN || check_preconditions || exit 1
echo

FAILED=()
for key in "${MODULES[@]}"; do
    path="${MODULE_PATH[$key]}"
    tests="${MODULE_TESTS[$key]}"
    log="$OUT_DIR/$key.log"

    echo -e "${BLUE}--- mutating $path ---${NC}"
    echo -e "    tests: $tests"
    echo -e "    log:   $log"

    # `-n0` overrides addopts' `-n auto` (last -n wins) so each mutant runs SERIALLY.
    # The test paths ride in PYTEST_ADDOPTS because pytest prepends its contents to the
    # command line, and [tool.mutmut] runner deliberately names no paths — that is what
    # makes per-module narrowing possible without a mutmut-version-specific flag.
    # `-o addopts=` CLEARS pyproject's addopts rather than fighting them. mutmut runs
    # pytest in-process, so the inherited `-n auto --dist loadgroup` cannot be satisfied
    # (appending `-n0` was not enough -- xdist still initialises) and the inherited
    # `-m 'not integration and not gpu'` would deselect by marker inside every mutant.
    # This is the same override scripts/run-integration-tests.sh uses for its phases.
    addopts="-o addopts= -p no:cacheprovider $tests"

    # mutmut 3.x has NO --paths-to-mutate flag (it exits 2 on the unknown option) and
    # takes MUTANT_NAMES positionally instead, so per-module scoping is a dotted-path
    # glob over the mutant ids: app/services/redaction/spans.py -> app.services.redaction.spans*
    mutant_glob="${path%.py}"
    mutant_glob="${mutant_glob//\//.}*"

    if $DRY_RUN; then
        echo -e "${YELLOW}    would run:${NC}"
        echo "      cd $BACKEND"
        echo "      env ${GATES[*]} PYTEST_ADDOPTS=\"$addopts\" \\"
        echo "        venv/bin/mutmut run '$mutant_glob'"
        echo
        continue
    fi

    if env "${GATES[@]}" PYTEST_ADDOPTS="$addopts" \
        "$VENV_BIN/mutmut" run "$mutant_glob" 2>&1 | tee "$log"; then
        echo -e "${GREEN}✓ $key complete${NC}"
    else
        # A non-zero exit from mutmut means "mutants survived", which is a FINDING, not a
        # broken run. Record it and keep going; the report at the end names them.
        echo -e "${YELLOW}! $key: mutmut exited non-zero (survivors, or a run error)${NC}"
        FAILED+=("$key")
    fi
    echo
done

$DRY_RUN && exit 0

echo -e "${BLUE}========================================${NC}"
"$VENV_BIN/mutmut" results || true
echo
echo -e "${BLUE}How to read this${NC}"
echo -e "  ${GREEN}killed${NC}    a test noticed the edit — the line is genuinely checked"
echo -e "  ${RED}survived${NC}  THE FINDING: the suite runs that line but asserts nothing about it."
echo -e "            Inspect the diff ($0 --show <id>), then either add the missing"
echo -e "            assertion or conclude the line is dead and delete it."
echo -e "  ${YELLOW}timeout${NC}   the edit caused a hang; counts as killed, but check it is not a"
echo -e "            real infinite loop the tests were papering over"
echo -e "  ${YELLOW}suspicious${NC} much slower than baseline — usually a mutated retry/sleep bound"
echo
echo -e "Logs: $OUT_DIR/"
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo -e "${YELLOW}Modules with survivors or errors: ${FAILED[*]}${NC}"
    exit 1
fi
exit 0
