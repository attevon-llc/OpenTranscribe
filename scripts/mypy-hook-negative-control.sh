#!/bin/bash
# Negative control for the mypy pre-commit hook (issue #1271).
#
# WHY
#
# A green mypy hook proves nothing about its own eyesight. The hook runs in an isolated
# virtualenv containing only its `additional_dependencies`; a package missing from that list
# resolves to `Any` there, and mypy then waves through code that `backend/venv/bin/mypy`
# rejects. At the start of #1271 the hook reported 0 findings over files the venv reported
# 55 for, and nothing noticed for months. Only a deliberately wrong line that the hook is
# REQUIRED to reject can show the hook can still see the types it is supposed to check.
#
# WHAT IT DOES
#
# Builds a throwaway repository from `git archive REF` (default HEAD) under an `ot-redcheck.`
# temp dir, copies scripts/mypy_negative_control_fixture.py.txt into it as
# backend/app/zz_mypy_negative_control.py, and runs the REAL hook on that one file. Every
# `# EXPECT: <code>` line in the fixture must come back as `<file>:<line>: error: ... [<code>]`.
# It runs in a throwaway repo, so it can never stash or touch the checkout it was started from.
#
# USAGE
#
#   scripts/mypy-hook-negative-control.sh               # control against HEAD
#   scripts/mypy-hook-negative-control.sh --ref REF     # control against any commit
#
# Exit codes: 0 every control caught; 1 at least one NOT caught (the hook is blind to it);
# 2 misuse or environment (no pre-commit, bad ref).
#
# Red proof: `--ref <commit before the hook was aligned>` must exit 1 and say "NOT caught".
# On the aligned config it must exit 0 and say "caught 3/3".

set -euo pipefail

REF="HEAD"
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      REF="${2:?--ref needs a value}"
      shift 2
      ;;
    -h | --help)
      sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_REL="scripts/mypy_negative_control_fixture.py.txt"
TARGET_REL="backend/app/zz_mypy_negative_control.py"

# Prefer the pre-commit on PATH (CI), else the backend venv's.
if command -v pre-commit > /dev/null 2>&1; then
  PRE_COMMIT="pre-commit"
elif [ -x "$REPO_ROOT/backend/venv/bin/pre-commit" ]; then
  PRE_COMMIT="$REPO_ROOT/backend/venv/bin/pre-commit"
else
  echo "no pre-commit found (PATH, or backend/venv/bin/pre-commit)" >&2
  exit 2
fi

git -C "$REPO_ROOT" rev-parse --verify --quiet "$REF^{commit}" > /dev/null || {
  echo "unknown ref: $REF" >&2
  exit 2
}

RED="$(mktemp -d -t ot-redcheck.XXXXXX)"
cleanup() {
  # Only ever our own mktemp dir, and only if it still has the shape we made.
  case "$RED" in
    */ot-redcheck.*) rm -rf -- "$RED" ;;
  esac
}
trap cleanup EXIT

git -C "$REPO_ROOT" archive "$REF" | tar -x -C "$RED"
# The fixture is read from the CURRENT tree, so an old --ref is judged against today's control.
cp "$REPO_ROOT/$FIXTURE_REL" "$RED/$TARGET_REL"

cd "$RED"
git init -q
git add -A

echo "negative control: hook config from $(git -C "$REPO_ROOT" rev-parse --short "$REF")"
set +e
OUT="$("$PRE_COMMIT" run mypy --files "$TARGET_REL" 2>&1)"
RC=$?
set -e

total=0
caught=0
while IFS=: read -r lineno code; do
  total=$((total + 1))
  code="${code#* EXPECT: }"
  code="${code%% *}"
  # A here-string, not `printf | grep -q`: under pipefail grep -q's early exit can SIGPIPE the producer.
  if grep -Eq "zz_mypy_negative_control\.py:${lineno}: error: .*\[${code}\]" <<< "$OUT"; then
    echo "  caught       line ${lineno} [${code}]"
    caught=$((caught + 1))
  else
    echo "  NOT caught   line ${lineno} [${code}]"
  fi
done < <(grep -n '# EXPECT: ' "$TARGET_REL" | sed -E 's/^([0-9]+):.*(# EXPECT: .*)$/\1:\2/')

if [ "$total" -eq 0 ]; then
  echo "fixture has no EXPECT markers; the control itself is broken" >&2
  exit 2
fi

if [ "$caught" -eq "$total" ] && [ "$RC" -ne 0 ]; then
  echo "caught ${caught}/${total}: the mypy hook rejects every deliberately wrong line"
  exit 0
fi

echo "control NOT caught: ${caught}/${total} expected errors reported (hook exit ${RC})"
echo "--- hook output ---"
printf '%s\n' "$OUT"
exit 1
