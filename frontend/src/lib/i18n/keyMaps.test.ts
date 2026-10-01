/**
 * #970 guard: i18n keys must be literals.
 *
 * `$t(`x.${value}`)` renders the raw dotted key whenever `value` has no string, and
 * nothing else in the repo notices (`check:i18n` checks locale parity, `svelte-check`
 * sees a string). Two checks close it: (1) no source file builds a `t(...)` key by
 * interpolation or concatenation, (2) every literal key in `keyMaps.ts` exists,
 * non-empty, in every locale.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import * as keyMaps from './keyMaps';
import { keyFor, labelFor } from './keyMaps';

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = join(here, '..', '..');
const localesDir = join(here, 'locales');

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return name === 'node_modules' ? [] : walk(full);
    return /\.(svelte|ts)$/.test(name) && !/\.test\.ts$/.test(name) ? [full] : [];
  });
}

// `t(` preceded by a non-word char or `$`; first argument is a template literal with
// `${` or a string literal followed by `+`.
const DYNAMIC_KEY = /(?<![\w])t\(\s*(`[^`]*\$\{|'[^']*'\s*\+|"[^"]*"\s*\+)/;

describe('no dynamically built i18n keys', () => {
  it('scans a realistic number of files', () => {
    expect(walk(srcDir).length).toBeGreaterThan(200);
  });

  it('the detector matches the constructs it exists to catch', () => {
    expect(DYNAMIC_KEY.test('{$t(`a.b.${x}`)}')).toBe(true);
    expect(DYNAMIC_KEY.test("i18next.t('a.' + x)")).toBe(true);
    expect(DYNAMIC_KEY.test("$t('a.b')")).toBe(false);
    expect(DYNAMIC_KEY.test('$t(`a.b`)')).toBe(false);
  });

  it('no source file calls t() with an interpolated or concatenated key', () => {
    const offenders: string[] = [];
    for (const file of walk(srcDir)) {
      readFileSync(file, 'utf8')
        .split('\n')
        .forEach((line, i) => {
          if (DYNAMIC_KEY.test(line)) offenders.push(`${relative(srcDir, file)}:${i + 1}`);
        });
    }
    expect(offenders).toEqual([]);
  });
});

const mapKeys = Object.values(keyMaps)
  .filter((v): v is Record<string, string> => typeof v === 'object' && v !== null)
  .flatMap((m) => Object.values(m));

describe('keyMaps.ts keys resolve in every locale', () => {
  const locales = readdirSync(localesDir).filter((f) => f.endsWith('.json'));

  it('finds the locales and a non-trivial number of mapped keys', () => {
    expect(locales).toHaveLength(12);
    expect(mapKeys.length).toBeGreaterThan(50);
  });

  it.each(locales)('%s has a non-empty string for every mapped key', (file) => {
    const strings = JSON.parse(readFileSync(join(localesDir, file), 'utf8')) as Record<
      string,
      string
    >;
    const missing = mapKeys.filter((k) => typeof strings[k] !== 'string' || !strings[k].trim());
    expect(missing).toEqual([]);
  });
});

describe('keyFor / labelFor', () => {
  const map = { a: 'x.a' };
  const t = (k: string) => `T(${k})`;

  it('maps known values and falls back for unknown, null and prototype keys', () => {
    expect(keyFor(map, 'a', 'fb')).toBe('x.a');
    expect(keyFor(map, 'zzz', 'fb')).toBe('fb');
    expect(keyFor(map, null, 'fb')).toBe('fb');
    expect(keyFor(map, 'toString', 'fb')).toBe('fb');
  });

  it('labelFor shows the raw value, never a key, when unmapped', () => {
    expect(labelFor(t, map, 'a')).toBe('T(x.a)');
    expect(labelFor(t, map, 'new_engine')).toBe('new_engine');
    expect(labelFor(t, map, undefined)).toBe('');
  });
});
