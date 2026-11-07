/**
 * Copy changed after the v0.7.0 owner review: key PARITY (`npm run check:i18n`) would pass with
 * English pasted into every file, so each non-English locale must differ from English for these
 * keys. Reads the JSON directly, so the assertion is about what ships.
 */
import { readdirSync, readFileSync } from 'fs';
import { resolve } from 'path';
import { describe, it, expect } from 'vitest';

const DIR = resolve(__dirname, 'locales');
const load = (locale: string): Record<string, string> =>
  JSON.parse(readFileSync(resolve(DIR, `${locale}.json`), 'utf8'));

const en = load('en');
const KEYS = ['settings.recording.title', 'settings.recording.description'];
const OTHERS = readdirSync(DIR)
  .filter((f) => f.endsWith('.json') && f !== 'en.json')
  .map((f) => f.replace('.json', ''));

describe('owner-review copy is translated, not just present', () => {
  it('found the keys and the eleven other locales (a vacuous loop proves nothing)', () => {
    expect(KEYS.every((k) => k in en)).toBe(true);
    expect(OTHERS).toHaveLength(11);
  });

  it('says recording settings are for the microphone in the web page', () => {
    expect(en['settings.recording.title'].toLowerCase()).toContain('microphone');
    expect(en['settings.recording.description'].toLowerCase()).toContain('web page');
  });

  for (const locale of OTHERS) {
    it(`${locale} differs from English for every key`, () => {
      const loc = load(locale);
      for (const key of KEYS) {
        expect(loc[key], key).toBeTruthy();
        expect(loc[key], key).not.toBe(en[key]);
      }
    });
  }
});
