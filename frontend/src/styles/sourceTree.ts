/**
 * Source-tree scan helpers for the style-source tests (jsdom applies no cascade, so these tests
 * read `.svelte` / `.css` / `.ts` files directly).
 */
import fs from 'fs';
import path from 'path';

/** `frontend/src`. */
export const SRC = path.resolve(__dirname, '..');

/** Every file under `dir` whose name matches `include`, skipping directories named in `skipDirs`. */
export function walkSource(
  dir: string,
  include: RegExp,
  options: { exclude?: RegExp; skipDirs?: string[] } = {},
  out: string[] = []
): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (!options.skipDirs?.includes(e.name)) walkSource(p, include, options, out);
    } else if (include.test(e.name) && !options.exclude?.test(e.name)) out.push(p);
  }
  return out;
}

/** `p` relative to `frontend/src`, with forward slashes. */
export const rel = (p: string) => path.relative(SRC, p).split(path.sep).join('/');
