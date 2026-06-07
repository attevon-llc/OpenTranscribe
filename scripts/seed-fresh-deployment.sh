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

# Candidate SMALL media files (a few KB each) — never the multi-GB benchmark
# corpus. Add more here if richer seed data is wanted.
CANDIDATES=(
  "backend/tests/e2e/fixtures/sample_audio.wav"
  "backend/tests/e2e/fixtures/sample_video.mp4"
)

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
