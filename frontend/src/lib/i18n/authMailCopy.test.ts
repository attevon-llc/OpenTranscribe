/**
 * `npm run check:i18n` enforces key PARITY, not translation: English pasted into eleven
 * files passes it. The authentication-email copy (issue #1240) is the text an admin reads
 * when something is broken, so each non-English locale must differ from English for every
 * key this change added.
 *
 * Reads the JSON directly (not through the i18n runtime) so the assertion is about what
 * ships in the files.
 */
import { readdirSync, readFileSync } from 'fs';
import { resolve } from 'path';
import { describe, it, expect } from 'vitest';

const DIR = resolve(__dirname, 'locales');
const load = (locale: string): Record<string, string> =>
  JSON.parse(readFileSync(resolve(DIR, `${locale}.json`), 'utf8'));

const en = load('en');
const KEYS = Object.keys(en).filter(
  (k) =>
    k.startsWith('authMailNotice.') ||
    k === 'userManagement.inviteMailUnavailable' ||
    k.startsWith('firstRunWizard.summaryMail')
);
const OTHERS = readdirSync(DIR)
  .filter((f) => f.endsWith('.json') && f !== 'en.json')
  .map((f) => f.replace('.json', ''));

describe('authentication email copy is translated, not just present', () => {
  it('found the keys and the eleven other locales (a vacuous loop proves nothing)', () => {
    expect(KEYS.length).toBe(13);
    expect(OTHERS).toHaveLength(11);
  });

  it.each(OTHERS)('%s differs from English for every key', (locale) => {
    const messages = load(locale);
    const untranslated = KEYS.filter((k) => messages[k] === undefined || messages[k] === en[k]);
    expect(untranslated).toEqual([]);
  });

  it.each(OTHERS)('%s keeps the {{name}} placeholder where English has it', (locale) => {
    const messages = load(locale);
    const withName = KEYS.filter((k) => en[k].includes('{{name}}'));
    expect(withName.length).toBeGreaterThan(0);
    expect(withName.filter((k) => !messages[k]?.includes('{{name}}'))).toEqual([]);
  });

  it('no longer carries the retired "either" sentence in any locale', () => {
    for (const locale of ['en', ...OTHERS]) {
      expect(load(locale)['settings.authentication.authMail.noTransport']).toBeUndefined();
    }
  });
});
