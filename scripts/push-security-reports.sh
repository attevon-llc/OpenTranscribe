#!/bin/bash
set -e

# Push Security Reports Script
# Handles pre-commit hook modifications with retry logic

# shellcheck source=scripts/lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/log.sh"

# Configuration
MAX_RETRIES=3
REPORTS_DIR="./security-reports"
VERSION="${VERSION:-$(cat VERSION 2>/dev/null || echo 'unknown')}"

# Check if security reports exist
if [ ! -d "${REPORTS_DIR}" ] || [ -z "$(ls -A ${REPORTS_DIR} 2>/dev/null)" ]; then
    print_warning "No security reports found in ${REPORTS_DIR}"
    exit 0
fi

# Check for changes
if ! git status --porcelain "${REPORTS_DIR}" | grep -q .; then
    print_info "No changes to security reports"
    exit 0
fi

print_info "Pushing security reports for version ${VERSION}..."
print_info "Reports directory: ${REPORTS_DIR}"

# Function to attempt commit
attempt_commit() {
    local attempt=$1
    print_info "Commit attempt ${attempt}/${MAX_RETRIES}..."

    # Stage all security reports
    git add "${REPORTS_DIR}/"

    # Attempt commit
    if git commit -m "chore: Update security reports for ${VERSION}

Security scan results from Docker build process.
Reports include: Hadolint, Dockle, Trivy, Grype, and Syft SBOM.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>" 2>&1; then
        return 0
    else
        return 1
    fi
}

# Main commit loop with retry logic
COMMITTED=false
for attempt in $(seq 1 ${MAX_RETRIES}); do
    # Stage files
    git add "${REPORTS_DIR}/"

    # Check if there are staged changes
    if ! git diff --cached --quiet; then
        if attempt_commit ${attempt}; then
            COMMITTED=true
            break
        else
            print_warning "Commit attempt ${attempt} failed (pre-commit hooks may have modified files)"

            # Re-stage any files modified by pre-commit hooks
            if git status --porcelain "${REPORTS_DIR}" | grep -q .; then
                print_info "Re-staging files modified by pre-commit hooks..."
                git add "${REPORTS_DIR}/"
            fi

            # Small delay before retry
            sleep 1
        fi
    else
        print_info "No staged changes after attempt ${attempt}"

        # Check if there are still unstaged changes
        if git status --porcelain "${REPORTS_DIR}" | grep -q .; then
            print_info "Found unstaged changes, staging and retrying..."
            git add "${REPORTS_DIR}/"
        else
            print_info "All changes already committed"
            COMMITTED=true
            break
        fi
    fi
done

if [ "${COMMITTED}" = false ]; then
    print_error "Failed to commit security reports after ${MAX_RETRIES} attempts"
    print_info "You may need to commit manually:"
    print_info "  git add ${REPORTS_DIR}/"
    print_info "  git commit --no-verify -m 'chore: Update security reports'"
    exit 1
fi

# Push to current branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
print_info "Pushing to ${CURRENT_BRANCH}..."

if git push origin "${CURRENT_BRANCH}"; then
    print_success "Security reports committed and pushed to ${CURRENT_BRANCH}"
else
    print_error "Failed to push security reports"
    print_info "You may need to push manually: git push origin ${CURRENT_BRANCH}"
    exit 1
fi

# Summary
print_info ""
print_info "Security Reports Summary:"
ls -lh "${REPORTS_DIR}"/*.txt "${REPORTS_DIR}"/*.json 2>/dev/null | awk '{print "  " $9 ": " $5}'
print_success "Done!"
