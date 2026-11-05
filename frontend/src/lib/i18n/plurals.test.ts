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
  it('ru: 1, 2, 5, 21 and a fraction each pick their own CLDR form', async () => {
    const t = await translator('ru', ru);
    expect(t(KEY, { count: 1 })).toBe('Повторите попытку через 1 секунду');
    expect(t(KEY, { count: 2 })).toBe('Повторите попытку через 2 секунды');
    expect(t(KEY, { count: 5 })).toBe('Повторите попытку через 5 секунд');
    expect(t(KEY, { count: 21 })).toBe('Повторите попытку через 21 секунду');
    expect(t(KEY, { count: 1.5 })).toBe('Повторите попытку через 1.5 секунды');
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
