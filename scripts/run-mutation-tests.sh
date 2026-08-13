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
    [spans]="tests/redaction/test_apply_redactions.py tests/redaction/test_span_merge_boundaries.py tests/redaction/test_word_offset_alignment.py tests/redaction/test_non_ascii_masking.py"
    [password_policy]="tests/unit/test_auth_config_behaviour.py tests/test_fedramp_compliance.py tests/unit/test_account_lifecycle.py tests/unit/test_auth_policy_source_of_truth.py tests/unit/test_password_policy_controls.py"
    [security]="tests/api/endpoints/test_auth_comprehensive.py tests/unit/test_token_type_binding.py tests/test_fips_140_3.py tests/unit/test_bcrypt_test_rounds.py tests/unit/test_local_auth_policy.py tests/unit/test_jwt_algorithm_downgrade.py"
    # ⚠️ THIS LIST IS THE MEASUREMENT. An omitted file is not a smaller run — it is a
    # batch of FALSE survivors that look exactly like real findings. The first run of
    # this target reported 41 survivors in `_enforce_proxy_identity_consistency` and
    # was read as "proxy header spoofing has no coverage"; in fact
    # tests/api/test_proxy_auth_endpoint.py had covered both the untrusted-peer and
    # identity-mismatch cases all along, and simply was not selected. Same failure
    # mode as the RUN_* gate trap in backend/tests/CLAUDE.md.
    # A static reference-based guard for this list was tried and REJECTED: the two files
    # it needed to find (test_proxy_auth_endpoint.py, test_cloud_seams.py) name neither
    # the module nor its helpers -- they drive it over HTTP and through the provider
    # registry -- so nothing static could derive them. The coverage pre-flight below
    # measures the property directly instead.
    [dependencies]="tests/unit/test_route_privilege_tiers.py tests/unit/test_account_lifecycle.py tests/unit/test_account_approval.py tests/unit/test_mfa_enforcement.py tests/unit/test_flower_access.py tests/unit/test_banner_acknowledgment.py tests/unit/test_token_type_binding.py tests/unit/test_access_token_revocation_epoch.py tests/unit/test_credential_gate_fail_closed.py tests/api/test_proxy_auth_endpoint.py tests/test_cloud_seams.py tests/unit/test_proxy_identity_consistency.py tests/unit/test_lifecycle_denial_audit_records.py tests/unit/test_optional_current_user.py tests/unit/test_external_token_auth.py"
    # Coverage pre-flight caught this list at 56% of the module on its first real run:
    # it omitted test_lockout_atomicity.py (named for the module it tests) and
    # test_auth_config_behaviour.py, both of which import app.auth.lockout directly, plus
    # the login path that drives it end to end. Now 80.4%.
    [lockout]="tests/unit/test_lockout_identifier_canonical.py tests/unit/test_auth_state_degradation.py tests/test_fedramp_controls.py tests/unit/test_lockout_cleanup_sweep.py tests/unit/test_lockout_atomicity.py tests/unit/test_auth_config_behaviour.py tests/api/test_auth_endpoints.py"
    # HISTORY, kept because the reasoning still applies to the next target like it:
    # this entry used to warn that ~every OIDCStateStore mutant would survive, because
    # `store_state`/`get_state`/`delete_state` had no test at all and the target
    # therefore measured ABSENCE, not weakness. #33 landed those tests and the
    # 2026-08-12 run kills the store_state mutants, so the warning is retired. The
    # distinction it drew -- a target can report survivors because nothing tests the
    # code, not because the tests are weak -- is now checked mechanically by the
    # coverage pre-flight rather than remembered in a comment.
    [session]="tests/unit/test_session_lifetime.py tests/unit/test_auth_state_degradation.py tests/unit/test_oidc_state_single_use.py"
)


