#!/usr/bin/env node
/**
 * i18n unused-key checker.
 *
 * `check:i18n` proves every locale has the same keys; it does not prove anyone uses them.
 * This fails when a key in en.json is referenced nowhere in frontend/src (test files do not
 * count: a key only a test mentions is dead in the product).
 *
 * A key counts as used when any of these holds:
 *   1. its full dotted key (or, for plural forms, the base before `_one`/`_other`/...) appears
 *      in a source file;
 *   2. it matches a template literal such as `chat.trace.stage.${stage}` or the
 *      `'prefix.' + x` form, treating the static parts as a pattern;
 *   3. it starts with a prefix listed in scripts/i18n-dynamic-prefixes.json (prefix -> reason).
 *
 * The settings search indexes whole namespaces of en.json, so an unreferenced key there would be
 * a search hit for text no page renders; this check is what keeps those namespaces honest.
 *
 * Usage: node scripts/check-i18n-unused.mjs [--selftest]   (run from frontend/)
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = join(here, '..', 'src');
const PLURAL_SUFFIX = /_(zero|one|two|few|many|other)$/;
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function sourceFiles(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (name !== 'locales') sourceFiles(path, out);
    } else if (/\.(svelte|ts|js|html)$/.test(name) && !/\.(test|spec)\./.test(name)) {
      out.push(path);
    }
  }
  return out;
}

/** Regexes for keys built at runtime. Needs a literal dot and 5+ static chars to be specific. */
export function dynamicPatterns(text) {
  const patterns = [];
  for (const m of text.matchAll(/`([^`\n]*\$\{[^`\n]*)`/g)) {
    const parts = m[1].split(/\$\{[^}]*\}/);
    const staticPart = parts.join('');
    if (!staticPart.includes('.') || staticPart.length < 5) continue;
    patterns.push(new RegExp(`^${parts.map(escapeRe).join('\\S+?')}$`));
  }
  for (const m of text.matchAll(/['"]([A-Za-z0-9_.-]+\.)['"]\s*\+/g))
    patterns.push(new RegExp(`^${escapeRe(m[1])}.+$`));
  return patterns;
}

/** The keys of `en` that nothing references. */
export function findUnusedKeys(en, text, extraPrefixes) {
  const patterns = dynamicPatterns(text);
  return Object.keys(en).filter((key) => {
    const base = key.replace(PLURAL_SUFFIX, '');
    if (text.includes(key) || text.includes(base)) return false;
    if (extraPrefixes.some((p) => key.startsWith(p))) return false;
    return !patterns.some((re) => re.test(key) || re.test(base));
  });
}

function selfTest() {
  const en = { 'a.used': 'x', 'a.dyn.one': 'x', 'a.dead': 'x', 'ns.thing': 'x', 'p.n_one': 'x' };
  const text = "t('a.used'); t(`a.dyn.${v}`); t('p.n', {count})";
  const got = findUnusedKeys(en, text, ['ns.']);
  const ok = got.length === 1 && got[0] === 'a.dead';
  if (!ok) {
    console.error(`  self-test FAILED: expected only a.dead, got ${JSON.stringify(got)}`);
    process.exit(1);
  }
  return 1;
}

const cases = selfTest();
if (process.argv.includes('--selftest')) {
  console.log(`i18n unused-key self-test OK (${cases} case)`);
  process.exit(0);
}

const en = JSON.parse(readFileSync(join(srcDir, 'lib/i18n/locales/en.json'), 'utf8'));
const text = sourceFiles(srcDir)
  .map((f) => readFileSync(f, 'utf8'))
  .join('\n');
const allow = JSON.parse(readFileSync(join(here, 'i18n-dynamic-prefixes.json'), 'utf8'));
const prefixes = [...Object.keys(allow)];

const unused = findUnusedKeys(en, text, prefixes);
if (unused.length) {
  console.error(`i18n unused keys: ${unused.length} key(s) in en.json are referenced nowhere:`);
  for (const k of unused.slice(0, process.env.I18N_ALL ? unused.length : 40))
    console.error(`  ${k}`);
  if (unused.length > 40) console.error(`  … and ${unused.length - 40} more`);
  console.error(
    '\nDelete them from all 12 locales, or - if a runtime-built key reaches them - add the\n' +
      'prefix with a reason to scripts/i18n-dynamic-prefixes.json.'
  );
  process.exit(1);
}
console.log(`i18n unused-key check OK - all ${Object.keys(en).length} keys are referenced.`);
