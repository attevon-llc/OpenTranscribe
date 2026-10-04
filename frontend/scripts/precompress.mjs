/**
 * Post-build: write a `.gz` sibling next to every compressible file in `dist/` (issue #1131).
 *
 * nginx serves them with `gzip_static on`, so the hashed bundles are compressed once at
 * level 9 here instead of on every request at level 6. The originals stay in place for
 * clients that do not send `Accept-Encoding: gzip`. Not brotli: the stock nginx image has
 * no brotli module, and a `.br` file nothing serves is dead weight.
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

export const COMPRESSIBLE = new Set([
  '.js',
  '.mjs',
  '.css',
  '.html',
  '.json',
  '.svg',
  '.wasm',
  '.txt',
  '.xml',
  '.webmanifest',
]);
export const MIN_BYTES = 1024;

/** @param {string} dir @returns {string[]} */
export function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? walk(join(dir, entry.name)) : [join(dir, entry.name)]
  );
}

/**
 * Write `<file>.gz` when it is worth it. Returns the compressed size, or null if skipped.
 * @param {string} file
 */
export function precompress(file) {
  if (!COMPRESSIBLE.has(extname(file)) || statSync(file).size < MIN_BYTES) return null;
  const source = readFileSync(file);
  const gz = gzipSync(source, { level: 9 });
  if (gz.length >= source.length) return null;
  writeFileSync(`${file}.gz`, gz);
  return gz.length;
}

function main() {
  const dist = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist');
  let count = 0;
  let before = 0;
  let after = 0;
  for (const file of walk(dist)) {
    if (file.endsWith('.gz')) continue;
    const size = precompress(file);
    if (size === null) continue;
    count += 1;
    before += statSync(file).size;
    after += size;
  }
  console.log(
    `precompress: ${count} files, ${(before / 1048576).toFixed(1)} MB -> ${(
      after / 1048576
    ).toFixed(1)} MB gzip`
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
