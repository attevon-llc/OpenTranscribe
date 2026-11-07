import { execFileSync } from 'node:child_process';
import { describe, it, expect } from 'vitest';
import i18next from 'i18next';
import en from './locales/en.json';
import ru from './locales/ru.json';
import ar from './locales/ar.json';
import ja from './locales/ja.json';

async function translator(lng: string, resources: Record<string, string>) {
  const inst = i18next.createInstance();
  await inst.init({
    lng,
    fallbackLng: 'en',
    resources: { en: { translation: en }, [lng]: { translation: resources } },
    interpolation: { escapeValue: false },
  });
  return inst.t.bind(inst);
}

const KEY = 'common.retryAfterSeconds';

describe('plural forms render in the active language, not the English fallback', () => {
  it('ru: each count picks its own CLDR form (fixture resources, no wording pinned)', async () => {
    const fixture = {
      [`${KEY}_one`]: 'form-one {{count}}',
      [`${KEY}_few`]: 'form-few {{count}}',
      [`${KEY}_many`]: 'form-many {{count}}',
      [`${KEY}_other`]: 'form-other {{count}}',
    };
    const t = await translator('ru', fixture);
    const picked: Array<[number, string]> = [
      [0, 'many'],
      [1, 'one'],
      [2, 'few'],
      [4, 'few'],
      [5, 'many'],
      [11, 'many'],
      [12, 'many'],
      [14, 'many'],
      [21, 'one'],
      [22, 'few'],
      [25, 'many'],
      [101, 'one'],
      [1.5, 'other'],
    ];
    for (const [count, form] of picked) {
      expect(t(KEY, { count })).toBe(`form-${form} ${count}`);
    }
  });

  it('ru: the shipped locale carries four distinct, non-English forms with the count', async () => {
    const t = await translator('ru', ru);
    const forms = [1, 2, 5, 1.5].map((count) => t(KEY, { count }));
    for (const [i, count] of [1, 2, 5, 1.5].entries()) {
      expect(forms[i]).toContain(String(count));
      expect(forms[i]).not.toBe(t(KEY, { count, lng: 'en' }));
    }
    // Compare the sentences with the number masked out: the numbers alone would make them differ.
    const masked = forms.map((form, i) => form.replace(String([1, 2, 5, 1.5][i]), '#'));
    expect(new Set(masked.slice(0, 3)).size).toBe(3);
  });

  it('ar: dual and few forms are Arabic', async () => {
    const t = await translator('ar', ar);
    expect(t(KEY, { count: 2 })).toBe('أعد المحاولة بعد ثانيتين');
    expect(t(KEY, { count: 5 })).toBe('أعد المحاولة بعد 5 ثوانٍ');
  });

  it('ja: a language with only "other" resolves every count', async () => {
    const t = await translator('ja', ja);
    expect(t(KEY, { count: 1 })).toBe('1秒後にもう一度お試しください');
  });
});

describe('parity script plural rules', () => {
  it('self-test passes (fires on a missing ru _few and a stray en _few)', () => {
    const out = execFileSync('node', ['scripts/check-i18n-parity.mjs', '--selftest'], {
      encoding: 'utf8',
    });
    expect(out).toContain('self-test OK');
  });
});

describe('speakers review count', () => {
  const REVIEW = 'speakers.clusters.emptyReviewDesc';

  it('en: one waiting speaker reads in the singular ("1 speakers have" before)', async () => {
    const t = await translator('en', en);
    expect(t(REVIEW, { count: 1 })).toContain('1 speaker has no match and is waiting');
    expect(t(REVIEW, { count: 3 })).toContain('3 speakers have no match and are waiting');
  });
});
