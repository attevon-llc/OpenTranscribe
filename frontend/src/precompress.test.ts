/**
 * Issue #1131 — `scripts/precompress.mjs` writes the `.gz` siblings nginx serves with
 * `gzip_static`. A wrong `.gz` (stale, or for a file nginx would serve plain) is served
 * with the original's Content-Type, so the contract is pinned here.
 */
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gunzipSync } from 'node:zlib';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { precompress } from '../scripts/precompress.mjs';

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'precompress-'));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe('precompress', () => {
  it('writes a gzip sibling that round-trips to the original bytes', () => {
    const file = join(dir, 'app.js');
    const body = 'export const x = "hello world";\n'.repeat(200);
    writeFileSync(file, body);
    expect(precompress(file)).toBeGreaterThan(0);
    expect(gunzipSync(readFileSync(`${file}.gz`)).toString()).toBe(body);
  });

  it('skips tiny files, non-text assets and already-compressed formats', () => {
    const tiny = join(dir, 'tiny.js');
    writeFileSync(tiny, 'x');
    const png = join(dir, 'logo.png');
    writeFileSync(png, 'p'.repeat(5000));
    expect(precompress(tiny)).toBeNull();
    expect(precompress(png)).toBeNull();
    expect(existsSync(`${tiny}.gz`)).toBe(false);
    expect(existsSync(`${png}.gz`)).toBe(false);
  });
});