# Warn when a target's cached mutmut verdicts were produced by a DIFFERENT test list.
#
# mutmut keeps results in backend/mutants/ and reuses them across runs, so the report can
# mix records from a previous run with this one. That matters the moment MODULE_TESTS
# changes: after widening lockout's list from 56% to 80% coverage, the report still listed
# survivors recorded under the old list -- including one ("a locked account reports NOT
# locked") that the widened suite demonstrably kills. A cached survivor from a weaker test
# set reads exactly like a real finding: the same trap as an incomplete list, one level up.
#
# A function rather than inline code so it is testable without a 30-90 minute run.
# Args: $1 = module key, $2 = the test list, $3 = cache dir, $4 = mutants dir.
# Echoes "stale" when the results must not be trusted, "" otherwise. Always rewrites the
# fingerprint, so the warning fires once per change and not forever after.
warn_if_test_list_changed() {
    local key=$1 tests=$2 out_dir=$3 mutants_dir=$4
    local tests_hash prev_hash hash_file
    tests_hash=$(printf '%s' "$tests" | sha256sum | cut -c1-16)
    hash_file="$out_dir/$key.testhash"
    prev_hash=$(cat "$hash_file" 2>/dev/null || true)
    printf '%s' "$tests_hash" > "$hash_file"
    if [[ -n "$prev_hash" && "$prev_hash" != "$tests_hash" && -d "$mutants_dir" ]]; then
        echo stale
    fi
}


# Verify ONE claimed survivor by applying its mutation to the real source and running the
# module's tests. This exists because mutmut's own verdict has been wrong here.
#
# `app.auth.lockout.x__check_and_record_attempt_memory__mutmut_13` turns
# `return True, locked_until_dt` into `return False, ...` -- a locked account reporting NOT
# locked -- and mutmut recorded it as SURVIVED. Applied directly to app/auth/lockout.py it
# fails `test_lockout_atomicity.py::test_locked_account_short_circuits_without_writing`
# immediately. So the survivor list has false positives from a THIRD mechanism, after the
# incomplete test list and the stale cache: mutmut's own per-mutant environment and test
# selection. The coverage pre-flight cannot see this one, because it measures coverage
# OUTSIDE mutmut.
#
# A survivor is a claim about the test suite. This turns it into a checked claim. Only
# single-line mutations are handled, and a non-unique or multi-line diff is reported as
# UNVERIFIABLE rather than guessed at.
verify_survivor() {
    # Serialised per module. TWO verifies running against the same file is not a slow
    # verify, it is a corrupt one: each applies a mutation and restores from its own
    # backup, so one process's restore can reinstate the other's mutation -- or leave one
    # behind entirely. Observed here: two mutations applied to lockout.py simultaneously,
    # because a second verify was started while a batch was already looping.
    #
    # The lock is taken on fd 9 for the whole body, so `--verify` in a shell loop is safe
    # and two operators are safe. flock is util-linux, already required elsewhere in this
    # repo's tooling.
    local _lock_file="$OUT_DIR/.verify-$2.lock"
    exec 9>"$_lock_file"
    flock 9
    _verify_survivor_locked "$@"
    local _rc=$?
    exec 9>&-
    return $_rc
}

