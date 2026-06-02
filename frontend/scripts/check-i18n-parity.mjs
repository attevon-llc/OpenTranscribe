#!/usr/bin/env node
/**
 * i18n key-parity checker.
 *
 * en.json is the source of truth. Every other locale must contain exactly the same
 * (flat, dot-notation) key set — no missing keys (untranslated UI) and no extra keys
 * (stale entries). Exits non-zero on any mismatch so it can gate pre-commit / CI.
 *
 * Usage: node scripts/check-i18n-parity.mjs   (run from frontend/)
 */
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const localesDir = join(here, '..', 'src', 'lib', 'i18n', 'locales');

const keysOf = (file) =>
  new Set(Object.keys(JSON.parse(readFileSync(join(localesDir, file), 'utf8'))));

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
  const missing = [...refKeys].filter((k) => !keys.has(k));
  const extra = [...keys].filter((k) => !refKeys.has(k));
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
  console.error('\ni18n parity FAILED — every locale must share en.json’s key set.');
  process.exit(1);
}
console.log('\ni18n parity OK — all locales match en.json.');
