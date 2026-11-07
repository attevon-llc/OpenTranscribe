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
const KEYS = [
  'settings.recording.title',
  'settings.recording.description',
  'takedown.admin.export',
  'takedown.admin.exportEmpty',
  'settings.quarantine.columnLegalHold',
  'settings.quarantine.legalHoldBadge',
  'settings.quarantine.tabLegalHold',
  'settings.quarantine.holdHelp',
  'settings.quarantine.holdHelpLink',
];
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

  it('keeps a takedown legal hold distinct from Settings > Legal holds in the quarantine copy', () => {
    expect(en['settings.quarantine.columnLegalHold'].toLowerCase()).toContain('takedown');
    expect(en['settings.quarantine.holdHelp']).toMatch(/separate/);
    expect(en['settings.quarantine.holdHelp']).toMatch(/without quarantining it/);
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
