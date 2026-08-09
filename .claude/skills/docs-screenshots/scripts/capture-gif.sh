#!/bin/bash
# Assemble already-captured screenshots into a discrete-scene, paletted GIF.
#
# Usage: capture-gif.sh <output-name> <scene-name> [scene-name ...]
#
# Each scene must already exist as ~/bin/browser-tools/screenshots/<scene-name>.png
# (captured via capture.sh or browse.js directly). Frames are held ~1.8s each, matching
# the pacing of the existing docs-site/static/img/opentranscribe-workflow.gif. Output goes
# to docs-site/static/img/<output-name>.gif.
set -e

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <output-name> <scene-name> [scene-name ...]" >&2
  exit 1
fi

OUTPUT_NAME="$1"
shift
SCENES=("$@")

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found" >&2
  exit 1
fi

SCREENSHOTS_DIR="$HOME/bin/browser-tools/screenshots"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
TARGET="$REPO_ROOT/docs-site/static/img/$OUTPUT_NAME.gif"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

FRAME_HOLD_SECONDS="${FRAME_HOLD_SECONDS:-1.8}"
FPS="10"
FRAMES_PER_SCENE=$(awk "BEGIN { printf \"%d\", $FRAME_HOLD_SECONDS * $FPS }")

i=0
for scene in "${SCENES[@]}"; do
  src="$SCREENSHOTS_DIR/$scene.png"
  if [[ ! -f "$src" ]]; then
    echo "Missing scene screenshot: $src" >&2
    exit 1
  fi
  for ((f = 0; f < FRAMES_PER_SCENE; f++)); do
    printf -v frame_name "%s/frame-%04d.png" "$WORKDIR" "$i"
    cp "$src" "$frame_name"
    i=$((i + 1))
  done
done

PALETTE="$WORKDIR/palette.png"
ffmpeg -y -framerate "$FPS" -i "$WORKDIR/frame-%04d.png" \
  -vf "fps=$FPS,scale=1280:-1:flags=lanczos,palettegen" "$PALETTE" \
  -loglevel error

ffmpeg -y -framerate "$FPS" -i "$WORKDIR/frame-%04d.png" -i "$PALETTE" \
  -lavfi "fps=$FPS,scale=1280:-1:flags=lanczos [x]; [x][1:v] paletteuse" \
  "$TARGET" \
  -loglevel error

echo "-> $TARGET"
ls -lh "$TARGET"
