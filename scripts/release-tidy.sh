#!/bin/bash
# OpenTranscribe — the release closing checklist's mechanical half (issue #1370).
#
# DRY-RUN BY DEFAULT: without --yes this only lists. Every deletion is delegated to an existing,
# scoped tool; nothing here removes anything itself, and nothing here ever runs
# `docker ... prune`, `-a` or `--volumes` (the host Docker daemon is shared).
#
# Usage: scripts/release-tidy.sh [--yes] [--base BRANCH] [--project COMPOSE_PROJECT]...
#
# Shapes (the complete list):
#   merged-branch   local branch fully merged into --base (default master): `git branch -d`,
#                   which refuses an unmerged branch or one checked out in a worktree.
#                   Never the base, release/*, or the current branch.
#   fresh-stack     `rel-*` fresh deployments from `./opentr.sh fresh-list --names`:
#                   `./opentr.sh fresh-destroy <name> --yes`.
#   test-leftovers  per --project: `./opentr.sh test-cleanup --project P` (its own registered
#                   shapes and age guard; --dry-run is passed through when not --yes).
#   scratch-trees  `/tmp/ot-redcheck.*` and the other registered test tmp shapes, through
#                   scripts/cleanup-test-data.py --planes tmp (owner, shape and age guards;
#                   --execute-unambiguous only with --yes). Needs no running stack.
# Listed only, never removed: git worktrees (remove by hand).
#
# OT_TIDY_OPENTR / OT_TIDY_CLEANUP_DATA override the opentr.sh and cleanup-test-data.py
# commands (the unit test points them at fakes).
# Exit: 0 ok · 1 a delegated step failed · 2 misuse.

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2
OPENTR="${OT_TIDY_OPENTR:-$REPO_ROOT/opentr.sh}"

YES=false
BASE=master
PROJECTS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) YES=true; shift ;;
        --base) [ $# -ge 2 ] || { echo "--base needs a branch" >&2; exit 2; }; BASE="$2"; shift 2 ;;
        --project) [ $# -ge 2 ] || { echo "--project needs a name" >&2; exit 2; }; PROJECTS+=("$2"); shift 2 ;;
        -h | --help) sed -n '2,/^set -uo/p' "$0" | sed -e '/^set -uo/d' -e 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

rc=0
mode="DRY RUN (nothing will be changed; pass --yes to act)"
$YES && mode="ACTING (--yes)"
echo "release-tidy: $mode"

echo "== merged-branch (into $BASE) =="
if git rev-parse --verify --quiet "$BASE" >/dev/null; then
    current="$(git rev-parse --abbrev-ref HEAD)"
    while IFS= read -r b; do
        case "$b" in "" | "$BASE" | "$current" | release/*) continue ;; esac
        if $YES; then
            git branch -d "$b" || rc=1
        else
            echo "  would delete branch: $b"
        fi
    done < <(git branch --merged "$BASE" --format='%(refname:short)')
else
    echo "  base branch '$BASE' not found; skipped"
fi

echo "== fresh-stack (rel-*) =="
# Capture first: a failing listing must be an error, never an empty list.
if ! fresh_names="$("$OPENTR" fresh-list --names)"; then
    echo "release-tidy: '$OPENTR fresh-list --names' failed; cannot tell which fresh stacks exist" >&2
    exit 1
fi
while IFS= read -r name; do
    case "$name" in rel-*) ;; *) continue ;; esac
    if $YES; then
        "$OPENTR" fresh-destroy "$name" --yes || rc=1
    else
        echo "  would destroy fresh stack: $name"
    fi
done <<< "$fresh_names"

echo "== test-leftovers =="
if [ ${#PROJECTS[@]} -eq 0 ]; then
    echo "  no --project given; skipped"
fi
for p in "${PROJECTS[@]}"; do
    if $YES; then
        "$OPENTR" test-cleanup --project "$p" --once || rc=1
    else
        "$OPENTR" test-cleanup --project "$p" --once --dry-run || rc=1
    fi
done

echo "== scratch-trees =="
if [ -n "${OT_TIDY_CLEANUP_DATA:-}" ]; then
    cleanup_cmd=("$OT_TIDY_CLEANUP_DATA")
elif [ -x "$REPO_ROOT/backend/venv/bin/python" ]; then
    cleanup_cmd=("$REPO_ROOT/backend/venv/bin/python" "$REPO_ROOT/scripts/cleanup-test-data.py")
else
    cleanup_cmd=(python3 "$REPO_ROOT/scripts/cleanup-test-data.py")
fi
if $YES; then
    "${cleanup_cmd[@]}" --planes tmp --execute-unambiguous || rc=1
else
    "${cleanup_cmd[@]}" --planes tmp || rc=1
fi

echo "== listed only (remove by hand) =="
git worktree list | sed 's/^/  worktree: /'
exit "$rc"
