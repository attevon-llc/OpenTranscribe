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
#   fresh-stack     `rel-*` fresh deployments listed by `./opentr.sh fresh-list`:
#                   `./opentr.sh fresh-destroy <name> --yes`.
#   test-leftovers  per --project: `./opentr.sh test-cleanup --project P` (its own registered
#                   shapes and age guard; --dry-run is passed through when not --yes).
# Listed only, never removed: git worktrees, /tmp/ot-redcheck.* trees (remove by hand).
#
# OT_TIDY_OPENTR overrides the opentr.sh path (the unit test points it at a fake).
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
while IFS= read -r name; do
    [ -n "$name" ] || continue
    if $YES; then
        "$OPENTR" fresh-destroy "$name" --yes || rc=1
    else
        echo "  would destroy fresh stack: $name"
    fi
done < <("$OPENTR" fresh-list 2>/dev/null | sed -n 's/^  • \(rel-[^ ]*\) .*/\1/p')

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

echo "== listed only (remove by hand) =="
git worktree list | sed 's/^/  worktree: /'
for d in "${TMPDIR:-/tmp}"/ot-redcheck.*; do [ -d "$d" ] && echo "  scratch: $d"; done
exit "$rc"
