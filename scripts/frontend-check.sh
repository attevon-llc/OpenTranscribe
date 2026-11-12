#!/bin/bash
# Frontend Build Verification Script
# Runs eslint, i18n parity, svelte-check, vitest, the test-quality auditors, and vite build,
# with optional Claude Code auto-fix
#
# Usage:
#   ./scripts/frontend-check.sh [OPTIONS]
#
# Options:
#   --no-claude      Skip Claude auto-fix on failure
#   --check-only     Skip the vite build. Everything else still runs — see the note below.
#   --related-only   Commit-stage hook only: vitest runs just the tests related to the STAGED
#                    frontend files (`vitest related`), without the coverage floors. The full
#                    suite with floors runs at push stage (frontend-build), in CI, and in
#                    every caller that does not pass this flag.
#   --verbose        Show full output from checks
#   -h, --help       Show this help message
#
# Environment:
#   OT_HOOK_VITEST_WORKERS  vitest --maxWorkers for the coverage run (default 8; number or N%).
#                           Caps per-commit CPU when many agents commit concurrently (issue #77).
#
# ⚠️ vitest is INSIDE --check-only, deliberately.
#
# 189 test files and ~1,750 tests ran in NO local gate at all: `npm run test`,
# `test:audit` and `test:audit:selftest` appeared only in .github/workflows/pre-commit.yml.
# `run-dev-tests.sh --full` and `test-matrix.sh` leg 1.4 both call this script with
# --check-only, so putting them outside that flag would have left them exactly as unrun as
# they were — a green `--full` saying nothing about the entire frontend suite, and a
# pre-release rehearsal equally blind.
#
# --check-only's exclusion is specifically the `vite build`, whose `prebuild` downloads fonts
# and shells out to `docker buildx` for the FFmpeg.wasm core (measured 91.8 s of a 328 s
# whole-tree pre-commit run, issue #688). Measured on this host 2026-09-07: vitest 29.6 s,
# test:audit:selftest 0.8 s, test:audit 1.4 s — a third of the build's cost, for the only
# evidence in this script that the frontend actually WORKS rather than merely compiles.

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Script configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
CLAUDE_FIX_ENABLED=true
BUILD_ENABLED=true
VERBOSE=false
CLAUDE_TIMEOUT=120

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-claude)
            CLAUDE_FIX_ENABLED=false
            shift
            ;;
        --check-only|--no-build)
            BUILD_ENABLED=false
            shift
            ;;
        --related-only)
            RELATED_ONLY=true
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

print_info()    { echo -e "${BLUE}[frontend-check]${NC} $1"; }
print_success() { echo -e "${GREEN}[frontend-check]${NC} $1"; }
print_error()   { echo -e "${RED}[frontend-check]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[frontend-check]${NC} $1"; }

