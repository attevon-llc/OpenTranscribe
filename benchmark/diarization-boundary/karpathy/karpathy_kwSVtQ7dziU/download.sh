#!/bin/bash
# Download + decode the reporter's acceptance clip (issue #193) to 16 kHz mono WAV.
# Run INSIDE the backend container (yt-dlp + ffmpeg live there, not on the host):
#   docker compose exec backend bash benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/download.sh
set -e

URL="https://www.youtube.com/watch?v=kwSVtQ7dziU"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/audio.wav"

if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "yt-dlp not found — run this inside the backend container." >&2
  exit 1
fi

tmp="$(mktemp -d)"
yt-dlp -f bestaudio -o "$tmp/src.%(ext)s" "$URL"
ffmpeg -y -i "$tmp"/src.* -ar 16000 -ac 1 -c:a pcm_s16le "$OUT"
rm -rf "$tmp"

echo "Wrote $OUT"
sha256sum "$OUT"
echo "→ record this sha256 in ../../corpus.json (sha256 field) for reproducibility."
