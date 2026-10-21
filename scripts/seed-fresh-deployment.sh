#!/bin/bash
#
# seed-fresh-deployment.sh — upload a handful of SMALL media files into a
# freshly-started OpenTranscribe deployment via the REST API, so a brand-new
# isolated stack has something to look at.
#
# Invoked by `./opentr.sh start dev --fresh <name> --seed-benchmark`, but also
# runnable standalone:
#
#   BACKEND_URL=http://localhost:5174 bash scripts/seed-fresh-deployment.sh
#
# Degrades gracefully: missing files are skipped, a not-yet-ready backend is
# waited on (bounded), and any failure is non-fatal to the caller.
#
# IDEMPOTENT PER FILE (#1238). The deployment is listed first and a seed file
# the library already holds is skipped, so re-running this against a stack that
# keeps its volumes does not pile up duplicates (it used to upload every file on
# EVERY start: four starts left 12 files and a 6.8% gallery-baseline diff). If
# the listing cannot be read the script uploads NOTHING -- "upload anyway" on an
# unknowable state is exactly the old bug.
#
# "Already present" is decided by NAME, not content: the stored `title` plus the
# stored file's extension, compared with the file's on-disk basename. The stored
# `filename` is NOT usable for this -- the backend sanitizes it
# (`sanitize_filename`), which rewrites 11 of the 40 files in the benchmark
# corpus -- but `title` keeps the original stem. A replaced file of the same name
# is not detected here; scripts/e2e/update-visual-baselines.sh also compares
# sizes before it photographs anything. Do not "fix" this into a hash: the
# server-side fingerprint is imohash, which is not available to a bash script.
#
# `--manifest` prints the files this script WOULD seed (`# source: ...` first,
# then `<basename><TAB><size_bytes>`) without any network call. The capture
# wrapper derives its expected corpus from it, so the expectation can never
# drift from the code that creates the corpus.
#
# Credentials: fresh stacks seed the default admin (admin@example.com / password)
# on first boot via initial_data.py, so no extra setup is required.

set -uo pipefail

MANIFEST_ONLY=0
for _arg in "$@"; do
  case "$_arg" in
    --manifest) MANIFEST_ONLY=1 ;;
    *)
      echo "seed-fresh-deployment.sh: unknown argument: $_arg (try --manifest)" >&2
      exit 2
      ;;
  esac
done

BACKEND_URL="${BACKEND_URL:-http://localhost:5174}"
SEED_EMAIL="${SEED_EMAIL:-admin@example.com}"
SEED_PASSWORD="${SEED_PASSWORD:-password}"
WAIT_TIMEOUT="${SEED_WAIT_TIMEOUT:-120}"

# Seed-media selection, best first:
#   1. SEED_MEDIA_DIR (or ./benchmark/test_audio when present, gitignored):
#      REAL speech recordings — picks the SEED_MAX_FILES smallest media files,
#      so a fresh deployment starts with genuine transcripts/speakers.
#   2. Fallback: the tiny e2e fixtures. These are SYNTHETIC SILENCE — the
#      pipeline will (correctly) mark them "no audio content detected", which
#      is still a useful end-to-end smoke but looks like an error in the UI,
#      so we say so loudly.
# `benchmark/` is gitignored, so the real corpus exists ONLY in the main checkout. A relative
# default silently resolved to nothing when this ran from a git worktree
# (`.claude/worktrees/<name>`), and the script then seeded synthetic silence that the pipeline
# correctly marks as errors (#973). `git rev-parse --git-common-dir` names the main checkout's
# .git from any worktree, so look there. It returns a RELATIVE ".git" in the main checkout
# itself, which resolves to the current directory -- the same answer.
_default_seed_media_dir() {
  local common root
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=""
  if [ -n "$common" ]; then
    root="$(cd "$(dirname "$common")" 2>/dev/null && pwd)" || root=""
    if [ -n "$root" ] && [ -d "$root/benchmark/test_audio" ]; then
      echo "$root/benchmark/test_audio"
      return 0
    fi
  fi
  echo "benchmark/test_audio"
}
SEED_MEDIA_DIR="${SEED_MEDIA_DIR:-$(_default_seed_media_dir)}"
SEED_MAX_FILES="${SEED_MAX_FILES:-3}"
SEED_MAX_MB="${SEED_MAX_MB:-200}"

# The ONE place seed media is chosen. Both the real seeding run and `--manifest` go through
# it, so what is expected and what is uploaded cannot drift apart.
CANDIDATES=()
FILES=()
SYNTHETIC_FALLBACK=0
select_candidates() {
  local f
  CANDIDATES=()
  FILES=()
  SYNTHETIC_FALLBACK=0
  if [ -d "$SEED_MEDIA_DIR" ]; then
    while IFS= read -r f; do
      CANDIDATES+=("$f")
    done < <(find "$SEED_MEDIA_DIR" -maxdepth 1 -type f \
               \( -name '*.wav' -o -name '*.mp3' -o -name '*.m4a' -o -name '*.mp4' -o -name '*.mkv' -o -name '*.webm' \) \
               -size "-${SEED_MAX_MB}M" -printf '%s\t%p\n' 2>/dev/null \
             | sort -n | head -n "$SEED_MAX_FILES" | cut -f2-)
  fi
  if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    SYNTHETIC_FALLBACK=1
    CANDIDATES=(
      "backend/tests/e2e/fixtures/sample_audio.wav"
      "backend/tests/e2e/fixtures/sample_video.mp4"
    )
  fi
  # Keep only the files that actually exist.
  for f in "${CANDIDATES[@]}"; do
    [ -f "$f" ] && FILES+=("$f")
  done
}
select_candidates

