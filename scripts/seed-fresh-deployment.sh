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
# Credentials: fresh stacks seed the default admin (admin@example.com / password)
# on first boot via initial_data.py, so no extra setup is required.

set -uo pipefail

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
SEED_MEDIA_DIR="${SEED_MEDIA_DIR:-benchmark/test_audio}"
SEED_MAX_FILES="${SEED_MAX_FILES:-3}"
SEED_MAX_MB="${SEED_MAX_MB:-200}"

CANDIDATES=()
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

if ! command -v curl >/dev/null 2>&1; then
  echo "⚠️  seed: curl not found — skipping seed."
  exit 0
fi

# Collect the files that actually exist.
FILES=()
for f in "${CANDIDATES[@]}"; do
  [ -f "$f" ] && FILES+=("$f")
done
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

uploaded=0
for f in "${FILES[@]}"; do
  name="$(basename "$f")"
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
    echo "   ✗ ${name} (upload failed — continuing)"
  fi
done

echo "🌱 seed: uploaded ${uploaded}/${#FILES[@]} file(s) into the fresh deployment."
exit 0
