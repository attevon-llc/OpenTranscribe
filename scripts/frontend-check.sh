#!/bin/bash
# Frontend Build Verification Script
# Runs svelte-check and vite build with optional Claude Code auto-fix
#
# Usage:
#   ./scripts/frontend-check.sh [OPTIONS]
#
# Options:
#   --no-claude      Skip Claude auto-fix on failure
#   --check-only     Only run svelte-check, skip vite build
#   --verbose        Show full output from checks
#   -h, --help       Show this help message

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
        --verbose)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            sed -n '2,13p' "$0" | sed 's/^# \?//'
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
4. Do NOT modify tsconfig.json or svelte.config.js
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

# Main execution
main() {
    check_node_modules
    check_svelte_kit

    local check_output=""
    local check_failed=false

    # Step 0: Run ESLint (lint gate — passes on warnings, fails only on errors)
    print_info "Running eslint..."
    local eslint_output
    local eslint_exit=0
    eslint_output=$(cd "$FRONTEND_DIR" && npm run lint 2>&1) || eslint_exit=$?

    if [ $eslint_exit -ne 0 ]; then
        check_failed=true
        check_output="${check_output}
--- ESLint Errors ---
${eslint_output}"
        print_error "eslint failed"
        if [ "$VERBOSE" = true ]; then
            echo "$eslint_output"
        fi
    else
        print_success "eslint passed"
    fi

    # Step 0b: i18n key parity across all 12 locales.
    # This ran in CI only (.github/workflows/pre-commit.yml), so a local commit that added a
    # user-facing string to en.json alone looked clean and failed the PR instead. Any UI change
    # needs the i18n check, so it belongs in the same local gate as eslint/svelte-check.
    # NOTE: this enforces key PARITY, not translation — a key copied into all 12 files with
    # English text passes here and ships untranslated. Parity is the floor, not the goal.
    print_info "Running i18n parity check..."
    local i18n_output
    local i18n_exit=0
    i18n_output=$(cd "$FRONTEND_DIR" && npm run check:i18n 2>&1) || i18n_exit=$?

    if [ $i18n_exit -ne 0 ]; then
        check_failed=true
        check_output="${check_output}
--- i18n Parity Errors ---
${i18n_output}"
        print_error "i18n parity check failed"
        if [ "$VERBOSE" = true ]; then
            echo "$i18n_output"
        fi
    else
        print_success "i18n parity check passed"
    fi

    # Step 1: Run svelte-check
    print_info "Running svelte-check..."
    local svelte_output
    local svelte_exit=0
    svelte_output=$(cd "$FRONTEND_DIR" && npx svelte-check --tsconfig ./tsconfig.json --threshold warning 2>&1) || svelte_exit=$?

    if [ $svelte_exit -ne 0 ]; then
        check_failed=true
        check_output="$svelte_output"
        local summary
        summary=$(echo "$svelte_output" | grep -E "svelte-check found|Error:" | head -5 || true)
        print_error "svelte-check failed"
        if [ -n "$summary" ]; then
            echo "$summary"
        fi
        if [ "$VERBOSE" = true ]; then
            echo "$svelte_output"
        fi
    else
        print_success "svelte-check passed"
        if [ "$VERBOSE" = true ]; then
            echo "$svelte_output"
        fi
    fi

    # Step 2: Run vite build
    if [ "$BUILD_ENABLED" = true ]; then
        print_info "Running vite build..."
        local build_output
        local build_exit=0
        build_output=$(cd "$FRONTEND_DIR" && npm run build 2>&1) || build_exit=$?

        if [ $build_exit -ne 0 ]; then
            check_failed=true
            check_output="${check_output}
--- Vite Build Errors ---
${build_output}"
            print_error "Vite build failed"
            if [ "$VERBOSE" = true ]; then
                echo "$build_output"
            fi
        else
            print_success "Vite build passed"
            if [ "$VERBOSE" = true ]; then
                echo "$build_output"
            fi
        fi
    fi

    # If everything passed, done
    if [ "$check_failed" = false ]; then
        print_success "All frontend checks passed"
        return 0
    fi

    # Checks failed — attempt Claude fix if enabled
    if [ "$CLAUDE_FIX_ENABLED" = true ]; then
        if attempt_claude_fix "$check_output"; then
            # Re-run checks after Claude fix
            print_info "Re-running checks after Claude fix..."

            local recheck_failed=false

            local recheck_output
            local recheck_exit=0
            recheck_output=$(cd "$FRONTEND_DIR" && npx svelte-check --tsconfig ./tsconfig.json --threshold warning 2>&1) || recheck_exit=$?

            if [ $recheck_exit -ne 0 ]; then
                recheck_failed=true
                print_error "svelte-check still failing after Claude fix"
                echo "$recheck_output"
            else
                print_success "svelte-check passed after fix"
            fi

            if [ "$BUILD_ENABLED" = true ] && [ "$recheck_failed" = false ]; then
                local rebuild_output
                local rebuild_exit=0
                rebuild_output=$(cd "$FRONTEND_DIR" && npm run build 2>&1) || rebuild_exit=$?

                if [ $rebuild_exit -ne 0 ]; then
                    recheck_failed=true
                    print_error "Vite build still failing after Claude fix"
                    echo "$rebuild_output"
                else
                    print_success "Vite build passed after fix"
                fi
            fi

            if [ "$recheck_failed" = false ]; then
                print_success "All frontend checks passed after Claude auto-fix"
                return 0
            fi

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
