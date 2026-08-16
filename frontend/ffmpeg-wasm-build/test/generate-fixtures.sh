#!/bin/bash
# Regenerates test/fixtures/ — the ~1s container/codec matrix run-matrix.mjs validates
# against. Fixtures are checked in (small, deterministic); re-run this only if the matrix
# needs a new combination. Requires a full host ffmpeg (any recent build — this only
# ENCODES test inputs, unrelated to the minimal decode-free core this directory builds).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/fixtures"

gen() {
  ffmpeg -y -hide_banner -loglevel error \
    -f lavfi -i "sine=frequency=440:duration=1" \
    -f lavfi -i "color=c=blue:s=64x64:d=1" \
    -shortest "$@"
}

gen -c:v libx264 -c:a aac -movflags +faststart mp4_aac.mp4
gen -c:v libx264 -c:a aac mov_aac.mov
gen -c:v libvpx-vp9 -c:a libopus -f matroska mkv_opus.mkv
gen -c:v libvpx-vp9 -c:a libopus -f webm webm_opus.webm
gen -c:v mpeg4 -c:a pcm_s16le avi_pcm.avi
gen -c:v flv -c:a aac -f flv flv_aac.flv
gen -c:v libx264 -c:a aac -f mpegts ts_aac.ts
gen -c:v wmv2 -c:a wmav2 -f asf wmv_wmav.wmv
gen -c:v libtheora -c:a libvorbis -f ogg ogg_vorbis.ogv

ffmpeg -y -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=1" wav_pcm.wav
ffmpeg -y -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=1" -c:a libmp3lame standalone.mp3
ffmpeg -y -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=1" -c:a aac -f adts standalone.aac
ffmpeg -y -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=1" -c:a flac standalone.flac

ls -la