if [ "$MANIFEST_ONLY" -eq 1 ]; then
  if [ "$SYNTHETIC_FALLBACK" -eq 1 ]; then
    echo "# source: synthetic"
  else
    echo "# source: ${SEED_MEDIA_DIR}"
  fi
  for f in ${FILES[@]+"${FILES[@]}"}; do
    printf '%s\t%s\n' "$(basename "$f")" "$(stat -c %s "$f")"
  done
  exit 0
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "⚠️  seed: curl not found — skipping seed."
  exit 0
fi

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "⚠️  seed: no seed media found (looked for ${CANDIDATES[*]}) — skipping."
  exit 0
fi

# Wait (bounded) for the backend health endpoint.
echo "⏳ seed: waiting for backend at ${BACKEND_URL} (up to ${WAIT_TIMEOUT}s)..."
deadline=$(( $(date +%s) + WAIT_TIMEOUT ))
until curl -fsS "${BACKEND_URL}/api/health" >/dev/null 2>&1 \
   || curl -fsS "${BACKEND_URL}/health" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "⚠️  seed: backend not ready after ${WAIT_TIMEOUT}s — skipping seed."
    exit 0
  fi
  sleep 3
done

# Log in for an access token.
TOKEN="$(curl -fsS -X POST "${BACKEND_URL}/api/auth/login" \
  --data-urlencode "username=${SEED_EMAIL}" \
  --data-urlencode "password=${SEED_PASSWORD}" 2>/dev/null \
  | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"

if [ -z "${TOKEN}" ]; then
  echo "⚠️  seed: could not obtain an access token (admin not seeded yet?) — skipping."
  exit 0
fi

if [ "$SYNTHETIC_FALLBACK" -eq 1 ]; then
  echo "⚠️  seed: no real media found in '${SEED_MEDIA_DIR}' — seeding the synthetic e2e"
  echo "   fixtures instead. They contain NO SPEECH: the pipeline will correctly mark"
  echo "   them 'no audio content detected' (shows as an error chip in the gallery)."
  echo "   Set SEED_MEDIA_DIR=/path/to/real/media for genuine seed transcripts."
fi

# What the deployment already holds, as one `<name><TAB><size>` line per file. Paginated:
# /api/files defaults to 20 per page and caps at 100, so a single request is a window onto
# the library, not the library. A non-zero exit means the listing could not be read.
# NOTE: the Python below sits inside bash single quotes, so it must contain none.
list_existing() {
  OT_SEED_URL="$BACKEND_URL" OT_SEED_TOKEN="$TOKEN" python3 -I -c '
import json, os, sys, urllib.request
from pathlib import PurePosixPath

base, token = os.environ["OT_SEED_URL"], os.environ["OT_SEED_TOKEN"]
page, total_pages = 1, 1
while page <= total_pages:
    req = urllib.request.Request(
        f"{base}/api/files?page={page}&page_size=100",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    items = body["items"]
    total_pages = int(body["total_pages"])
    for f in items:
        name = f["filename"]
        title = f.get("title") or PurePosixPath(name).stem
        size = f.get("file_size")
        print(f"{title}{PurePosixPath(name).suffix}\t{size}")
    page += 1
'
}

EXISTING=""
if ! EXISTING="$(list_existing)"; then
  echo "⚠️  seed: could not read the deployment's file list — uploading NOTHING rather than"
  echo "   risking duplicates (re-run once the backend answers /api/files)."
  echo "🌱 seed: uploaded 0, already present 0, failed 0 of ${#FILES[@]} file(s)."
  exit 0
fi
EXISTING_NAMES="$(cut -f1 <<< "$EXISTING")"

uploaded=0
present=0
failed=0
for f in "${FILES[@]}"; do
  name="$(basename "$f")"
  # A here-string, not `printf | grep -q`: under pipefail, grep exiting early SIGPIPEs the
  # printf and the pipeline's 141 reads as "not present" -- a duplicate upload.
  if grep -Fxq -- "$name" <<< "$EXISTING_NAMES"; then
    present=$((present + 1))
    echo "   = ${name} (already present)"
    continue
  fi
  # curl defaults the part to application/octet-stream, which the upload
  # endpoint rejects ("File must be an audio or video format") — declare the
  # real MIME type from the extension.
  case "$name" in
    *.wav) mime="audio/wav" ;;
    *.mp3) mime="audio/mpeg" ;;
    *.m4a) mime="audio/mp4" ;;
    *.mp4) mime="video/mp4" ;;
    *.mkv) mime="video/x-matroska" ;;
    *.webm) mime="video/webm" ;;
    *) mime="application/octet-stream" ;;
  esac
  echo "📤 seed: uploading ${name} (${mime})..."
  if curl -fsS -X POST "${BACKEND_URL}/api/files" \
       -H "Authorization: Bearer ${TOKEN}" \
       -F "file=@${f};type=${mime}" >/dev/null 2>&1; then
    uploaded=$((uploaded + 1))
    echo "   ✓ ${name}"
  else
    failed=$((failed + 1))
    echo "   ✗ ${name} (upload failed — continuing)"
  fi
done

echo "🌱 seed: uploaded ${uploaded}, already present ${present}, failed ${failed} of ${#FILES[@]} file(s)."
exit 0
