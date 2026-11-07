/**
 * Issue #794 — first-load checks computed from Vite's build manifest. Fixture dists only,
 * so nothing here needs a real build. The locale case is the must-fire control: a layout that
 * statically imports a locale JSON must fail the gate, and the clean fixture must pass.
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { manifestClosure } from '../scripts/bundle-manifest.mjs';
import { run } from '../scripts/check-bundle-budget.mjs';

const SENTENCE = 'Welcome back to the transcription workspace today';
const BIG = { node: 10_000, closure: 10_000 };
const EXTRA = { entry: 10_000, firstLoad: 10_000, totalJs: 10_000 };

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'ot-budget-'));
});
afterEach(() => rmSync(dir, { recursive: true, force: true }));

/** Build a fixture dist plus manifest; `layoutImportsLocale` mimics a static locale import. */
function fixture(layoutImportsLocale: boolean, inlined = false) {
  const imm = join(dir, 'dist', '_app', 'immutable');
  for (const d of ['entry', 'nodes', 'chunks']) mkdirSync(join(imm, d), { recursive: true });
  const write = (f: string, body: string) => writeFileSync(join(dir, 'dist', f), body);
  write('_app/immutable/entry/start.js', 'import"../chunks/rt.js";');
  write('_app/immutable/entry/app.js', 'export const a=1;');
  write('_app/immutable/chunks/rt.js', 'export const r=1;');
  write('_app/immutable/chunks/en.js', `export default {a:"${SENTENCE}"};`);
  write(
    '_app/immutable/nodes/0.abc.js',
    inlined ? `const t="${SENTENCE}";export{t};` : 'import"../chunks/en.js";'
  );
  write('_app/immutable/nodes/1.js', 'export const e=1;');
  const lazy = ['chunks/en.js'];
  const manifest: Record<string, any> = {
    '../k/runtime/client/entry.js': {
      file: '_app/immutable/entry/start.js',
      isEntry: true,
      imports: ['chunks/rt'],
    },
    '.svelte-kit/generated/client-optimized/app.js': {
      file: '_app/immutable/entry/app.js',
      isEntry: true,
    },
    '.svelte-kit/generated/client-optimized/nodes/0.js': {
      file: '_app/immutable/nodes/0.abc.js',
      isEntry: true,
      imports: layoutImportsLocale && !inlined ? ['src/lib/i18n/locales/en.json'] : [],
    },
    '.svelte-kit/generated/client-optimized/nodes/1.js': {
      file: '_app/immutable/nodes/1.js',
      isEntry: true,
    },
    'chunks/rt': { file: '_app/immutable/chunks/rt.js' },
    'src/lib/i18n/locales/en.json': {
      file: `_app/immutable/${lazy[0]}`,
      src: 'src/lib/i18n/locales/en.json',
      isDynamicEntry: true,
    },
  };
  mkdirSync(join(dir, 'locales'));
  writeFileSync(join(dir, 'locales', 'en.json'), JSON.stringify({ greeting: SENTENCE }));
  writeFileSync(join(dir, 'manifest.json'), JSON.stringify(manifest));
  return {
    root: imm,
    manifestPath: join(dir, 'manifest.json'),
    localeDir: join(dir, 'locales'),
    env: {},
    base: BIG,
    extra: EXTRA,
  };
}

describe('manifest first-load checks', () => {
  it('passes a layout that keeps locales lazy', () => {
    const r = run(fixture(false));
    expect(r.stderr).toEqual([]);
    expect(r.code).toBe(0);
    expect(r.stdout.join('\n')).toContain('no locale JSON in the first-load closure');
  });

  it('fails when a locale JSON is a static import of the layout', () => {
    const r = run(fixture(true));
    expect(r.code).toBe(1);
    expect(r.stderr.join('\n')).toContain('locale JSON in the first-load closure');
    expect(r.stderr.join('\n')).toContain('src/lib/i18n/locales/en.json');
  });

  it('fails when the locale is inlined into a first-load chunk with no manifest src', () => {
    const r = run(fixture(true, true));
    expect(r.code).toBe(1);
    expect(r.stderr.join('\n')).toContain('locale text inlined in _app/immutable/nodes/0.abc.js');
  });

  it('fails when the manifest is missing (fail closed)', () => {
    const f = fixture(false);
    const r = run({ ...f, manifestPath: join(dir, 'nope.json') });
    expect(r.code).toBe(1);
    expect(r.stderr.join('\n')).toContain('build manifest not found');
  });

  it.each([
    ['entry', { ...EXTRA, entry: 1 }, 'entry chunks'],
    ['first-load', { ...EXTRA, firstLoad: 1 }, 'first-load closure'],
    ['total', { ...EXTRA, totalJs: 1 }, 'total JS'],
  ])('fails when the %s budget is exceeded', (_n, extra, label) => {
    const r = run({ ...fixture(false), extra });
    expect(r.code).toBe(1);
    expect(r.stderr.join('\n')).toContain(label);
  });
});

describe('manifestClosure', () => {
  it('follows imports, not dynamicImports', () => {
    const m = {
      a: { file: 'a.js', imports: ['b'], dynamicImports: ['c'] },
      b: { file: 'b.js' },
      c: { file: 'c.js' },
    };
    expect(manifestClosure(m, ['a']).sort()).toEqual(['a', 'b']);
  });
});
