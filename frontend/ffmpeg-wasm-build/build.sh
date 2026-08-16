#!/bin/bash
# Compiles the minimal LGPL-only FFmpeg.wasm core (issue #473) and exports
# dist/esm/{ffmpeg-core.js,ffmpeg-core.wasm} + configure-summary.txt into ./dist.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

DOCKER_BUILDKIT=1 docker buildx build \
  --target exportor \
  -o dist \
  .

# The `-o dist` local exporter dumps the exportor stage's whole rootfs (which itself has
# top-level /dist and /configure-summary.txt paths) into ./dist, so output lands one level
# deeper than it looks: ./dist/dist/esm/ffmpeg-core.{js,wasm} and ./dist/configure-summary.txt.
echo "Build complete. Output:"
ls -la dist/dist/esm dist/configure-summary.txt
