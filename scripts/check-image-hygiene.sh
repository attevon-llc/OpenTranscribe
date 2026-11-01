#!/bin/bash
# Fail-closed check that a built image carries no developer-machine artifacts.
#
# v0.6.0 published backend and lite images containing /app/mutants (mutmut's mutated
# source), test.db, celerybeat-schedule*, and .import_linter_cache, because `COPY . .`
# ships whatever is on the build host and backend/.dockerignore did not exclude them.
#
# Usage:
#   check-image-hygiene.sh <image> [<image> ...]   scan images (docker run --entrypoint find)
#   check-image-hygiene.sh --list-patterns         print the denylist (one -name glob per line)
#
# Exit codes (same split as security-scan.sh): 0 clean, 1 an artifact was found,
# 2 could not check (unknown image kind, docker failure, image missing). 2 is never a pass.
set -euo pipefail

# find -name globs. Kept as the single home: the unit test derives its cases from this list.
PATTERNS=(
  'mutants' 'mutmut-stats.json'
  '*.db' '*.db-shm' '*.db-wal' '*.sqlite' '*.sqlite3'
  'celerybeat-schedule*' 'celerybeat.pid'
  '.import_linter_cache'
  '.env' '.env.*'
  '.git' '.gitignore'
  '.pytest_cache' '.mypy_cache' '.ruff_cache' '.hypothesis' '.coverage' 'coverage.xml'
  'node_modules'
)

if [[ "${1:-}" == "--list-patterns" ]]; then
  printf '%s\n' "${PATTERNS[@]}"
  exit 0
fi
if [[ $# -eq 0 ]]; then
  echo "usage: $0 <image> [<image> ...] | --list-patterns" >&2
  exit 2
fi

# Roots that hold the shipped payload. Everything else in an image is base-OS content.
root_for_image() {
  case "${1%%:*}" in
    */opentranscribe-backend | */opentranscribe-backend-lite) echo /app ;;
    */opentranscribe-frontend | */opentranscribe-docs) echo /usr/share/nginx/html ;;
    *) return 1 ;;
  esac
}

find_args=()
for p in "${PATTERNS[@]}"; do
  [[ ${#find_args[@]} -gt 0 ]] && find_args+=(-o)
  find_args+=(-name "$p")
done

rc=0
for image in "$@"; do
  if ! root=$(root_for_image "$image"); then
    echo "CANNOT CHECK $image: unrecognised image repository" >&2
    rc=2
    continue
  fi
  status=0
  found=$(docker run --rm --entrypoint find "$image" "$root" \( "${find_args[@]}" \) -print 2>&1) || status=$?
  if [[ $status -ne 0 ]]; then
    echo "CANNOT CHECK $image: find exited $status" >&2
    printf '%s\n' "$found" | tail -n 5 >&2
    [[ $rc -lt 2 ]] && rc=2
    continue
  fi
  if [[ -n "$found" ]]; then
    echo "FAIL $image contains developer artifacts:" >&2
    printf '%s\n' "$found" | head -n 40 | sed 's/^/  /' >&2
    [[ $rc -eq 0 ]] && rc=1
  else
    echo "ok   $image"
  fi
done
exit "$rc"
