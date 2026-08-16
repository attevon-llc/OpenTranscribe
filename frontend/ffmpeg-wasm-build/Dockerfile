# syntax=docker/dockerfile-upstream:master-labs
#
# Minimal, LGPL-2.1+-only FFmpeg.wasm core for issue #473.
#
# `audioExtractionService.ts` only ever runs `-c copy` / `-c:a copy` (stream copy — no
# decode, no encode, no filters). That needs zero external codec libraries and zero GPL
# flags, unlike the published `@ffmpeg/core` (built with --enable-gpl --enable-libx264 etc).
# See PROVENANCE.md for the full story and configure-summary.txt for the verified license.
#
# Build with: `docker buildx build -o dist --target exportor .` from this directory
# (see build.sh). Produces dist/esm/{ffmpeg-core.js,ffmpeg-core.wasm}.

FROM emscripten/emsdk:3.1.40 AS emsdk-base
ENV INSTALL_DIR=/opt
ENV FFMPEG_VERSION=n5.1.4
ENV CFLAGS="-I$INSTALL_DIR/include $CFLAGS"
ENV CXXFLAGS="$CFLAGS"
ENV LDFLAGS="-L$INSTALL_DIR/lib $LDFLAGS $CFLAGS"
ENV EM_PKG_CONFIG_PATH=$EM_PKG_CONFIG_PATH:$INSTALL_DIR/lib/pkgconfig:/emsdk/upstream/emscripten/system/lib/pkgconfig
ENV EM_TOOLCHAIN_FILE=$EMSDK/upstream/emscripten/cmake/Modules/Platform/Emscripten.cmake
ENV PKG_CONFIG_PATH=$PKG_CONFIG_PATH:$EM_PKG_CONFIG_PATH
# Single-threaded core only — this app never loads @ffmpeg/core-mt.
ENV FFMPEG_ST=1
RUN apt-get update && \
      apt-get install -y pkg-config autoconf automake libtool ragel

# FFmpeg source only — NO external codec library stages (x264/x265/libvpx/lame/opus/
# theora/vorbis/zlib/libwebp/freetype2/fribidi/harfbuzz/libass/zimg). We don't need any of
# them: every operation is a stream copy, so FFmpeg's own container (de)muxers are enough.
FROM emsdk-base AS ffmpeg-base
ADD https://github.com/FFmpeg/FFmpeg.git#$FFMPEG_VERSION /src

