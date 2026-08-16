# Provenance

This directory compiles a minimal, license-confirmed FFmpeg.wasm core for issue #473.

## Vendored files

- `src/bind/ffmpeg/{bind.js,export.js,export-runtime.js}` — copied verbatim from
  [`ffmpegwasm/ffmpeg.wasm`](https://github.com/ffmpegwasm/ffmpeg.wasm), commit
  `f876f907c7e9b9bf51d4ed0b913a855a63ae63fc` (2025-09-16). **MIT licensed** (ffmpegwasm's own
  Emscripten bindings — the JS glue that exposes the compiled module, not FFmpeg itself).
- `src/fftools/*` — copied verbatim from the same commit/repo. This is FFmpeg's own
  command-line-tool source (`ffmpeg.c`, `cmdutils.c`, etc.), lightly patched upstream by
  ffmpegwasm for Emscripten compatibility. License headers are unmodified: every file in this
  directory carries FFmpeg's own **LGPL 2.1+** header (verified — none carry a GPL-only
  notice). We did not alter these headers or the files' content.

## Local modifications to vendored files

- `build-scripts/ffmpeg-wasm.sh` and `build-scripts/ffmpeg.sh` (vendored from upstream
  `build/ffmpeg-wasm.sh` / `build/ffmpeg.sh`, same commit — renamed `build/` →
  `build-scripts/` here only because this repo's root `.gitignore` has a blanket `build/`
  rule that would otherwise silently exclude these vendored scripts). Local modifications,
  both required by this repo's pre-commit `shellcheck --severity=warning` gate:
  - `ffmpeg-wasm.sh`: removed the hardcoded `-Llibpostproc -lpostproc` link flags. Legacy
    `libpostproc` isn't produced by `--disable-everything` (it's not a
    demuxer/muxer/parser/bsf/protocol) and stream-copy needs no post-processing filters, so
    linking it unconditionally broke the link step.
  - `ffmpeg-wasm.sh`: quoted `"-I$INSTALL_DIR/include"`, `"-L$INSTALL_DIR/lib"`,
    `-sEXPORTED_FUNCTIONS="$(...)"`, `-sEXPORTED_RUNTIME_METHODS="$(...)"`, and `"$@"`
    (SC2206/SC2207/SC2068) — all fixed values or single command-substitution results, safe to
    quote. `$LDFLAGS` is different: it's genuinely a space-separated multi-flag value meant to
    split into multiple `CONF_FLAGS` array elements, so quoting it would collapse it into one
    and break the build — shellcheck's own suggested fix applies instead: `read -ra
LDFLAGS_ARR <<<"$LDFLAGS"` before the array, then splice `"${LDFLAGS_ARR[@]}"` in
    (a per-line `# shellcheck disable=SC2206` does not work here — shellcheck doesn't honor
    directives on individual elements of a multi-line array literal, confirmed with a minimal
    repro).
  - `ffmpeg.sh`: quoted `"$@"` (SC2068). No other changes to either file.

## What's different from upstream

Upstream's `Dockerfile` builds `@ffmpeg/core` with `--enable-gpl --enable-libx264
--enable-libx265 --enable-libvpx --enable-libmp3lame --enable-libtheora --enable-libvorbis
--enable-libopus --enable-zlib --enable-libwebp --enable-libfreetype --enable-libfribidi
--enable-libass --enable-libzimg` — full transcode support, GPL 2+ as a result (x264/x265 are
GPL-only).

`audioExtractionService.ts` only ever runs `-c copy` / `-c:a copy` (stream copy, no
decode/encode/filter). That needs zero external codec libraries and zero GPL flags — just
FFmpeg's own container demuxers/muxers. This build:

- Skips every external-library builder stage (x264/x265/libvpx/lame/opus/theora/vorbis/zlib/
  libwebp/freetype2/fribidi/harfbuzz/libass/zimg) entirely.
- Configures FFmpeg with `--disable-everything` plus an explicit allowlist of demuxers,
  muxers, parsers, bitstream filters and protocols (see `Dockerfile` and
  `configure-summary.txt` for the final list and the verified `License:` line).
- Links only FFmpeg's own libraries (`avformat`, `avcodec`, `avutil`, `avdevice`, `avfilter`,
  `swresample`, `swscale`, `postproc`) — no external codec libs at all.
- Builds only the single-threaded core (this app never loads `@ffmpeg/core-mt`).

Result: **LGPL 2.1+ only**, confirmed by the captured `configure` output in
`configure-summary.txt` and a zero-count grep for `--enable-gpl`/`--enable-nonfree` in the
build's own configure invocation (see `Dockerfile`, `ffmpeg-builder` stage).

## Upgrading

Re-run `build.sh` against a newer FFmpeg tag/commit if a demuxer/muxer/codec-mapping change in
`audioExtractionService.ts` requires it, and re-verify the compatibility matrix in this
directory's `test/` before merging. Re-pin `src/bind`/`src/fftools` from a newer ffmpegwasm
commit only if the Emscripten toolchain version changes (they're small, stable glue files).
