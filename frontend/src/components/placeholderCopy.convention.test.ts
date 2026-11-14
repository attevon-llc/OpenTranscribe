/**
 * A static `placeholder="..."` is not translated, so it may only hold a technical example (a
 * hostname, a DN, a header name, a cron line). Prose such as "e.g. ... or its docker subnet" or
 * "(blank = AWS)" shipped English in every locale because `check:i18n` only sees `$t` keys.
 * Write such text as `placeholder={$t('...')}`.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative, resolve } from 'path';

const SRC = resolve(__dirname, '..');

const STATIC_PLACEHOLDER = /placeholder="([^"{]*)"/g;
// Words that only appear in sentences, never in the technical examples placeholders hold.
const PROSE =
  /\b(e\.g\.?|i\.e\.?|blank|never|your|leave|empty|enter|type here|optional|or|and|the|for)\b/i;

function proseIn(source: string): string[] {
  return [...source.matchAll(STATIC_PLACEHOLDER)]
    .map((m) => m[1] ?? '')
    .filter((value) => PROSE.test(value));
}

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (full.endsWith('.svelte')) out.push(full);
  }
  return out;
}

describe('proseIn (the detector itself)', () => {
  it('fires on an English sentence in a static placeholder', () => {
    expect(proseIn('<input placeholder="e.g. 10.0.0.1 or its subnet" />')).toHaveLength(1);
    expect(proseIn('<input placeholder="https://s3.amazonaws.com (blank = AWS)" />')).toHaveLength(
      1
    );
  });

  it('stays quiet for technical examples and translated placeholders', () => {
    const src = `
      <input placeholder="CN=Admin User,OU=Admins,O=Example Corp,C=US" />
      <input placeholder="openid email profile" />
      <input placeholder="0 3 * * *" />
      <input placeholder={$t('settings.x.placeholder')} />`;
    expect(proseIn(src)).toEqual([]);
  });
});

describe('static placeholders hold no untranslated prose', () => {
  const files = walk(SRC);

  it('scans the component tree', () => {
    expect(files.length).toBeGreaterThan(100);
  });

  it('every prose placeholder goes through $t', () => {
    const offenders = files
      .map((f) => ({ file: relative(SRC, f), found: proseIn(readFileSync(f, 'utf8')) }))
      .filter((r) => r.found.length > 0);
    expect(offenders).toEqual([]);
  });
});