# Configure with --disable-everything, then enable only the demuxers/muxers/parsers/bsfs
# actually exercised by audioExtractionService.ts's two commands. No --enable-gpl, no
# --enable-nonfree, no external -lXXX. This is the compliance-critical step — the full
# configure invocation is captured to configure-summary.txt for the .legal/ record.
#
# Two components beyond pure demux/mux, both verified empirically against real fixtures
# (test/run-matrix.mjs) — a "should need nothing else" build silently failed both:
#   - `null` muxer: `-f null -` (the metadata-read command) IS a muxer, just a discarding one.
#     Without it every metadata read failed with "Requested output format 'null' is not a
#     suitable output format" — not an edge case, the common path.
#   - `aac` decoder: raw-ADTS AAC (a standalone .aac upload, or audio inside mpegts) reports
#     "0 channels" during probing and then fails to mux with -c:a copy, because
#     avformat_find_stream_info() falls back to briefly decoding when the ADTS parser alone
#     can't resolve channel config. Confirmed against host ffmpeg (full build) succeeding on
#     the same fixture with the same command — this is a real gap, not a fixture artifact.
#     FFmpeg's native AAC decoder is LGPL (not an external/GPL codec lib), so this doesn't
#     change the license outcome.
FROM ffmpeg-base AS ffmpeg-builder
COPY build-scripts/ffmpeg.sh /src/build.sh
# Needed for the RUN below: without pipefail, `... | tee ...` masks a failing build.sh
# behind tee's own success, silently producing an empty/stale configure-summary.txt.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN bash -x /src/build.sh \
      --disable-everything \
      --enable-protocol=file,pipe \
      --enable-demuxer=mov,matroska,avi,flv,mpegts,asf,ogg,wav,mp3,aac,flac \
      --enable-muxer=ipod,mp3,ogg,wav,flac,null \
      --enable-parser=aac,mpegaudio,vorbis,opus,flac \
      --enable-bsf=aac_adtstoasc,extract_extradata \
      --enable-decoder=aac \
      2>&1 | tee /src/configure-build.log && \
    LICENSE_LINE=$(grep '^license=' /src/ffbuild/config.log | head -1 | cut -d= -f2- | tr -d "'") && \
    { \
      echo "License: $LICENSE_LINE"; \
      echo; \
      echo "config.h license macros:"; \
      grep -E 'CONFIG_(GPL|NONFREE|VERSION3|GPLV3) ' /src/config.h; \
      echo; \
      echo "gpl/nonfree flags passed to configure (must be 0):"; \
      grep -c -- '--enable-gpl\|--enable-nonfree' /src/configure-build.log || true; \
      echo; \
      echo "Enabled demuxers:"; grep '_DEMUXER 1' /src/config_components.h | sed -E 's/#define CONFIG_(.*)_DEMUXER 1/\1/' | tr '\n' ' '; echo; \
      echo "Enabled muxers:"; grep '_MUXER 1' /src/config_components.h | sed -E 's/#define CONFIG_(.*)_MUXER 1/\1/' | tr '\n' ' '; echo; \
      echo "Enabled parsers:"; grep '_PARSER 1' /src/config_components.h | sed -E 's/#define CONFIG_(.*)_PARSER 1/\1/' | tr '\n' ' '; echo; \
      echo "Enabled bsfs:"; grep '_BSF 1' /src/config_components.h | sed -E 's/#define CONFIG_(.*)_BSF 1/\1/' | tr '\n' ' '; echo; \
      echo "Enabled protocols:"; grep '_PROTOCOL 1' /src/config_components.h | sed -E 's/#define CONFIG_(.*)_PROTOCOL 1/\1/' | tr '\n' ' '; echo; \
      echo "Encoders enabled (must be 0): $(grep -c '_ENCODER 1' /src/config_components.h || true)"; \
      echo "Decoders enabled (must be 1: native AAC only, for probing — see comment above):"; \
      grep '_DECODER 1' /src/config_components.h | sed -E 's/#define CONFIG_(.*)_DECODER 1/\1/' | tr '\n' ' '; echo; \
      echo "Filters enabled (must be 0): $(grep -c '_FILTER 1' /src/config_components.h || true)"; \
    } > /src/configure-summary.txt

# Bind FFmpeg's fftools CLI (ffmpeg.c etc, vendored+patched by ffmpegwasm for Emscripten —
# see PROVENANCE.md) into a WASM module via the same emcc pattern ffmpegwasm itself uses,
# minus every external -lXXX (we linked none).
FROM ffmpeg-builder AS ffmpeg-wasm-builder
COPY src/bind /src/src/bind
COPY src/fftools /src/src/fftools
COPY build-scripts/ffmpeg-wasm.sh /src/build.sh
# build-scripts/ffmpeg-wasm.sh (locally modified from upstream — see PROVENANCE.md) already links
# just FFmpeg's own libs, no -lpostproc (legacy libpostproc isn't built by
# --disable-everything and stream-copy needs no post-processing filters) and no external
# codec libs (-lx264 etc — none are linked, none are needed for -c copy).
RUN mkdir -p /src/dist/esm && bash -x /src/build.sh \
      -sEXPORT_ES6 \
      -o dist/esm/ffmpeg-core.js

FROM scratch AS exportor
COPY --from=ffmpeg-wasm-builder /src/dist /dist
COPY --from=ffmpeg-builder /src/configure-summary.txt /configure-summary.txt