_verify_survivor_locked() {
    local mutant=$1 module_key=$2
    # A dirty target file is the signature of a verify that DIED between applying its
    # mutation and restoring it. The trap covers INT/TERM/ERR/EXIT, but nothing survives a
    # SIGKILL of the process group -- which is exactly what happened when a batch loop was
    # stopped mid-cycle and left `now < locked_until_dt` mutated to `now <= locked_until_dt`
    # in app/auth/lockout.py. Verifying against an already-mutated file silently reports the
    # wrong verdict, so say so before doing any work. A warning, not a refusal: legitimate
    # uncommitted edits to the module are normal during development.
    if git -C "$REPO_ROOT" diff --quiet -- "backend/${MODULE_PATH[$module_key]}" 2>/dev/null; then
        :
    else
        echo -e "${YELLOW}  ⚠ backend/${MODULE_PATH[$module_key]} has uncommitted changes.${NC}" >&2
        echo -e "${YELLOW}    If a previous --verify was killed, this is its leftover mutation and${NC}" >&2
        echo -e "${YELLOW}    every verdict below is measured against the wrong source. Check with:${NC}" >&2
        echo -e "${YELLOW}      git diff -- backend/${MODULE_PATH[$module_key]}${NC}" >&2
    fi
    local path tests backup rc out
    path="${MODULE_PATH[$module_key]:-}"
    tests="${MODULE_TESTS[$module_key]:-}"
    if [[ -z "$path" ]]; then
        echo "UNVERIFIABLE  unknown module key '$module_key'"; return 2
    fi

    # SAFETY: this transiently mutates the LIVE source file. Two consequences, both real:
    #
    #  * A Ctrl-C, timeout or crash between the write and the restore leaves a mutated
    #    production file behind. Hence the trap: it restores on INT/TERM/ERR/EXIT, not just
    #    on the happy path.
    #  * Do not commit while a verify is running. pre-commit stashes the whole tree, so it
    #    can capture the mutated file mid-cycle -- which has already happened on this branch,
    #    to another agent's in-flight mutation. The batch case is worse than the single case,
    #    so `--verify` says so out loud.
    backup=$(mktemp)
    cp "$BACKEND/$path" "$backup"
    # shellcheck disable=SC2064  # expand $backup and $path NOW, not when the trap fires
    trap "cp '$backup' '$BACKEND/$path' 2>/dev/null; rm -f '$backup'" INT TERM ERR EXIT

    # The hunk is applied via its CONTEXT lines, not by line number: mutmut prints the
    # diff relative to the mutated FUNCTION, so `@@ -24,7 +24,7 @@` is not a file offset.
    # Context also disambiguates a changed line that occurs more than once -- e.g.
    # `return True, locked_until_dt` appears twice in lockout.py, and a bare
    # search-and-replace either picks the wrong one or gives up.
    # The mutant id carries its function (`...x__check_and_record_attempt_memory__mutmut_13`),
    # which is what disambiguates a context block that appears in more than one place --
    # and it does here: the Redis and in-memory lockout paths are near-duplicates, so 7
    # mutants came back UNVERIFIABLE until the search was scoped to the right function.
    mut_func="${mutant##*.}"; mut_func="${mut_func%%__mutmut_*}"; mut_func="${mut_func#x}"
    out=$("$VENV_BIN/mutmut" show "$mutant" 2>/dev/null | MUT_TARGET="$BACKEND/$path" \
        MUT_FUNC="$mut_func" \
        "$VENV_BIN/python" -c '
import os, pathlib, sys

show = sys.stdin.read().splitlines()
old, new = [], []
for line in show:
    if line.startswith(("---", "+++", "@@", "#")):
        continue
    if line.startswith("-"):
        old.append(line[1:])
    elif line.startswith("+"):
        new.append(line[1:])
    else:
        old.append(line[1:] if line.startswith(" ") else line)
        new.append(line[1:] if line.startswith(" ") else line)

if not old or old == new:
    print("NODIFF"); raise SystemExit(0)

old_block, new_block = "\n".join(old), "\n".join(new)
target = pathlib.Path(os.environ["MUT_TARGET"])
source = target.read_text()

# Narrow to the mutated function when its name is known, so a context block shared by two
# near-duplicate functions is no longer ambiguous.
lo, hi = 0, len(source)
want = os.environ.get("MUT_FUNC", "")
if want:
    import ast
    lines = source.splitlines(keepends=True)
    offsets, run = [], 0
    for line in lines:
        offsets.append(run); run += len(line)
    offsets.append(run)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == want:
            lo = offsets[node.lineno - 1]
            hi = offsets[min(node.end_lineno, len(lines))]
            break

region = source[lo:hi]
count = region.count(old_block)
if count != 1:
    # Fall back to the whole file: mutmut names nested/decorated helpers in ways ast may
    # not match, and a whole-file unique match is still unambiguous.
    count = source.count(old_block)
    if count != 1:
        print(f"AMBIGUOUS {count}"); raise SystemExit(0)
    target.write_text(source.replace(old_block, new_block))
    print("APPLIED"); raise SystemExit(0)

target.write_text(source[:lo] + region.replace(old_block, new_block) + source[hi:])
print("APPLIED")
')

    case "$out" in
        APPLIED) : ;;
        NODIFF)
            trap - INT TERM ERR EXIT
            cp "$backup" "$BACKEND/$path"; rm -f "$backup"
            echo "UNVERIFIABLE  $mutant (mutmut show produced no usable diff)"; return 2 ;;
        *)
            trap - INT TERM ERR EXIT
            cp "$backup" "$BACKEND/$path"; rm -f "$backup"
            echo "UNVERIFIABLE  $mutant (context block matched ${out#AMBIGUOUS } times)"; return 2 ;;
    esac

    # STAGED, and this is a correctness-preserving optimisation rather than a shortcut: a
    # mutant is KILLED iff ANY selected test fails, so the fast files run first with `-x`
    # and a failure short-circuits the rest. Only a SURVIVED verdict has to pay for the
    # whole list.
    #
    # It matters because `--verify` re-runs the module's ENTIRE list, while mutmut itself
    # runs only the tests covering each mutant. That asymmetry made verification 28-48 s per
    # mutant (lockout's list includes tests/api/test_auth_endpoints.py, a DB + HTTP suite),
    # i.e. hours for one module -- so the tool that exists to make survivors trustworthy was
    # too slow to use on them. KILLED is the common case once tests exist, and it is now the
    # cheap one.
    local fast="" slow=""
    for f in $tests; do
        case "$f" in tests/unit/*) fast="$fast $f" ;; *) slow="$slow $f" ;; esac
    done

    rc=0
    for stage in "$fast" "$slow"; do
        [[ -n "${stage// /}" ]] || continue
        ( cd "$BACKEND" && env "${GATES[@]}" "$VENV_BIN/python" -m pytest $stage \
            -o addopts= -p no:cacheprovider -q --no-header -x >/dev/null 2>&1 )
        rc=$?
        [[ $rc -ne 0 ]] && break
    done
    trap - INT TERM ERR EXIT
    cp "$backup" "$BACKEND/$path"
    rm -f "$backup"

    if [[ $rc -eq 0 ]]; then
        echo "SURVIVED      $mutant  (confirmed: the suite does not notice)"
        return 1
    fi
    # Two ways to get here, and the message must not pick one: mutmut's verdict can be
    # wrong, or the suite can have gained a test since the run. --verify always uses the
    # CURRENT suite, which is the question worth answering either way.
    echo "KILLED        $mutant  (the CURRENT suite catches it -- the recorded survivor verdict is stale or wrong)"
    return 0
}

# Same set scripts/run-backend-tests.sh --gated enables. Mandatory here: three of the
# test files above are behind a module-level skipif, and a skipped test cannot kill a
# mutant — an ungated run would report their mutants as survivors.
GATES=(RUN_PKI_TESTS=true RUN_MFA_TESTS=true RUN_LLM_TESTS=true
       RUN_FEDRAMP_TESTS=true RUN_FIPS_TESTS=true
       RUN_AUTH_CONFIG_TESTS=true RUN_ADVANCED_ADMIN_TESTS=true)

# Service credentials must be EXPORTED, not left to conftest's .env read.
#
# mutmut runs pytest from inside backend/mutants/, so conftest's
# `_project_root = _backend_dir.parent` resolves to backend/ and looks for
# backend/.env — which does not exist (the real one is at the repo root). The DB
# credentials then fall back to defaults and every DB-backed test ERRORS at setup,
# which aborts mutmut's baseline before a single mutant runs. `spans` never hit this
# because its tests are pure functions.
#
# conftest uses `os.environ.setdefault`, and documents that explicitly exported
# values win over .env precisely so CI and throwaway-DB runs can override — this is
# that escape hatch. Read straight into the environment; never echoed.
if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1090  # a filtered subset of .env, by design
    source <(grep -E '^(POSTGRES_(USER|PASSWORD|DB)|MINIO_ROOT_(USER|PASSWORD)|MEDIA_BUCKET_NAME)=' \
             "$REPO_ROOT/.env" || true)
    set +a
fi

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
        --verify)  MODE=verify; SHOW_ID="${2:-}"; shift ;;
        # Refuse a second --module rather than silently keeping the last one. Passing
        # `--module lockout --module session` looked like it ran both and ran only session,
        # so the missing module read as "no findings there" — the same silently-dropped-work
        # shape this script's own MODULE_TESTS bug had. One module at a time is deliberate
        # (see the header); make the misuse loud instead of plausible.
        --module)
            if [[ -n "$MODULE" ]]; then
                echo -e "${RED}--module given twice ('$MODULE' then '${2:-}').${NC}" >&2
                echo -e "${RED}Run one module at a time, or use --all.${NC}" >&2
                exit 2
            fi
            MODE=run; MODULE="${2:-}"; shift ;;
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
# NOT cfg['runner'] -- that key does not exist and must not come back (see the
# comment on it in pyproject.toml). Reading it made --list crash with KeyError.
print(f"\n  source_paths: {cfg['source_paths']}")
print(f"  timeout_constant: {cfg.get('timeout_constant', 'unset')}s")
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

if [[ "$MODE" == verify ]]; then
    # Turn a claimed survivor into a CHECKED claim. See verify_survivor's comment for the
    # mutant whose verdict was wrong and prompted this.
    [[ -n "$SHOW_ID" ]] || {
        echo -e "${RED}--verify needs a mutant id, e.g.${NC}" >&2
        echo -e "${RED}  --verify app.auth.lockout.x__check_and_record_attempt_memory__mutmut_13${NC}" >&2
        exit 2
    }
    # Derive the module key from the mutant id so the caller cannot pair a mutant with the
    # wrong module's test list — which would silently verify against the wrong suite.
    verify_key=""
    for k in "${!MODULE_PATH[@]}"; do
        dotted="${MODULE_PATH[$k]%.py}"; dotted="${dotted//\//.}"
        case "$SHOW_ID" in "$dotted".*) verify_key="$k" ;; esac
    done
    [[ -n "$verify_key" ]] || {
        echo -e "${RED}Cannot tell which configured module '$SHOW_ID' belongs to.${NC}" >&2
        exit 2
    }
    echo -e "${BLUE}Verifying against MODULE_TESTS[$verify_key]${NC}"
    echo -e "${YELLOW}  NOTE: this transiently edits ${MODULE_PATH[$verify_key]}.${NC}" >&2
    echo -e "${YELLOW}  Do not commit while it runs: pre-commit stashes the whole tree and${NC}" >&2
    echo -e "${YELLOW}  can capture the mutation mid-cycle.${NC}" >&2
    verify_survivor "$SHOW_ID" "$verify_key"
    exit $?
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
#: Percent of the target module the SELECTED tests must execute before survivors mean
#: anything. Not a coverage target for the codebase — a sanity check on MODULE_TESTS.
MIN_TARGET_COVERAGE=${MIN_TARGET_COVERAGE:-60}
LOW_COVERAGE=()
STALE_CACHE=()
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

    if ! $DRY_RUN && [[ -n $(warn_if_test_list_changed "$key" "$tests" "$OUT_DIR" "$BACKEND/mutants") ]]; then
        echo -e "${RED}    ⚠ MODULE_TESTS[$key] changed since the cached results were written.${NC}" >&2
        echo -e "${RED}      backend/mutants/ still holds verdicts from the OLD list, and the${NC}" >&2
        echo -e "${RED}      report cannot tell them apart from this run's. Re-run as:${NC}" >&2
        echo -e "${RED}        ./scripts/run-mutation-tests.sh --clean${NC}" >&2
        echo -e "${RED}        ./scripts/run-mutation-tests.sh --module $key${NC}" >&2
        STALE_CACHE+=("$key")
    fi

    # PRE-FLIGHT: do the selected tests actually EXECUTE this module?
    #
    # This exists because a run of `dependencies` reported 41 survivors in
    # `_enforce_proxy_identity_consistency` and they were read as "proxy header spoofing
    # has no coverage". It had coverage — in tests/api/test_proxy_auth_endpoint.py, which
    # simply was not in MODULE_TESTS. A test that is never selected kills no mutant, so an
    # incomplete list does not produce a smaller run; it produces FALSE survivors that are
    # indistinguishable from real findings, and the natural reading of them is a
    # vulnerability report about code that is in fact tested.
    #
    # Coverage of the target module is the cheap, direct check: below the floor, the
    # survivors measure test SELECTION, not test strength, and the report says so instead
    # of leaving the reader to infer it.
    if ! $DRY_RUN; then
        # Dotted form, because --cov takes an importable name; and --cov-report=term is
        # required -- an empty --cov-report prints no total to parse, which silently made
        # this check a no-op the first time it was written.
        cov_mod="${path%.py}"
        cov_mod="${cov_mod//\//.}"
        cov_pct=$(cd "$BACKEND" && env "${GATES[@]}" "$VENV_BIN/python" -m pytest $tests \
            -o addopts= -p no:cacheprovider -q --no-header \
            --cov="$cov_mod" --cov-report=term 2>/dev/null \
            | awk '/^TOTAL/ {gsub(/%/, "", $NF); print $NF}' | tail -1 | cut -d. -f1)
        if [[ -n "${cov_pct:-}" ]]; then
            if (( cov_pct < MIN_TARGET_COVERAGE )); then
                echo -e "${RED}    ⚠ selected tests cover only ${cov_pct}% of $path${NC}"
                echo -e "${RED}      Survivors below ${MIN_TARGET_COVERAGE}% measure SELECTION, not weakness —${NC}"
                echo -e "${RED}      add the missing test files to MODULE_TESTS[$key] before believing them.${NC}"
                LOW_COVERAGE+=("$key:${cov_pct}%")
            else
                echo -e "    coverage of target by selected tests: ${GREEN}${cov_pct}%${NC}"
            fi
        else
            echo -e "${YELLOW}    could not measure target coverage — treat survivors with suspicion${NC}"
        fi
    fi

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
if [[ ${#STALE_CACHE[@]} -gt 0 ]]; then
    echo -e "${RED}⚠ RESULTS FOR THESE TARGETS MIX TWO TEST SETS: ${STALE_CACHE[*]}${NC}"
    echo -e "${RED}  Their MODULE_TESTS changed since the cache was written. Run --clean, then${NC}"
    echo -e "${RED}  re-run the module, before triaging a single survivor.${NC}"
fi
if [[ ${#LOW_COVERAGE[@]} -gt 0 ]]; then
    # Printed AFTER the survivor list on purpose: it is the caveat that decides how to read
    # everything above it. Without it, a reader takes a survivor count at face value.
    echo -e "${RED}⚠ SURVIVORS FROM THESE TARGETS ARE NOT TRUSTWORTHY: ${LOW_COVERAGE[*]}${NC}"
    echo -e "${RED}  The selected tests barely execute the module, so a survivor mostly means${NC}"
    echo -e "${RED}  'no selected test runs this line' — fix MODULE_TESTS before triaging.${NC}"
fi
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo -e "${YELLOW}Modules with survivors or errors: ${FAILED[*]}${NC}"
    exit 1
fi
exit 0
