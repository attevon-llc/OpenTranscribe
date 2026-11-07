/**
 * The bridge from the app locale to Video.js's translation packs (plan #1327 row 43):
 * only the active locale's pack is fetched, and it follows a `locale` store change.
 * https://videojs.org/docs/framework/html/guides/internationalization
 */
import { describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import { hasRegisteredLocale } from '@videojs/html/i18n';
import { SUPPORTED_LANGUAGES } from '$lib/i18n/languages';
import { PLAYER_LOCALE_PACKS, bindPlayerLocale, createPlayerLocaleLoader } from './i18n';

const fakeLoaders = () => {
  const calls: string[] = [];
  const loaders = Object.fromEntries(
    SUPPORTED_LANGUAGES.map(({ code }) => [
      code,
      code === 'en'
        ? null
        : vi.fn(async () => {
            calls.push(code);
          }),
    ])
  );
  return { loaders, calls };
};

describe('PLAYER_LOCALE_PACKS', () => {
  it('has exactly one entry per app locale, English built in', () => {
    const app = SUPPORTED_LANGUAGES.map((l) => l.code).sort();
    expect(app).toHaveLength(12);
    expect(Object.keys(PLAYER_LOCALE_PACKS).sort()).toEqual(app);
    expect(PLAYER_LOCALE_PACKS.en).toBeNull();
    for (const code of app.filter((c) => c !== 'en')) {
      expect(typeof PLAYER_LOCALE_PACKS[code], code).toBe('function');
    }
  });

  it('registers the real Video.js pack for a locale when loaded', async () => {
    expect(hasRegisteredLocale('ko')).toBe(false);
    await PLAYER_LOCALE_PACKS.ko?.();
    expect(hasRegisteredLocale('ko')).toBe(true);
  });
});

describe('createPlayerLocaleLoader', () => {
  it('loads a pack once, however often it is asked for', async () => {
    const { loaders, calls } = fakeLoaders();
    const ensure = createPlayerLocaleLoader(loaders);
    await Promise.all([ensure('fr'), ensure('fr')]);
    await ensure('fr');
    expect(calls).toEqual(['fr']);
  });

  it('loads nothing for English or an unknown code', async () => {
    const { loaders, calls } = fakeLoaders();
    const ensure = createPlayerLocaleLoader(loaders);
    await ensure('en');
    await ensure('xx');
    expect(calls).toEqual([]);
  });

  it('resolves when a pack fails to load, and retries it next time', async () => {
    let attempts = 0;
    const ensure = createPlayerLocaleLoader({
      de: async () => {
        attempts += 1;
        if (attempts === 1) throw new Error('chunk 404');
      },
    });
    await expect(ensure('de')).resolves.toBe(false);
    await expect(ensure('de')).resolves.toBe(true);
    expect(attempts).toBe(2);
  });
});

describe('bindPlayerLocale', () => {
  it('fetches only the active locale, then follows the store', async () => {
    const { loaders, calls } = fakeLoaders();
    const locale = writable('ja');
    const stop = bindPlayerLocale(locale, createPlayerLocaleLoader(loaders));
    await Promise.resolve();
    expect(calls).toEqual(['ja']);

    locale.set('ar');
    await Promise.resolve();
    expect(calls).toEqual(['ja', 'ar']);

    stop();
    locale.set('ru');
    await Promise.resolve();
    expect(calls).toEqual(['ja', 'ar']);
  });
});
