---
sidebar_position: 11
---

# Client-Side FFmpeg.wasm Build (Internals)

This page documents the self-compiled FFmpeg.wasm core used for client-side video→audio
extraction ([issue #473](https://github.com/attevon-llc/OpenTranscribe/issues/473)): why it
exists, what it can and can't do, its license, and its size/build-time footprint.

## Purpose

`frontend/src/lib/services/audioExtractionService.ts` lets the browser strip the audio track
out of a video file **before upload**, so a multi-gigabyte video never has to cross the network
just to hand the backend an audio stream it's going to extract anyway. It runs entirely
client-side via [FFmpeg.wasm](https://github.com/ffmpegwasm/ffmpeg.wasm) (`@ffmpeg/ffmpeg` +
`@ffmpeg/util`, both MIT), using exactly two FFmpeg commands, both stream-copy only — no
decode, no encode, no filters:

| Operation | Command | Purpose |
|---|---|---|
| Metadata read | `ffmpeg -i <input> -vn -c copy -f null -` | Reads container metadata (title/artist/duration/codec) from the log output; writes nothing |
| Audio extraction | `ffmpeg -i <input> -vn -c:a copy <output>` | Copies the audio stream byte-for-byte into a new container, discarding video |

## Why self-compiled instead of the published `@ffmpeg/core`

The published `@ffmpeg/core` package (what this app used to fetch from unpkg at build time) is
compiled by its own upstream project with `--enable-gpl --enable-libx264 --enable-libx265
--enable-libmp3lame` and similar flags. FFmpeg's license is conditional — LGPL 2.1+ by default,
but the moment any GPL-only component is linked in, the whole binary becomes **GPL 2+**. That
generic build is confirmed GPL 2+, not merely ambiguous.

Since this app's only two operations are stream-copy, none of that is needed: a vanilla FFmpeg
build with **zero external codec libraries** and **zero** `--enable-gpl`/`--enable-nonfree`
flags covers the exact use case, and is fully **LGPL 2.1+**. `frontend/ffmpeg-wasm-build/`
compiles exactly that, forked from `ffmpegwasm/ffmpeg.wasm`'s own build toolchain (pinned commit
`f876f907c7e9b9bf51d4ed0b913a855a63ae63fc` — see `PROVENANCE.md` in that directory) so the
WASM-specific FFmpeg patches don't have to be rediscovered from scratch, with the dependency
pyramid of external codec libraries (x264, x265, libvpx, lame, opus, vorbis, theora, zlib,
libwebp, freetype, fribidi, harfbuzz, libass, zimg) removed entirely, since none of it is linked.

## Alternative considered: Mediabunny

[Mediabunny](https://mediabunny.dev/) (npm `mediabunny`, MPL-2.0) was evaluated side by side
before settling on the self-compiled FFmpeg core, because it sidesteps the FFmpeg licensing
question a different way: it's 100% TypeScript with **zero compiled WASM binary**, using the
browser's native [WebCodecs API](https://developer.mozilla.org/en-US/docs/Web/API/WebCodecs_API)
for decode/encode and implementing container demuxing/muxing itself in JS. No FFmpeg anywhere
in the stack means no FFmpeg license question at all — genuinely appealing for exactly the
reason this issue exists. Its `Conversion` API also does what this app needs: given
`video: { discard: true }`, it copies audio without re-encoding whenever the source codec is
compatible with the target container, falling back to a real WebCodecs transcode otherwise.

It was implemented (`mediabunnyExtractionService.ts`, mirroring `audioExtractionService.ts`'s
two operations) and run through the same 13-fixture matrix as the FFmpeg core, in the same
real-browser E2E test, before being removed again. Two real gaps decided it, not vendor docs:

| | Self-compiled FFmpeg core | Mediabunny |
|---|---|---|
| Container support | mov, matroska, avi, flv, mpegts, asf, ogg, wav, mp3, aac, flac | ISOBMFF/mov, Matroska/WebM, Ogg, MP3, WAV, ADTS, FLAC, MPEG-TS, HLS — **no AVI, FLV, or WMV/ASF** |
| Runtime dependency | WebAssembly + Worker (near-universal) | [WebCodecs API](https://caniuse.com/webcodecs) — ~93.6% global support, but **missing entirely** on Firefox versions before 130 and Safari versions before 16.4 |
| Compiled binary | ~2.4 MB, self-hosted | None — pure JS/TS |
| License | LGPL 2.1+ (FFmpeg) | MPL-2.0 |

The FFmpeg core has neither gap: it works in any WASM-capable browser regardless of WebCodecs
support, and the AVI/FLV/WMV fixtures that fail against Mediabunny extract cleanly against it.
Since this app doesn't restrict what video containers a user can upload, losing three real-world
container formats was the deciding factor — **not** the WebCodecs browser-support gap alone,
which is closing over time and would eventually be moot on its own.

Given those gaps, Mediabunny was **removed from the codebase entirely** rather than kept as an
unused, unwired option: no `mediabunny` dependency, no service module, and it isn't documented
anywhere as something this app can use. If browser WebCodecs support and Mediabunny's own
container coverage both broaden enough to close the gap above, it's worth re-evaluating — the
comparison methodology (real fixtures, real browser, `backend/tests/e2e/test_audio_extraction.py`)
is what to reuse, not just re-reading the vendor docs.

## License

Confirmed by FFmpeg's own `./configure`, captured at build time into
`frontend/ffmpeg-wasm-build/configure-summary.txt`:

```
License: LGPL version 2.1 or later

config.h license macros:
#define CONFIG_GPL 0
#define CONFIG_NONFREE 0
#define CONFIG_VERSION3 0
#define CONFIG_GPLV3 0

gpl/nonfree flags passed to configure (must be 0):
0
```

Zero encoders, zero filters, exactly one native decoder compiled in (AAC — needed only to probe
raw-ADTS audio streams during demuxing, not to decode for output). The vendored WASM bindings
(`frontend/ffmpeg-wasm-build/src/bind/`) are MIT (from `ffmpegwasm/ffmpeg.wasm`); the vendored
`src/fftools/` sources are FFmpeg's own code, unmodified license headers, LGPL 2.1+ — see
`PROVENANCE.md` for the exact provenance of both.

Full inventory entry: `.legal/02-licensing-ip/MASTER-LICENSE-INVENTORY.md` §8 (internal,
gitignored — not published in this repo).

## What it can and can't do

Scoped to exactly what the two commands above need — nothing more:

- **Demuxers** (input containers): `mov` (mp4/mov/m4a/3gp/mj2), `matroska` (mkv/webm), `avi`,
  `flv`, `mpegts`, `asf` (wmv), `ogg`, `wav`, `mp3`, `aac`, `flac`.
- **Muxers** (output containers): `ipod` (m4a, holds AAC), `mp3`, `ogg` (opus/vorbis), `wav`,
  `flac`, `null` (metadata-read sink).
- **Parsers**: `aac`, `mpegaudio`, `vorbis`, `opus`, `flac`.
- **Bitstream filters**: `aac_adtstoasc` (ADTS→ASC, needed when copying AAC into an mp4-family
  container), `extract_extradata`.
- **No encoders, no decoders (bar the one AAC probing decoder above), no filters.** Every
  operation is a stream copy — if a source codec doesn't fit the target container's muxer, the
  extraction fails with a clear error rather than silently transcoding or producing empty
  output (see the codec-mapping gap below).

Verified against a 13-fixture container×codec matrix, both in a standalone Node harness
(`frontend/ffmpeg-wasm-build/test/run-matrix.mjs`) and — more importantly — against the real
compiled core loaded in a real browser, in `backend/tests/e2e/test_audio_extraction.py`
(13/13 passing).

**Known gap, not fixed by this build:** `getAudioExtension()` in `audioExtractionService.ts`
doesn't map `wmav2` to a working output container — it falls back to `m4a`, and no FFmpeg build
(minimal or full) can stream-copy `wmav2` into an mp4/ipod-muxer container. This is a
pre-existing, unrelated app-level gap; the extraction path now fails loudly on it instead of
silently producing an empty file.

## Size and build time

| | This build | Previous (`@ffmpeg/core@0.12.6` CDN fetch) |
|---|---|---|
| `ffmpeg-core.js` | 207,778 bytes (~203 KB) | — |
| `ffmpeg-core.wasm` | 2,246,577 bytes (~2.14 MB) | ~31 MB |
| **Total** | **~2.4 MB** | **~31 MB (~13x larger)** |

Compile time, measured on a 48-core / 503 GB RAM host:

| Scenario | Time |
|---|---|
| Clean build (`docker buildx build --no-cache`), `emscripten/emsdk:3.1.40` base image already pulled | **~2m17s** |
| Incremental rebuild, `ffmpeg-wasm-build/Dockerfile` unchanged (the normal case — Docker layer cache hits every stage) | **A few seconds** |
| One-time cost on a machine that has never built this before | add the `emscripten/emsdk:3.1.40` base image pull, **~652 MB** compressed |

Slower hosts (fewer cores, no local emsdk image, cold Docker Hub rate limits) will take longer,
mainly in the FFmpeg `./configure`/`make` step (`build-scripts/ffmpeg.sh`) — CPU-bound and
already using `emmake make -j` (all available cores).

## Where it's used

- **Local/dev builds** (`./opentr.sh start dev`, or `npm run build` run directly on the host):
  `frontend/scripts/build-ffmpeg.js` (the `prebuild` npm script) builds it via
  `frontend/ffmpeg-wasm-build/build.sh` if `static/ffmpeg/{ffmpeg-core.js,ffmpeg-core.wasm}`
  aren't already present and above the size floor, and copies the result in. If Docker isn't
  available, it warns and skips — client-side extraction is an optional, lazily-loaded feature,
  so a missing core doesn't fail the build.
- **Production images**: `frontend/Dockerfile.prod` compiles the identical core as its own
  early build stage (kept in sync by hand with `ffmpeg-wasm-build/Dockerfile` — see the comment
  at the top of each file) and `COPY`s the output into the image before `npm run build` runs.
  **No CDN fetch happens anywhere in the production build.**
- Verified end to end for both paths: the dev container serves `/ffmpeg/ffmpeg-core.{js,wasm}`
  at the sizes above via Vite, and `frontend/Dockerfile.prod` built standalone produces an image
  where nginx serves the same files at the same sizes with the correct `application/wasm`
  content type.

## Rebuilding after a configure-flag change

Any change to the enabled demuxers/muxers/parsers/bitstream-filters must be made in **both**
`frontend/ffmpeg-wasm-build/Dockerfile` (the standalone entry point for local iteration and
`frontend/ffmpeg-wasm-build/test/run-matrix.mjs`) and `frontend/Dockerfile.prod`'s early build
stages (what actually ships) — they are not generated from a shared source, by design (keeping
the production Dockerfile self-contained, with no dependency on a sibling directory's Docker
context, was judged more important than eliminating the duplication). After changing either,
regenerate `configure-summary.txt` and re-run the compatibility matrix before shipping.
