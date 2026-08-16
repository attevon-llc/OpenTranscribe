// Empirically validates the minimal FFmpeg.wasm core against every container/codec
// combination audioExtractionService.ts's two production commands need to handle.
// Run: node test/run-matrix.mjs (from frontend/ffmpeg-wasm-build/)
import { readFileSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const coreJsPath = join(__dirname, '../dist/dist/esm/ffmpeg-core.js');
const wasmPath = join(__dirname, '../dist/dist/esm/ffmpeg-core.wasm');
const fixturesDir = join(__dirname, 'fixtures');
// Bypass the fetch()-based wasm loader entirely (Node's fetch doesn't support file:// URLs,
// and there's no HTTP server here) — pass the binary directly, same as browsers could via
// Module.wasmBinary.
const wasmBinary = new Uint8Array(readFileSync(wasmPath));

if (!existsSync(coreJsPath)) {
  console.error(`Missing built core at ${coreJsPath} — run build.sh first.`);
  process.exit(1);
}

// The core was built with -sENVIRONMENT=worker (matches how the browser loads it via a
// Worker), so it probes `self` at startup. Node has no `self` global — polyfill it.
if (typeof globalThis.self === 'undefined') {
  globalThis.self = globalThis;
}
if (typeof globalThis.self.location === 'undefined') {
  globalThis.self.location = { href: pathToFileURL(coreJsPath).href };
}
if (typeof globalThis.importScripts === 'undefined') {
  // The core's ENVIRONMENT=worker startup check only probes for this symbol's existence
  // (typeof === 'function'); it's never actually called from Node's code path.
  globalThis.importScripts = () => {
    throw new Error('importScripts is not supported outside a Worker');
  };
}

const { default: createFFmpegCore } = await import(pathToFileURL(coreJsPath).href);

// codec -> output extension/container, mirrors getAudioExtension() in audioExtractionService.ts
const CODEC_TABLE = {
  aac: { ext: 'm4a', muxer: 'ipod' },
  mp3: { ext: 'mp3', muxer: null },
  opus: { ext: 'ogg', muxer: 'ogg' },
  vorbis: { ext: 'ogg', muxer: 'ogg' },
  flac: { ext: 'flac', muxer: null },
  pcm_s16le: { ext: 'wav', muxer: null },
};

// input fixture -> the audio codec it was muxed with (from test/fixtures generation)
const FIXTURES = [
  { file: 'mp4_aac.mp4', codec: 'aac' },
  { file: 'mov_aac.mov', codec: 'aac' },
  { file: 'mkv_opus.mkv', codec: 'opus' },
  { file: 'webm_opus.webm', codec: 'opus' },
  { file: 'avi_pcm.avi', codec: 'pcm_s16le' },
  { file: 'flv_aac.flv', codec: 'aac' },
  { file: 'ts_aac.ts', codec: 'aac' },
  // wmav2 isn't in CODEC_TABLE, but getAudioExtension()'s `|| 'm4a'` fallback means the real
  // app attempts extraction anyway, defaulting to an m4a/ipod container — test that path too.
  { file: 'wmv_wmav.wmv', codec: 'wmav2-unmapped' },
  { file: 'ogg_vorbis.ogv', codec: 'vorbis' },
  { file: 'wav_pcm.wav', codec: 'pcm_s16le' },
  { file: 'standalone.mp3', codec: 'mp3' },
  { file: 'standalone.aac', codec: 'aac' },
  { file: 'standalone.flac', codec: 'flac' },
];

const results = [];

for (const { file, codec } of FIXTURES) {
  const path = join(fixturesDir, file);
  if (!existsSync(path)) {
    results.push({ file, step: 'fixture', pass: false, detail: 'fixture missing' });
    continue;
  }
  const data = readFileSync(path);

  const logs = [];
  const core = await createFFmpegCore({ wasmBinary });
  core.setLogger(({ message }) => logs.push(message));
  core.FS.writeFile(file, new Uint8Array(data));

  // Step 1: metadata read (-vn -c copy -f null -), same as extractMetadata()
  logs.length = 0;
  const metaRet = core.exec('-i', file, '-vn', '-c', 'copy', '-f', 'null', '-');
  const metaOk =
    metaRet === 0 && logs.some((l) => l.includes('Duration:') || l.includes('Stream #'));
  results.push({
    file,
    step: 'metadata (-vn -c copy -f null -)',
    pass: metaOk,
    detail: metaOk ? 'ok' : `ret=${metaRet} logs_tail=${logs.slice(-5).join(' | ')}`,
  });

  // Step 2: audio extraction (-vn -c:a copy <out>), same as _extractAudioInternal(), only
  // for codecs our codec table maps to an output extension (or the getAudioExtension()
  // fallback default 'm4a' for anything unmapped, which the real app also attempts).
  if (codec) {
    const { ext, muxer } = CODEC_TABLE[codec] ?? { ext: 'm4a', muxer: 'ipod' };
    const out = `out_${file}.${ext}`;
    const args = muxer
      ? ['-i', file, '-vn', '-c:a', 'copy', '-f', muxer, out]
      : ['-i', file, '-vn', '-c:a', 'copy', out];
    logs.length = 0;
    const extractRet = core.exec(...args);
    let extractOk = extractRet === 0;
    let outSize = 0;
    if (extractOk) {
      try {
        outSize = core.FS.readFile(out).length;
        extractOk = outSize > 0;
      } catch {
        extractOk = false;
      }
    }
    results.push({
      file,
      step: `extract codec=${codec} -> .${ext}`,
      pass: extractOk,
      detail: extractOk
        ? `ok, ${outSize} bytes`
        : `ret=${extractRet} logs_tail=${logs.slice(-5).join(' | ')}`,
    });
  } else {
    results.push({
      file,
      step: 'extract',
      pass: null,
      detail: 'skipped — codec not in extraction codec table (metadata-only expected)',
    });
  }
}

console.log('\n=== Compatibility matrix ===');
let failCount = 0;
for (const r of results) {
  const mark = r.pass === true ? 'PASS' : r.pass === false ? 'FAIL' : 'SKIP';
  if (r.pass === false) failCount++;
  console.log(`[${mark}] ${r.file} :: ${r.step}${r.pass === false ? ' — ' + r.detail : ''}`);
}
console.log(
  `\n${results.filter((r) => r.pass === true).length} passed, ${failCount} failed, ${
    results.filter((r) => r.pass === null).length
  } skipped`
);
process.exit(failCount > 0 ? 1 : 0);
