#!/bin/bash
# Seed Watch Source test data (issue #26).
#
# Generates small ffmpeg test-tone media that exercises every watch-source code
# path, into:
#   - the local watch folder (WATCH_HOST_PATH, default ./watch)   [always]
#   - a MinIO "watch-source-test" bucket                           [if MinIO up]
#   - the SMB test share (opentranscribe-smb-test:/share)         [if running]
#
# Files generated:
#   meeting_2026_P001.mp4 / _P002.mp4 / _P003.mp4  → multi-part stitch group
#   standalone_talk.mp4                            → normal import
#   duplicate_of_talk.mp4                          → content dup of standalone (dedup)
#   old_recording.mp4 (mtime 90 days ago)          → age-skip
#   notes.txt                                      → extension filtering (ignored)
#   podcast.mp3                                     → audio import
#
# Usage: bash scripts/setup-watch-source-test-data.sh [target_dir]

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[watch-test]${NC} $1"; }
success() { echo -e "${GREEN}[watch-test]${NC} $1"; }
warn()    { echo -e "${YELLOW}[watch-test]${NC} $1"; }
err()     { echo -e "${RED}[watch-test]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_DIR="${1:-${WATCH_HOST_PATH:-$REPO_ROOT/watch}}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  err "ffmpeg not found on the host. Install ffmpeg or run inside the backend container."
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

gen_video() {  # name duration freq
  ffmpeg -y -loglevel error \
    -f lavfi -i "testsrc=duration=$2:size=320x240:rate=15" \
    -f lavfi -i "sine=frequency=$3:duration=$2" \
    -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$WORK/$1" </dev/null
}

gen_audio() {  # name duration freq
  ffmpeg -y -loglevel error \
    -f lavfi -i "sine=frequency=$3:duration=$2" -c:a libmp3lame "$WORK/$1" </dev/null
}

info "Generating test media with ffmpeg..."
gen_video "meeting_2026_P001.mp4" 3 440
gen_video "meeting_2026_P002.mp4" 3 494
gen_video "meeting_2026_P003.mp4" 3 523
gen_video "standalone_talk.mp4" 4 660
cp "$WORK/standalone_talk.mp4" "$WORK/duplicate_of_talk.mp4"
gen_video "old_recording.mp4" 2 330
gen_audio "podcast.mp3" 3 392
echo "These are reviewer notes, not media." > "$WORK/notes.txt"
success "Generated $(find "$WORK" -type f | wc -l) test files"

# ---- local folder ----
info "Seeding local watch folder: $TARGET_DIR"
mkdir -p "$TARGET_DIR"
cp "$WORK"/* "$TARGET_DIR"/
# Backdate the old file ~90 days so skip_files_older_than_days catches it.
if touch -d "90 days ago" "$TARGET_DIR/old_recording.mp4" 2>/dev/null; then
  success "Backdated old_recording.mp4 (~90 days) for age-skip testing"
else
  warn "Could not backdate old_recording.mp4 (touch -d unsupported)"
fi
chown -R 1000:1000 "$TARGET_DIR" 2>/dev/null || true
success "Local folder seeded"

# ---- MinIO S3 bucket (optional) ----
# Seed via the backend container's boto3 (always present) rather than mc, which
# isn't reliably available in the MinIO image.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q opentranscribe-backend; then
  info "Seeding MinIO bucket 'watch-source-test' via backend boto3..."
  for f in standalone_talk.mp4 podcast.mp3 meeting_2026_P001.mp4 meeting_2026_P002.mp4; do
    docker cp "$WORK/$f" "opentranscribe-backend:/tmp/$f" 2>/dev/null || true
  done
  docker exec -w /app opentranscribe-backend sh -c 'PYTHONPATH=/app python3 - <<PY 2>/dev/null
import os, boto3
c = boto3.client("s3", endpoint_url="http://minio:9000",
                 aws_access_key_id=os.getenv("MINIO_ROOT_USER", "minioadmin"),
                 aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"),
                 use_ssl=False)
try:
    c.create_bucket(Bucket="watch-source-test")
except Exception:
    pass
for f in ("standalone_talk.mp4", "podcast.mp3", "meeting_2026_P001.mp4", "meeting_2026_P002.mp4"):
    p = "/tmp/" + f
    if os.path.exists(p):
        c.upload_file(p, "watch-source-test", f)
print("ok")
PY' >/dev/null 2>&1 && success "MinIO bucket 'watch-source-test' seeded (endpoint: http://minio:9000)" ||
    warn "Could not seed S3 bucket via backend"
else
  warn "Backend container not running; skipped S3 seed"
fi

# ---- SMB test share (optional) ----
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q opentranscribe-smb-test; then
  info "Seeding SMB test share..."
  for f in standalone_talk.mp4 podcast.mp3 meeting_2026_P001.mp4 meeting_2026_P002.mp4 meeting_2026_P003.mp4; do
    docker cp "$WORK/$f" "opentranscribe-smb-test:/share/$f" 2>/dev/null || true
  done
  success "SMB share seeded (smb://smb-test/media — testuser/testpass)"
else
  warn "SMB test container not running; start with --with-smb-test to seed it"
fi

echo ""
success "Done. Configure watch sources in Settings → Watch Sources, then 'Scan Now'."
