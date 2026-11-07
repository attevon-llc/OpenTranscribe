/**
 * Bridge from the app's UI language to Video.js 10's player translations.
 *
 * `<media-i18n>` follows the nearest ancestor `lang`, and `$stores/locale` already keeps
 * `<html lang>` and `dir` in step with the app locale, so labels switch with no remount.
 * The provider would lazy-load the pack itself, but only after first paint, which shows
 * English for a moment; the documented fix is to register the pack before it is needed
 * ("Translations flash in after load or after switching"). This preloads the active
 * locale's pack through its `/register` side-effect entry, one lazy chunk per locale, so a
 * viewer downloads only their own language:
 * https://videojs.org/docs/framework/html/guides/internationalization
 *
 * The map is keyed by the app's 12 locales (`$lib/i18n/languages`); `i18n.test.ts` fails
 * when the two lists drift. English is built into the library and has no pack to load.
 */
import type { Readable } from 'svelte/store';

type PackLoader = () => Promise<unknown>;

export const PLAYER_LOCALE_PACKS: Readonly<Record<string, PackLoader | null>> = {
  en: null,
  ar: () => import('@videojs/html/i18n/locales/ar/register'),
  de: () => import('@videojs/html/i18n/locales/de/register'),
  es: () => import('@videojs/html/i18n/locales/es/register'),
  fr: () => import('@videojs/html/i18n/locales/fr/register'),
  it: () => import('@videojs/html/i18n/locales/it/register'),
  ja: () => import('@videojs/html/i18n/locales/ja/register'),
  ko: () => import('@videojs/html/i18n/locales/ko/register'),
  nl: () => import('@videojs/html/i18n/locales/nl/register'),
  pt: () => import('@videojs/html/i18n/locales/pt/register'),
  ru: () => import('@videojs/html/i18n/locales/ru/register'),
  zh: () => import('@videojs/html/i18n/locales/zh/register'),
};

/**
 * A memoised loader over *packs*. Resolves `true` once the locale's translations are
 * registered (or built in), `false` for an unknown code or a failed fetch. A failure is not
 * cached, so the next switch retries; the player shows English labels meanwhile, never an
 * error.
 */
export function createPlayerLocaleLoader(
  packs: Readonly<Record<string, PackLoader | null>> = PLAYER_LOCALE_PACKS
): (code: string) => Promise<boolean> {
  const pending = new Map<string, Promise<boolean>>();
  return (code) => {
    if (!Object.hasOwn(packs, code)) return Promise.resolve(false);
    const load = packs[code];
    if (!load) return Promise.resolve(true);
    const existing = pending.get(code);
    if (existing) return existing;
    const attempt = load().then(
      () => true,
      () => {
        pending.delete(code);
        return false;
      }
    );
    pending.set(code, attempt);
    return attempt;
  };
}

/** The app-wide loader: every player on the page shares one memo. */
const ensurePlayerLocale = createPlayerLocaleLoader();

/** Preload the pack for *locale*'s current value and every later one. Returns the unsubscribe. */
export function bindPlayerLocale(
  locale: Readable<string>,
  ensure: (code: string) => Promise<boolean> = ensurePlayerLocale
): () => void {
  return locale.subscribe((code) => {
    void ensure(code);
  });
}
