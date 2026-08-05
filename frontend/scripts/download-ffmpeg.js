/**
 * Fetch the FFmpeg.wasm core into `static/ffmpeg/` so the production image can serve
 * `/ffmpeg/*` itself instead of reaching out to a CDN at runtime.
 *
 * `static/ffmpeg/` is gitignored (the core is ~31 MB), so without this prebuild step a
 * clean checkout builds an image with no `/ffmpeg/*` at all and client-side audio
 * extraction fails at load time. Same pattern as `download-fonts.js`, with one
 * difference: extraction is an optional, lazily-loaded feature, so a failed download
 * warns and lets the build continue rather than killing it (air-gapped builds).
 *
 * Version must stay in sync with the `@ffmpeg/ffmpeg` major line in package.json.
 */
import { existsSync, mkdirSync, statSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

const FFMPEG_CORE_VERSION = '0.12.6';
const ffmpegDir = join(__dirname, '../static/ffmpeg');

// Only these two: the app loads the single-threaded core (`coreURL` + `wasmURL`), so
// `ffmpeg-core.worker.js` is never requested — and it doesn't exist in the ESM build
// anyway. The shell script this replaced used `curl` without `-f`, so it saved unpkg's
// 404 body as a 62-byte "ffmpeg-core.worker.js". The minBytes floor below is what
// catches that class of truncated / error-page download.
const CORE_FILES = [
  { name: 'ffmpeg-core.js', minBytes: 50_000 },
  { name: 'ffmpeg-core.wasm', minBytes: 10_000_000 },
];

const baseUrl = `https://unpkg.com/@ffmpeg/core@${FFMPEG_CORE_VERSION}/dist/esm`;

async function downloadFile(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return Buffer.from(await response.arrayBuffer());
}

async function main() {
  if (!existsSync(ffmpegDir)) {
    mkdirSync(ffmpegDir, { recursive: true });
  }

  const missing = CORE_FILES.filter(({ name, minBytes }) => {
    const target = join(ffmpegDir, name);
    return !existsSync(target) || statSync(target).size < minBytes;
  });

  if (missing.length === 0) {
    console.log(`FFmpeg.wasm core already present in ${ffmpegDir}`);
    return;
  }

  for (const { name } of missing) {
    console.log(`Downloading ${name}...`);
    try {
      writeFileSync(join(ffmpegDir, name), await downloadFile(`${baseUrl}/${name}`));
      console.log(`Downloaded ${name}`);
    } catch (error) {
      console.warn(
        `Warning: could not download ${name} (${error.message}). ` +
          'Client-side audio extraction will be unavailable in this build.'
      );
    }
  }
}

main();
