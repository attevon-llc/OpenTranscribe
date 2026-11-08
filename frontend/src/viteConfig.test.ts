/**
 * Pins the dev-server cold-start mitigation (A16). Without warmup the first request to a
 * freshly started Vite server can stall on module transforms and the e2e login page never
 * shows `#email`; late-discovered deps (the Video.js player) trigger a mid-run re-optimize.
 */
import { existsSync } from 'fs';
import path from 'path';
import { describe, it, expect } from 'vitest';
import configFn from '../vite.config';

const root = path.resolve(__dirname, '..');
const cfg = (await (configFn as (e: { mode: string; command: string }) => unknown)({
  mode: 'development',
  command: 'serve',
})) as {
  server: { warmup: { clientFiles: string[] } };
  optimizeDeps: { include: string[] };
};

describe('vite dev config', () => {
  it('warms the entry, login and file-detail route graphs', () => {
    const files = cfg.server.warmup.clientFiles;
    for (const needle of ['+layout.svelte', 'login/+page.svelte', 'files/*/+page.svelte']) {
      expect(files.some((f) => f.endsWith(needle))).toBe(true);
    }
  });

  it('warms only files that exist (globs resolve to a real directory)', () => {
    for (const f of cfg.server.warmup.clientFiles) {
      const target = f.includes('*') ? path.dirname(f.replace(/\*.*$/, 'x')) : f;
      expect(existsSync(path.join(root, target)), f).toBe(true);
    }
  });

  it('pre-bundles the Video.js player', () => {
    expect(cfg.optimizeDeps.include).toEqual(expect.arrayContaining(['@videojs/html']));
  });
});