# `vite build` runs with emptyOutDir, so it rmdirs `dist` before writing anything. A directory
# a root-run container left in the bind mount makes that fail with a bare EACCES and a half-
# emptied `dist` (issue #1372). Say so up front, with the fix. Run from FRONTEND_DIR (run_step).
check_build_output_writable() {
    local blocked
    [ $# -gt 0 ] || set -- dist .svelte-kit
    blocked="$(find "$@" -type d ! -writable -print -quit 2>/dev/null || true)"
    [ -z "$blocked" ] && return 0
    echo "frontend/$blocked is not writable by $(id -un) (owner: $(stat -c %U "$blocked"))."
    echo "A container running as root probably wrote it into the ./frontend bind mount."
    echo "Fix: sudo chown -R $(id -u):$(id -g) frontend/dist frontend/.svelte-kit"
    return 1
}

# Check node_modules exist
check_node_modules() {
    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
        print_info "node_modules not found, installing..."
        # Prefer `npm ci`: a deterministic install from package-lock.json that
        # NEVER rewrites the lockfile. (A newer CI npm pruning optional entries
        # during `npm install` rewrites package-lock.json, which makes the
        # pre-commit hook fail on a modified tracked file.) Fall back to
        # `npm install` only if the lockfile is missing/out of sync so local
        # dev isn't blocked.
        if ! (cd "$FRONTEND_DIR" && npm ci --no-audit --no-fund 2>&1); then
            print_info "npm ci unavailable (lockfile out of sync?), falling back to npm install..."
            if ! (cd "$FRONTEND_DIR" && npm install --no-audit --no-fund 2>&1); then
                print_error "npm install failed"
                exit 1
            fi
        fi
    fi
}

# Generate .svelte-kit types if missing
check_svelte_kit() {
    if [ ! -d "$FRONTEND_DIR/.svelte-kit" ]; then
        print_info "Generating .svelte-kit types..."
        if ! (cd "$FRONTEND_DIR" && npx svelte-kit sync 2>&1); then
            print_warning "svelte-kit sync failed, continuing anyway..."
        fi
    fi
}

# Attempt Claude auto-fix
attempt_claude_fix() {
    local check_output="$1"

    # Check if Claude CLI is available
    if ! command -v claude &>/dev/null; then
        print_warning "Claude CLI not found. Install Claude Code to enable auto-fix."
        return 1
    fi

    print_info "Claude CLI detected. Attempting auto-fix..."

    # Truncate error output if very long (keep first 3000 chars)
    local truncated_output
    if [ "${#check_output}" -gt 3000 ]; then
        truncated_output="${check_output:0:3000}
... (truncated, ${#check_output} total chars)"
    else
        truncated_output="$check_output"
    fi

    local claude_exit=0
    timeout "$CLAUDE_TIMEOUT" claude -p \
        --model claude-sonnet-4-6 \
        --allowedTools "Read,Glob,Grep,Edit" \
        "You are fixing frontend build/type-check errors in a SvelteKit + TypeScript project.

The frontend is located in the 'frontend/' directory.
Path aliases: \$lib -> ./src/lib, \$components -> ./src/components, \$stores -> ./src/stores
TypeScript config: frontend/tsconfig.json (strict: false)

Here are the errors:

\`\`\`
$truncated_output
\`\`\`

Instructions:
1. Read each file mentioned in the errors
2. Fix ONLY the specific errors shown above
3. Do NOT change functionality, refactor, or add comments
4. Do NOT modify tsconfig.json or kit.config.js
5. Make the minimal change needed to resolve each error" \
        2>&1 || claude_exit=$?

    if [ $claude_exit -ne 0 ]; then
        print_warning "Claude auto-fix did not complete successfully (exit code: $claude_exit)"
        return 1
    fi

    print_success "Claude auto-fix completed"

    # Re-stage any frontend files Claude modified
    local modified_files
    modified_files=$(git -C "$PROJECT_ROOT" diff --name-only -- 'frontend/' 2>/dev/null || true)
    if [ -n "$modified_files" ]; then
        print_info "Re-staging modified files..."
        echo "$modified_files" | xargs -I{} git -C "$PROJECT_ROOT" add "{}"
    fi

    return 0
}

# Accumulated across every step of one pass. Set by run_step, read by run_all_checks.
CHECK_FAILED=false
CHECK_OUTPUT=""

# Run one check, in FRONTEND_DIR, recording its output if it fails.
#
# ⚠️ A step must be declared HERE and nowhere else. The recheck-after-Claude-fix path used to
# carry its own hand-written copy of svelte-check + vite build, so any step added to the first
# pass and not to that copy would be silently dropped the moment Claude's fix succeeded: the
# recheck would pass on the two steps it knew about and `return 0` with
# "All frontend checks passed after Claude auto-fix". Adding vitest to a duplicated list is
# how a failing test suite gets reported as a passing gate, so the duplication is gone.
#
# $1 = human label, $2 = grep -E pattern for a one-line failure summary ("" for none),
# $3.. = the command.
run_step() {
    local label="$1"; shift
    local summary_pattern="$1"; shift

    print_info "Running $label..."
    local output
    local exit_code=0
    output=$(cd "$FRONTEND_DIR" && "$@" 2>&1) || exit_code=$?

    if [ $exit_code -ne 0 ]; then
        CHECK_FAILED=true
        CHECK_OUTPUT="${CHECK_OUTPUT}
--- ${label} failures ---
${output}"
        print_error "$label failed"
        if [ -n "$summary_pattern" ]; then
            local summary
            summary=$(echo "$output" | grep -E "$summary_pattern" | head -5 || true)
            if [ -n "$summary" ]; then
                echo "$summary"
            fi
        fi
        if [ "$VERBOSE" = true ]; then
            echo "$output"
        fi
    else
        print_success "$label passed"
        if [ "$VERBOSE" = true ]; then
            echo "$output"
        fi
    fi
}

# The complete check set, in one place, run identically on the first pass and on the
# recheck after a Claude auto-fix.
run_all_checks() {
    CHECK_FAILED=false
    CHECK_OUTPUT=""

    # ESLint (lint gate — passes on warnings, fails only on errors)
    # Content-keyed result cache (base config only; never the type-aware lint:types). The lockfile
    # hash partitions it so a plugin upgrade cannot replay stale findings.
    ESLINT_CACHE_KEY=$(sha256sum "$FRONTEND_DIR/package-lock.json" | cut -c1-12) run_step "eslint" "" npm run lint

    # i18n key parity across all 12 locales.
    # This ran in CI only (.github/workflows/pre-commit.yml), so a local commit that added a
    # user-facing string to en.json alone looked clean and failed the PR instead. Any UI change
    # needs the i18n check, so it belongs in the same local gate as eslint/svelte-check.
    # NOTE: this enforces key PARITY, not translation — a key copied into all 12 files with
    # English text passes here and ships untranslated. Parity is the floor, not the goal.
    run_step "i18n parity check" "" npm run check:i18n

    run_step "svelte-check" "svelte-check found|Error:" \
        npx svelte-check --tsconfig ./tsconfig.json --threshold warning

    # The vitest suite — 189 files, ~1,750 tests — plus the frontend test auditor.
    #
    # These three were invoked by NOTHING outside .github/workflows/pre-commit.yml, so
    # `run-dev-tests.sh --full` and `test-matrix.sh`'s pre-release rehearsal were both green
    # while saying nothing whatsoever about the frontend suite. svelte-check proves the code
    # COMPILES; only this proves it works.
    #
    # Self-test FIRST, and the order is not cosmetic: the auditor's detectors are themselves
    # code that can silently stop matching, and a detector that matches nothing reports zero
    # findings — indistinguishable from a clean suite. Its 27 cases have already caught two
    # dead detectors. Same order CI uses.
    #
    # `test:coverage`, not `test`: the coverage floors in vitest.config.ts are ratchets, and a
    # floor only this script and CI evaluate is the one a commit can see fail. vitest exits
    # non-zero when a threshold is missed, and that line names the metric.
    # Worker cap (issue #77): vitest's own default is maxWorkers '50%' (24 on this 48-core host)
    # PER COMMIT, and several lanes commit at once. Five concurrent hooks drove load to 153 and
    # timed out tests whose code was fine. The hook therefore passes an explicit
    # `--maxWorkers=N` (vitest CLI option, overrides vitest.config.ts); default 8, override with
    # OT_HOOK_VITEST_WORKERS (a number or a percentage like 25%). Only this hook is capped:
    # CI and a bare `npm test` keep the config's '50%'. Coverage floors are unchanged.
    local vitest_workers="${OT_HOOK_VITEST_WORKERS:-8}"
    # Commit stage (--related-only, owner decision 2026-11-11): a one-line CSS change ran all
    # ~1,750 tests, and with several lanes committing the serialised hook queue reached 20-30
    # minutes per commit. `vitest related <files> --run` runs only the tests whose import graph
    # reaches the staged files (https://vitest.dev/guide/cli#vitest-related). The full suite
    # and the coverage floors still run at push stage (frontend-build) and in CI, which runs
    # both stages (test_precommit_stage_ci_parity.py).
    if [ "${RELATED_ONLY:-false}" = true ]; then
        local -a staged=()
        mapfile -t staged < <(git -C "$FRONTEND_DIR/.." diff --cached --name-only --diff-filter=ACMR -- 'frontend/src' | sed 's#^frontend/##')
        if [ "${#staged[@]}" -eq 0 ]; then
            print_info "vitest: no staged frontend files; the full suite runs at push stage"
        else
            run_step "vitest related (${#staged[@]} staged files, ${vitest_workers} workers)" "Tests +[0-9]+ failed|Test Files +[0-9]+ failed" npx vitest related --run --passWithNoTests "--maxWorkers=${vitest_workers}" "${staged[@]}"
        fi
    else
    run_step "vitest (with coverage floors, ${vitest_workers} workers)" "Tests +[0-9]+ failed|Test Files +[0-9]+ failed|Coverage for [a-z]+ .* does not meet" npm run test:coverage -- "--maxWorkers=${vitest_workers}"
    fi
    run_step "frontend test-auditor self-test" "" npm run test:audit:selftest
    run_step "frontend test-quality audit" "" npm run test:audit

    # vite build — the ONE step --check-only skips. Its `prebuild` downloads fonts and shells
    # out to `docker buildx`; measured at 91.8 s of a 328 s whole-tree run (issue #688), which
    # is why it lives at pre-push stage rather than commit stage.
    if [ "$BUILD_ENABLED" = true ]; then
        local writable_out=""
        if writable_out=$(cd "$FRONTEND_DIR" && check_build_output_writable 2>&1); then
            run_step "vite build" "" npm run build
        else
            print_error "$writable_out"
            CHECK_FAILED=true
            CHECK_OUTPUT="${CHECK_OUTPUT}
--- build output writable failures ---
${writable_out}"
        fi
    fi
}

# Main execution
main() {
    check_node_modules
    check_svelte_kit

    run_all_checks

    # If everything passed, done
    if [ "$CHECK_FAILED" = false ]; then
        print_success "All frontend checks passed"
        return 0
    fi

    local check_output="$CHECK_OUTPUT"

    # Checks failed — attempt Claude fix if enabled
    if [ "$CLAUDE_FIX_ENABLED" = true ]; then
        if attempt_claude_fix "$check_output"; then
            # Re-run the SAME set after the Claude fix — not a subset. See run_step's header.
            print_info "Re-running checks after Claude fix..."
            run_all_checks

            if [ "$CHECK_FAILED" = false ]; then
                print_success "All frontend checks passed after Claude auto-fix"
                return 0
            fi

            check_output="$CHECK_OUTPUT"
            print_error "Claude auto-fix was unable to resolve all issues"
        fi
    fi

    # Show errors and fail
    echo ""
    print_error "Frontend checks failed. Errors:"
    echo ""
    echo "$check_output"
    echo ""
    print_error "Please fix the errors above before committing."

    if [ "$CLAUDE_FIX_ENABLED" = false ] && command -v claude &>/dev/null; then
        print_info "Tip: Run without --no-claude to enable auto-fix, or use: claude /fix-frontend"
    elif ! command -v claude &>/dev/null; then
        print_info "Tip: Install Claude Code CLI to enable automatic error fixing."
    fi

    return 1
}

main
