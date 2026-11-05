#!/usr/bin/env node
/**
 * i18n key-parity checker.
 *
 * en.json is the source of truth. Every other locale must contain exactly the same
 * (flat, dot-notation) key set — no missing keys (untranslated UI) and no extra keys
 * (stale entries). Exits non-zero on any mismatch so it can gate pre-commit / CI.
 *
 * Plural keys are the one exception: i18next picks `<base>_<category>` using the CLDR
 * plural categories of the ACTIVE language, so a base that en.json pluralises
 * (`<base>_one` + `<base>_other`) must appear in each locale with exactly that locale's
 * own categories (ru: one few many other; ja: other only). A missing form silently
 * falls back to English at runtime; a stray form is dead text.
 *
 * Usage: node scripts/check-i18n-parity.mjs [--selftest]   (run from frontend/)
 */
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const localesDir = join(here, '..', 'src', 'lib', 'i18n', 'locales');

const PLURAL_RE = /^(.+)_(zero|one|two|few|many|other)$/;

/** Bases that the reference pluralises: both `_one` and `_other` present. */
export function pluralBases(refKeys) {
  const bases = new Set();
  for (const k of refKeys) {
    const m = PLURAL_RE.exec(k);
    if (m && m[2] === 'one' && refKeys.has(`${m[1]}_other`)) bases.add(m[1]);
  }
  return bases;
}

/**
 * CLDR cardinal plural categories for our 12 locales, pinned so the gate gives the same
 * answer on every Node/ICU build (CI runs Node 26, dev hosts differ). Taken from CLDR 47.0
 * (Node 22.22 / ICU 77.1). es/fr/it/pt carry `many` for millions ("1 millón de ...").
 */
export const PLURAL_CATEGORIES = {
  en: ['one', 'other'],
  ru: ['one', 'few', 'many', 'other'],
  ar: ['zero', 'one', 'two', 'few', 'many', 'other'],
  es: ['one', 'many', 'other'],
  fr: ['one', 'many', 'other'],
  it: ['one', 'many', 'other'],
  pt: ['one', 'many', 'other'],
  de: ['one', 'other'],
  nl: ['one', 'other'],
  ja: ['other'],
  ko: ['other'],
  zh: ['other'],
};

/** The key set `locale` must have, given the reference key set. */
export function expectedKeys(refKeys, locale) {
  const bases = pluralBases(refKeys);
  const categories = PLURAL_CATEGORIES[locale];
  if (!categories) throw new Error(`no pinned plural categories for locale "${locale}"`);
  const out = new Set();
  for (const k of refKeys) {
    const m = PLURAL_RE.exec(k);
    if (!(m && bases.has(m[1]))) out.add(k);
  }
  for (const b of bases) for (const c of categories) out.add(`${b}_${c}`);
  return out;
}

export function diffKeys(expected, actual) {
  return {
    missing: [...expected].filter((k) => !actual.has(k)),
    extra: [...actual].filter((k) => !expected.has(k)),
  };
}

function noteRuntimeDrift() {
  for (const [loc, pinned] of Object.entries(PLURAL_CATEGORIES)) {
    const live = new Intl.PluralRules(loc).resolvedOptions().pluralCategories;
    if ([...live].sort().join() !== [...pinned].sort().join())
      console.log(
        `  NOTE: ${loc} pinned [${pinned}] differs from this Node's Intl.PluralRules [${live}]`
      );
  }
}

function selfTest() {
  noteRuntimeDrift();
  const ref = new Set(['a', 'n_one', 'n_other']);
  const cases = [
    ['ru lacking _few', 'ru', ['a', 'n_one', 'n_many', 'n_other'], ['n_few'], []],
    ['ru complete', 'ru', ['a', 'n_one', 'n_few', 'n_many', 'n_other'], [], []],
    ['en stray _few', 'en', ['a', 'n_one', 'n_few', 'n_other'], [], ['n_few']],
    ['ja stray _one', 'ja', ['a', 'n_one', 'n_other'], [], ['n_one']],
    [
      'ar lacking _zero/_two',
      'ar',
      ['a', 'n_one', 'n_few', 'n_many', 'n_other'],
      ['n_two', 'n_zero'],
      [],
    ],
    ['non-plural key missing', 'de', ['n_one', 'n_other'], ['a'], []],
  ];
  let bad = 0;
  for (const [name, loc, have, wantMissing, wantExtra] of cases) {
    const d = diffKeys(expectedKeys(ref, loc), new Set(have));
    const ok =
      JSON.stringify(d.missing.sort()) === JSON.stringify(wantMissing) &&
      JSON.stringify(d.extra.sort()) === JSON.stringify(wantExtra);
    if (!ok) {
      bad++;
      console.error(`  self-test FAILED: ${name}: got ${JSON.stringify(d)}`);
    }
  }
  if (bad) process.exit(1);
  return cases.length;
}

const keysOf = (file) =>
  new Set(Object.keys(JSON.parse(readFileSync(join(localesDir, file), 'utf8'))));

const selfTestCases = selfTest();
if (process.argv.includes('--selftest')) {
  console.log(`i18n plural self-test OK (${selfTestCases} cases)`);
  process.exit(0);
}

const reference = 'en.json';
const refKeys = keysOf(reference);
const locales = readdirSync(localesDir)
  .filter((f) => f.endsWith('.json') && f !== reference)
  .sort();

let failed = false;
console.log(
  `i18n parity — reference ${reference} has ${refKeys.size} keys across ${
    locales.length + 1
  } locales`
);

for (const file of locales) {
  const keys = keysOf(file);
  const { missing, extra } = diffKeys(expectedKeys(refKeys, file.replace(/\.json$/, '')), keys);
  if (missing.length === 0 && extra.length === 0) {
    console.log(`  ✓ ${file} (${keys.size})`);
    continue;
  }
  failed = true;
  console.error(`  ✗ ${file} (${keys.size}) — ${missing.length} missing, ${extra.length} extra`);
  if (missing.length)
    console.error(
      `      missing: ${missing.slice(0, 15).join(', ')}${missing.length > 15 ? ' …' : ''}`
    );
  if (extra.length)
    console.error(
      `      extra:   ${extra.slice(0, 15).join(', ')}${extra.length > 15 ? ' …' : ''}`
    );
}

if (failed) {
  console.error(
    '\ni18n parity FAILED — every locale must share en.json’s key set (plurals per CLDR category).'
  );
  process.exit(1);
}
console.log('\ni18n parity OK — all locales match en.json.');
