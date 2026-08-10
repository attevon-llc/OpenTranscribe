/**
 * Locale strings are code-split (one chunk per language) — see index.ts. These
 * tests lock the two properties that make that safe: exactly one bundle is
 * registered up front, and any other language can be pulled in on demand.
 *
 * Tests share the i18next singleton, so they run in declaration order.
 */
import { describe, it, expect, beforeAll, vi } from 'vitest';
import i18next from 'i18next';
import { initI18n, ensureLocaleLoaded } from './index';

describe('i18n lazy locale loading', () => {
  beforeAll(async () => {
    await initI18n('es');
  });

  it('registers only the active locale', () => {
    expect(i18next.resolvedLanguage).toBe('es');
    expect(i18next.hasResourceBundle('es', 'translation')).toBe(true);
    expect(i18next.hasResourceBundle('fr', 'translation')).toBe(false);
    expect(i18next.hasResourceBundle('ru', 'translation')).toBe(false);
  });

  it('resolves real strings from the lazily loaded bundle', () => {
    // A raw dot-notation key coming back means the bundle never landed.
    expect(i18next.t('common.save')).not.toBe('common.save');
  });

  it('loads another locale on demand and collapses BCP-47 tags', async () => {
    expect(await ensureLocaleLoaded('fr-CA')).toBe('fr');
    expect(i18next.hasResourceBundle('fr', 'translation')).toBe(true);
  });

  it('falls back to the default language for unsupported codes', async () => {
    expect(await ensureLocaleLoaded('xx')).toBe('en');
    expect(i18next.hasResourceBundle('en', 'translation')).toBe(true);
  });

  it('is idempotent for an already-loaded locale', async () => {
    expect(await ensureLocaleLoaded('fr')).toBe('fr');
    expect(await ensureLocaleLoaded('fr')).toBe('fr');
  });

  it('locale.set() fetches the chunk before switching i18next', async () => {
    const { locale } = await import('$stores/locale');
    expect(i18next.hasResourceBundle('de', 'translation')).toBe(false);

    locale.set('de');

    await vi.waitFor(() => expect(i18next.language).toBe('de'));
    // If the switch had raced ahead of the fetch, the UI would render raw keys.
    expect(i18next.hasResourceBundle('de', 'translation')).toBe(true);
    expect(i18next.t('common.save')).not.toBe('common.save');
  });
});
