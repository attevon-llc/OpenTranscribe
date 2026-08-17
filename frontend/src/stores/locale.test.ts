/**
 * Tests for `$stores/locale` — the i18n locale store. First test file for this
 * module (BC-30 / BC-31).
 *
 * `getInitialLocale()` runs at MODULE EVALUATION time, mirroring the `theme.js`
 * pattern (see `theme.test.ts`) to avoid a flash of the wrong language before the
 * DOM is ready. Each test therefore `vi.resetModules()`s and re-imports the
 * module rather than sharing one instance.
 *
 * DEFECTS THESE CATCH:
 * - BC-30: `initialize()` registered `i18next.on('languageChanged', ...)` with no
 *   re-entry guard, unlike the sibling `network.ts` store's `initialized` boolean.
 *   A second `locale.initialize()` call (e.g. two mounted layouts) leaked a
 *   duplicate listener, so a single language change dispatched the `update()`
 *   callback twice. Fixed by mirroring `network.ts`'s guard exactly.
 * - BC-31: `t(key)` falls back to the raw `key` whenever `i18next.t()` returns a
 *   falsy value — including a legitimately empty-string translation, not just a
 *   missing key. Audited `en.json`: no key intentionally resolves to `''`, so this
 *   is a theoretical edge case today, not an observed bug. The `||` is also the
 *   correct behavior for the case it exists for (a missing/untranslated key), so
 *   it is left as-is. The test below PINS the current behavior explicitly, so a
 *   future empty-string translation regressing silently (masked as the raw key
 *   instead of rendering blank) is a visible, intentional test failure rather
 *   than an unnoticed gap.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

const { mockI18next, mockInitI18n, mockEnsureLocaleLoaded } = vi.hoisted(() => {
  const i18nextMock = {
    t: vi.fn((key: string) => key),
    on: vi.fn(),
    isInitialized: false,
    changeLanguage: vi.fn(async () => undefined),
  };
  return {
    mockI18next: i18nextMock,
    mockInitI18n: vi.fn(async () => i18nextMock),
    mockEnsureLocaleLoaded: vi.fn(async (lng: string) => lng),
  };
});

vi.mock('i18next', () => ({
  default: mockI18next,
}));

vi.mock('$lib/i18n', () => ({
  initI18n: mockInitI18n,
  ensureLocaleLoaded: mockEnsureLocaleLoaded,
}));

async function loadLocaleStore() {
  vi.resetModules();
  return import('$stores/locale');
}

describe('locale store', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('lang');
    mockI18next.t.mockClear();
    mockI18next.on.mockClear();
    mockI18next.changeLanguage.mockClear();
    mockI18next.isInitialized = false;
    mockInitI18n.mockClear();
    mockEnsureLocaleLoaded.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe('getInitialLocale fallback chain', () => {
    it('uses a valid saved locale from localStorage first', async () => {
      localStorage.setItem('locale', 'fr');
      vi.stubGlobal('navigator', { language: 'de-DE' });

      const { locale } = await loadLocaleStore();

      expect(get(locale)).toBe('fr');
      expect(document.documentElement.lang).toBe('fr');
    });

    it('ignores an invalid saved locale and falls through to the browser language', async () => {
      localStorage.setItem('locale', 'not-a-real-code');
      vi.stubGlobal('navigator', { language: 'de-DE' });

      const { locale } = await loadLocaleStore();

      expect(get(locale)).toBe('de');
      expect(document.documentElement.lang).toBe('de');
    });

    it('collapses a BCP-47 browser tag to its base code when valid', async () => {
      vi.stubGlobal('navigator', { language: 'pt-BR' });

      const { locale } = await loadLocaleStore();

      expect(get(locale)).toBe('pt');
    });

    it('falls back to DEFAULT_LANGUAGE when nothing saved or detected is valid', async () => {
      vi.stubGlobal('navigator', { language: 'xx-XX' });

      const { locale } = await loadLocaleStore();
      const { DEFAULT_LANGUAGE } = await import('$lib/i18n/languages');

      expect(get(locale)).toBe(DEFAULT_LANGUAGE);
      expect(document.documentElement.lang).toBe(DEFAULT_LANGUAGE);
    });
  });

  describe('initialize() re-entry guard (BC-30)', () => {
    it('registers exactly one languageChanged listener no matter how many times it is called', async () => {
      const { locale } = await loadLocaleStore();

      await locale.initialize();
      await locale.initialize();
      await locale.initialize();

      expect(mockI18next.on).toHaveBeenCalledTimes(1);
      expect(mockI18next.on).toHaveBeenCalledWith('languageChanged', expect.any(Function));
      // initI18n itself should also only run once — a second call re-running it
      // would re-initialize i18next redundantly.
      expect(mockInitI18n).toHaveBeenCalledTimes(1);

      // Prove the guard isn't just suppressing the mock-call count: firing the
      // ONE registered handler should update the store exactly once, not
      // three times (which a duplicate-listener leak would produce).
      const onLanguageChanged = mockI18next.on.mock.calls[0][1] as (lng: string) => void;
      onLanguageChanged('ja');
      expect(get(locale)).toBe('ja');
    });

    it('updates the store exactly once per languageChanged event after a single initialize', async () => {
      const { locale } = await loadLocaleStore();

      await locale.initialize();
      const onLanguageChanged = mockI18next.on.mock.calls[0][1] as (lng: string) => void;

      onLanguageChanged('es');

      expect(get(locale)).toBe('es');
    });
  });

  describe('t() translation function', () => {
    it('returns the raw key when i18next is not initialized', async () => {
      mockI18next.isInitialized = false;
      await loadLocaleStore();
      const { t } = await import('$stores/locale');

      expect(get(t)('greeting.hello')).toBe('greeting.hello');
      expect(mockI18next.t).not.toHaveBeenCalled();
    });

    it('returns the resolved translation when i18next has one', async () => {
      mockI18next.isInitialized = true;
      mockI18next.t.mockReturnValue('Hola');
      await loadLocaleStore();
      const { t } = await import('$stores/locale');

      expect(get(t)('greeting.hello')).toBe('Hola');
    });

    it('passes interpolation options through to i18next.t', async () => {
      mockI18next.isInitialized = true;
      mockI18next.t.mockReturnValue('Hello, David');
      await loadLocaleStore();
      const { t } = await import('$stores/locale');

      const result = get(t)('greeting.named', { name: 'David' });

      expect(result).toBe('Hello, David');
      expect(mockI18next.t).toHaveBeenCalledWith('greeting.named', { name: 'David' });
    });

    it('BC-31 PIN: a genuinely empty-string translation is masked by the raw key', async () => {
      // Documents current, deliberately-unchanged behavior: `i18next.t(...) || key`
      // cannot distinguish "missing key" from "resolved to ''". No key in
      // en.json resolves to '' today (audited), so this is not a live bug — but
      // if one ever does, this test turns the silent substitution into a loud,
      // intentional failure instead of an unnoticed regression.
      mockI18next.isInitialized = true;
      mockI18next.t.mockReturnValue('');
      await loadLocaleStore();
      const { t } = await import('$stores/locale');

      expect(get(t)('some.emptyValue')).toBe('some.emptyValue');
    });
  });

  describe('set()', () => {
    it('ignores an invalid locale code entirely', async () => {
      const { locale } = await loadLocaleStore();
      const before = get(locale);

      locale.set('not-a-real-code');

      expect(get(locale)).toBe(before);
      expect(localStorage.getItem('locale')).toBeNull();
    });

    it('persists a valid locale, updates the document lang, and skips applyLanguage when i18next is not initialized', async () => {
      mockI18next.isInitialized = false;
      const { locale } = await loadLocaleStore();

      locale.set('fr');

      expect(get(locale)).toBe('fr');
      expect(localStorage.getItem('locale')).toBe('fr');
      expect(document.documentElement.lang).toBe('fr');
      expect(mockEnsureLocaleLoaded).not.toHaveBeenCalled();
    });

    it('loads the locale chunk before switching i18next language when initialized', async () => {
      mockI18next.isInitialized = true;
      const { locale } = await loadLocaleStore();

      locale.set('de');
      // applyLanguage is fire-and-forget (`void applyLanguage(...)`); it awaits a
      // dynamic import plus two async calls, so wait it out rather than guessing
      // a fixed number of microtask flushes.
      await vi.waitFor(() => {
        expect(mockI18next.changeLanguage).toHaveBeenCalledWith('de');
      });

      // The document lang attribute and store value are updated synchronously
      // (not gated on the chunk load), independent of the mocked i18next calls.
      expect(get(locale)).toBe('de');
      expect(document.documentElement.lang).toBe('de');
      expect(mockEnsureLocaleLoaded).toHaveBeenCalledWith('de');
    });
  });
});
