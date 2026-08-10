#!/bin/bash
# Provision the small real-speech clips both release scenarios upload.
#
# WHY THIS EXISTS
#
# Both scenarios assert that an uploaded file produces a NON-EMPTY transcript.
# That makes the fixture load-bearing, and it has to be real speech:
#
#   * The e2e suite's sample_audio is a 2-second 440 Hz sine. WhisperX correctly
#     transcribes a tone as nothing, so `segments[]` would be empty and the
#     scenario would fail for a reason that has nothing to do with the release.
#   * `watch/podcast.mp3` looks like a candidate but is synthetic test data from
#     setup-watch-source-test-data.sh — 3 seconds, no speech.
#   * The scenarios cap fixtures at 5 MB, and the repo's real-speech assets are
#     22 MB and 46 MB.
#
# So the clips are DERIVED from a real-speech asset already in the repo: no
# network, no new binary committed, reproducible on any checkout, and the audio
# is genuinely speech so the assertion means something.
#
# TEST_MEDIA_DIR must be missing or these clips absent for this to do anything;
# it never overwrites and never touches anything outside TEST_MEDIA_DIR.
#
# Usage: ./scripts/release-tests/provision-test-media.sh [--force]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TEST_MEDIA_DIR="${TEST_MEDIA_DIR:-/mnt/nvm/opentranscribe-test-runs/test-media}"
FORCE=false
[[ "${1:-}" == "--force" ]] && FORCE=true

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[media]${NC} $*"; }
ok()   { echo -e "${GREEN}[media] ✓${NC} $*"; }
warn() { echo -e "${YELLOW}[media] ⚠${NC} $*" >&2; }
die()  { echo -e "${RED}[media] ✗${NC} $*" >&2; exit 1; }

command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg is required to derive the clips"

# Real-speech sources already in the repo, preferred order. Both were verified to
# have speech-like dynamics (wide mean-to-peak spread) rather than a steady tone.
SOURCES=(
    "$REPO_ROOT/test_videos/The Race to Develop Warp Drive and AI Passing the Turing Test.mp4"
    "$REPO_ROOT/test_videos/test_ai_video.mp4"
)

SOURCE=""
for candidate in "${SOURCES[@]}"; do
    [[ -f "$candidate" ]] && { SOURCE="$candidate"; break; }
done
[[ -n "$SOURCE" ]] || die "no real-speech source found under $REPO_ROOT/test_videos/
Set TEST_MEDIA_DIR to a directory of your own small speech clips instead."

log "source: $(basename "$SOURCE")"
mkdir -p "$TEST_MEDIA_DIR"

# Two clips from different offsets: the scenarios upload up to two files, and
# distinct content makes a per-file transcript diff meaningful rather than
# comparing a file against a copy of itself.
#
# 45 s of 64 kbps mono mp3 is ~360 KB — comfortably under the 5 MB cap, long
# enough for diarization to have something to segment.
make_clip() {
    local name="$1" start="$2" duration="$3"
    local out="$TEST_MEDIA_DIR/$name"

    if [[ -f "$out" && "$FORCE" == false ]]; then
        ok "$name already present ($(du -h "$out" | cut -f1))"
        return 0
    fi

    ffmpeg -hide_banner -loglevel error -y \
        -ss "$start" -t "$duration" -i "$SOURCE" \
        -vn -ac 1 -ar 16000 -b:a 64k \
        "$out"

    [[ -s "$out" ]] || die "ffmpeg produced an empty file: $out"

    # Guard the actual requirement. A silent clip would pass every structural
    # check and then fail the scenario's "segments[] non-empty" assertion an hour
    # later, which is the failure this script exists to prevent.
    local mean
    mean=$(ffmpeg -hide_banner -i "$out" -af volumedetect -f null - 2>&1 \
        | sed -n 's/.*mean_volume: \(-\?[0-9.]*\) dB.*/\1/p' | head -1)
    if [[ -z "$mean" ]]; then
        warn "$name: could not measure volume"
    elif awk -v m="$mean" 'BEGIN{exit !(m+0 < -50)}' 2>/dev/null; then
        die "$name is effectively silent (mean ${mean} dB) — it would transcribe to nothing"
    fi

    ok "$name  $(du -h "$out" | cut -f1)  mean ${mean:-?} dB"
}

make_clip "release-test-clip-a.mp3" 60  45
make_clip "release-test-clip-b.mp3" 180 45

echo
log "TEST_MEDIA_DIR=$TEST_MEDIA_DIR"
find "$TEST_MEDIA_DIR" -maxdepth 1 -type f \
    \( -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.mp4' -o -iname '*.wav' \) \
    -size -5M -printf '  %f  %s bytes\n'
