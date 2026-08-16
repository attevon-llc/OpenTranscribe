#!/bin/bash
# Concurrency guard for pre-commit (issue #434).
#
# WHY
#
# `pre-commit run` -- with or without --all-files -- stashes every unstaged change in the
# WHOLE repository before any hook runs, and restores it when the run ends. Two incidents in
# one checkout showed what that costs when something else is writing during the window:
#
#   1. Two agents shared one worktree. One ran `pre-commit run --all-files`; the other's
#      uncommitted edit was stashed mid-write and the restore reinstated an EARLIER draft,
#      silently discarding the newer one. Caught only by an unrelated `git diff`.
#   2. `run-mutation-tests.sh --verify` transiently mutates a live file in backend/app/ (see
#      that script's own header). A concurrent `pre-commit` stashed the mutation into its
#      patch and reapplied it on restore -- `getattr(getattr(None, "url", ...))` came back in
#      production, caught only by inspection.
#
# Both failures print NO findings at all ("No issues identified", "All frontend checks
# passed") alongside "files were modified by this hook" -- the stash/restore moved the files,
# not the hook. That signature is indistinguishable from a real failure to anyone who has not
# been bitten, and it trains people to just re-run until green.
#
# WHAT THIS DOES
#
# Refuses to start (rather than silently racing) when either is true:
#   * another safe-precommit.sh is already running (its own flock, held for the whole run)
#   * a `run-mutation-tests.sh --verify` run holds one of its per-module locks in
#     $OT_MUTATION_OUT_DIR (default .mutation/) -- the exact hazard from incident 2 above.
#
# This is deliberately the CHEAP option from #434 (a lock, not a stash-safety rewrite): it
# does not make concurrent writers safe, it makes the two KNOWN unsafe overlaps refuse to
# start instead of racing silently. Narrowing the whole-tree hooks (bandit -r, frontend-check)
# to staged paths -- the issue's option 3 -- removes most of the need for this and is not
# attempted here.
#
# USAGE
#
#   scripts/safe-precommit.sh run --all-files
#   scripts/safe-precommit.sh run --files backend/app/foo.py
#
# Any arguments are passed through verbatim to the real `pre-commit`.
#
# SELF-TEST
#
#   scripts/safe-precommit.sh --selftest
#
# Exercises the guards in an isolated temp dir -- never touches the real .mutation/ or the
# real pre-commit lock, and never invokes the real pre-commit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_BIN="$REPO_ROOT/backend/venv/bin"

# In a git WORKTREE `.git` is a FILE, not a directory, so `$REPO_ROOT/.git/...` made the
# `mkdir -p` below fail with "File exists" and this wrapper was unusable in exactly the
# checkout style CLAUDE.md tells you to work in. `--git-dir` resolves to the real
# per-worktree git directory in both layouts.
#
# Deliberately `--git-dir`, NOT `--git-common-dir`: the hazard this lock serialises is
# pre-commit stashing a WORKING TREE, and each worktree has its own. A lock in the shared
# common dir would serialise lanes that cannot interfere with each other, throwing away
# the isolation that is the whole reason for using worktrees here.
GIT_DIR_PATH="$(git -C "$REPO_ROOT" rev-parse --absolute-git-dir 2>/dev/null || echo "$REPO_ROOT/.git")"

# Overridable for --selftest so it never touches the real repo state.
MUTATION_OUT_DIR="${OT_MUTATION_OUT_DIR:-$REPO_ROOT/.mutation}"
PRECOMMIT_LOCK="${OT_PRECOMMIT_LOCK:-$GIT_DIR_PATH/safe-precommit.lock}"

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

# Returns 1 (refuse) if any `--verify` lock under MUTATION_OUT_DIR is currently held by
# another process. A lock FILE existing is not evidence by itself -- verify_survivor()
# creates it once and it is gitignored so it lingers empty after every clean run; only an
# actual flock held on it means a verify is in flight right now.
mutation_verify_in_progress() {
    local lock held=() fd
    shopt -s nullglob
    for lock in "$MUTATION_OUT_DIR"/.verify-*.lock; do
        # A separate fd per iteration: reusing one leaks across loop iterations under `set -e`
        # if an early continue skips the close.
        exec {fd}<>"$lock" 2>/dev/null || continue
        if ! flock -n "$fd"; then
            held+=("$(basename "$lock")")
        else
            flock -u "$fd"
        fi
        exec {fd}<&- 2>/dev/null || true
    done
    shopt -u nullglob
    if (( ${#held[@]} > 0 )); then
        echo -e "${RED}safe-precommit: refusing to start.${NC}" >&2
        echo -e "${RED}  A mutation --verify run holds: ${held[*]}${NC}" >&2
        echo -e "${YELLOW}  --verify transiently mutates live source in backend/app/; pre-commit's${NC}" >&2
        echo -e "${YELLOW}  whole-tree stash can capture the mutation mid-cycle (issue #434).${NC}" >&2
        echo -e "${YELLOW}  Wait for the verify run to finish, then retry.${NC}" >&2
        return 1
    fi
    return 0
}

main() {
    if [[ "${1:-}" == "--selftest" ]]; then
        exec bash "$SCRIPT_DIR/safe-precommit-selftest.sh"
    fi

    if [[ $# -eq 0 ]]; then
        echo "usage: $0 <pre-commit args...>  |  $0 --selftest" >&2
        exit 2
    fi

    mkdir -p "$(dirname "$PRECOMMIT_LOCK")" "$MUTATION_OUT_DIR"

    if ! mutation_verify_in_progress; then
        exit 3
    fi

    exec {precommit_fd}>"$PRECOMMIT_LOCK"
    if ! flock -n "$precommit_fd"; then
        echo -e "${RED}safe-precommit: refusing to start -- another pre-commit run already holds${NC}" >&2
        echo -e "${RED}  $PRECOMMIT_LOCK. Wait for it to finish, then retry.${NC}" >&2
        exit 3
    fi
    # Lock is released automatically when this process exits (fd closes), whether the
    # wrapped pre-commit run passes, fails, or is interrupted.

    local pre_commit_bin="$VENV_BIN/pre-commit"
    if [[ ! -x "$pre_commit_bin" ]]; then
        pre_commit_bin="pre-commit"
    fi

    if [[ "${SAFE_PRECOMMIT_DRY_RUN:-}" == "1" ]]; then
        echo -e "${GREEN}safe-precommit: guards clear -- would run: $pre_commit_bin $*${NC}"
        exit 0
    fi

    echo -e "${GREEN}safe-precommit: guards clear -- running pre-commit${NC}"
    "$pre_commit_bin" "$@"
}

main "$@"
