import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect } from 'vitest';
import i18next from 'i18next';

const dir = join(__dirname, 'locales');
const locales = readdirSync(dir)
  .filter((f) => f.endsWith('.json'))
  .map((f) => f.replace('.json', ''));
const load = (l: string): Record<string, string> =>
  JSON.parse(readFileSync(join(dir, `${l}.json`), 'utf8'));

// "file(s)", "Datei(en)", "video('s)": a count-neutral hedge instead of a real plural form.
const HEDGE = /\p{L}\((s|es|en|n|'s|ات|هات|ين)\)/u;
// "(s)" after a number-less unit such as seconds, and URL schemes, are not plural hedges.
const NOT_A_PLURAL = /http\(s\)|\(s\)$/;

describe('count copy uses real plural forms, never "(s)" hedges', () => {
  for (const l of locales) {
    it(`${l}: no value hedges a plural with (s)/(en)/(es)`, () => {
      const offenders = Object.entries(load(l)).filter(
        ([, v]) => HEDGE.test(v) && !NOT_A_PLURAL.test(v)
      );
      expect(offenders.map(([k]) => k)).toEqual([]);
    });
  }

  async function t(lng: string) {
    const inst = i18next.createInstance();
    await inst.init({
      lng,
      fallbackLng: 'en',
      resources: Object.fromEntries(locales.map((l) => [l, { translation: load(l) }])),
      interpolation: { escapeValue: false },
    });
    return inst.t.bind(inst);
  }

  it('en: the legal-hold toast reads naturally for 1 and for many files', async () => {
    const tr = await t('en');
    expect(tr('holds.placed', { count: 1 })).toBe('Legal hold placed on 1 file.');
    expect(tr('holds.placed', { count: 3 })).toBe('Legal hold placed on 3 files.');
  });

  it('ar: the same toast has distinct one/two/few forms', async () => {
    const tr = await t('ar');
    const forms = [1, 2, 3, 11].map((count) => tr('holds.placed', { count }));
    expect(new Set(forms).size).toBe(4);
    expect(forms.join()).not.toContain('(');
  });

  it('en: a verb agrees with its count (was / were)', async () => {
    const tr = await t('en');
    expect(tr('gallery.deleteHeld', { count: 1 })).toContain('was not deleted');
    expect(tr('gallery.deleteHeld', { count: 2 })).toContain('were not deleted');
  });

  it('a second count in one sentence is a separately pluralised phrase', async () => {
    const tr = await t('en');
    expect(tr('settings.search.userCount', { count: 1 })).toBe('1 user');
    expect(tr('settings.search.userCount', { count: 2 })).toBe('2 users');
  });
});
