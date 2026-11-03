#!/bin/bash
# Fail-closed check that a built image carries no developer-machine artifacts.
#
# v0.6.0 published backend and lite images containing /app/mutants (mutmut's mutated
# source), test.db, celerybeat-schedule*, and .import_linter_cache, because `COPY . .`
# ships whatever is on the build host and backend/.dockerignore did not exclude them.
#
# Usage:
#   check-image-hygiene.sh <image> [<image> ...]   scan local images
#   check-image-hygiene.sh --list-patterns         print the denylist (one basename glob per line)
#
# The image is never run: `docker create` + `docker export` reads the filesystem, so the check
# works for any architecture leg on any host and executes nothing from the image. Only the
# container this script created is removed; images are never pulled or removed.
#
# Exit codes (same split as security-scan.sh): 0 clean, 1 an artifact was found,
# 2 could not check (unknown image kind, image not present locally, docker failure).
# 2 is never a pass.
set -euo pipefail

# Basename globs (bash `[[ == ]]` patterns). Kept as the single home: the unit tests derive
# their cases from this list. Matched at every depth below the payload root.
PATTERNS=(
  'mutants' 'mutmut-stats.json' '.mutmut-cache'
  '*.db' '*.db-shm' '*.db-wal' '*.sqlite' '*.sqlite3'
  'celerybeat-schedule*' 'celerybeat.pid'
  '.import_linter_cache'
  '.env' '.env.*'
  '.git' '.gitignore'
  '.pytest_cache' '.mypy_cache' '.ruff_cache' '.hypothesis' '.coverage' 'coverage.xml' 'htmlcov'
  'node_modules'
  '*.pem' '*.key' '*.p12' 'id_rsa*' 'id_ed25519*'
  '.claude' '.vscode' '.idea' '.DS_Store'
  '*.bak' '*.orig' 'backups' '*.log'
)

if [[ "${1:-}" == "--list-patterns" ]]; then
  printf '%s\n' "${PATTERNS[@]}"
  exit 0
fi
if [[ $# -eq 0 ]]; then
  echo "usage: $0 <image> [<image> ...] | --list-patterns" >&2
  exit 2
fi

# Repository name of a reference: drop @digest, then a tag only when its ':' follows the last
# '/' (so a registry host:port is kept), then the registry/namespace path.
repo_basename() {
  local ref="${1%%@*}"
  local last="${ref##*/}"
  [[ "$last" == *:* ]] && ref="${ref%:*}"
  echo "${ref##*/}"
}

# Root (relative, as `docker export` writes it) that holds the shipped payload. Everything
# else in an image is base-OS content.
root_for_image() {
  case "$(repo_basename "$1")" in
    opentranscribe-backend | opentranscribe-backend-lite) echo app ;;
    opentranscribe-frontend | opentranscribe-docs) echo usr/share/nginx/html ;;
    *) return 1 ;;
  esac
}

# Basename globs -> one anchored ERE alternation (only `*` and `.` occur in PATTERNS).
patterns_regex() {
  local p out=""
  for p in "${PATTERNS[@]}"; do
    p="${p//./\\.}"
    p="${p//\*/[^/]*}"
    out+="${out:+|}${p}"
  done
  echo "^(${out})$"
}

# Reads `tar -t` paths on stdin, prints (the shallowest of each tree) those with a denylisted component
# below root $1. awk, not a bash loop: a backend image lists ~10^5 paths.
match_paths() {
  awk -v root="$1" -v rx="$(patterns_regex)" '
    {
      p = $0; sub(/^\.\//, "", p); sub(/\/$/, "", p)
      if (index(p, root "/") != 1) next
      if (last != "" && index(p, last "/") == 1) next
      n = split(substr(p, length(root) + 2), c, "/")
      for (i = 1; i <= n; i++) if (c[i] ~ rx) { last = p; print "/" p; next }
    }'
}

WORK="$(mktemp -d -t ot-hygiene.XXXXXX)"
CID=""
cleanup() {
  if [[ -n "$CID" ]]; then docker rm "$CID" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

rc=0
for image in "$@"; do
  if ! root=$(root_for_image "$image"); then
    echo "CANNOT CHECK $image: unrecognised image repository" >&2
    rc=2
    continue
  fi
  # stdout and stderr are kept apart: only stdout is data, so a warning can never read as an
  # artifact. The command is never executed; it only satisfies `create` for CMD-less images.
  if ! CID=$(docker create --pull never "$image" /ot-hygiene-not-run 2>"$WORK/err"); then
    CID=""
    echo "CANNOT CHECK $image: docker create failed (is the image present locally?)" >&2
    tail -n 5 "$WORK/err" >&2
    rc=2
    continue
  fi
  CID="$(tail -n 1 <<<"$CID")"
  status=0
  docker export "$CID" 2>"$WORK/err" | tar -t >"$WORK/list" 2>>"$WORK/err" || status=$?
  docker rm "$CID" >/dev/null 2>&1 || true
  CID=""
  if [[ $status -ne 0 ]]; then
    echo "CANNOT CHECK $image: export/list exited $status" >&2
    tail -n 5 "$WORK/err" >&2
    rc=2
    continue
  fi
  if ! grep -q -m1 "^\(\./\)\?${root}/" "$WORK/list"; then
    echo "CANNOT CHECK $image: no /$root in the image, so nothing was inspected" >&2
    rc=2
    continue
  fi
  found=$(match_paths "$root" <"$WORK/list")
  if [[ -n "$found" ]]; then
    echo "FAIL $image contains developer artifacts:" >&2
    # awk, not `| head`: under pipefail a reader that stops early SIGPIPEs the writer (141).
    awk 'NR <= 40 { print "  " $0 } END { if (NR > 40) print "  ... " NR - 40 " more" }' \
      <<<"$found" >&2
    [[ $rc -eq 0 ]] && rc=1
  else
    echo "ok   $image"
  fi
done
exit "$rc"
