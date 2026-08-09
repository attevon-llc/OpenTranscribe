import i18next from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, isValidLanguageCode } from './languages';

// Per-locale loaders — deliberately NON-eager. Statically importing all eight
// locale JSONs put ~2.3 MB of translations (every language, for every visitor)
// into a single entry chunk, of which one language is ever read. Without
// `eager`, `import.meta.glob` compiles to one `import()` per file, so Rollup
// emits one chunk per locale and the browser fetches exactly the one in use.
const localeLoaders = import.meta.glob('./locales/*.json', {
  import: 'default',
}) as Record<string, () => Promise<Record<string, string>>>;

// Managed-edition string packs: the commercial overlay drops per-locale flat
// JSON files into $lib/cloud/locales/ at image-build time; the community build
// has none, so this glob is empty and the merge is a no-op. Locale files are
// flat dot-notation key maps, so a shallow spread is a correct merge. These stay
// eager: the community glob resolves to zero files, and an edition pack is a
// small delta over the base locale, not a second full translation.
const editionPacks = import.meta.glob('../cloud/locales/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, Record<string, string>>;

const editionPackByLanguage: Record<string, Record<string, string>> = {};
for (const [path, pack] of Object.entries(editionPacks)) {
  editionPackByLanguage[path.replace(/^.*\/([a-z]{2})\.json$/, '$1')] = pack;
}

/** Collapse a BCP-47 tag ("en-US") to a supported base code, or the default. */
function normalizeLanguage(lng?: string | null): string {
  const base = (lng || '').split('-')[0].toLowerCase();
  return isValidLanguageCode(base) ? base : DEFAULT_LANGUAGE;
}

const loadedLanguages = new Set<string>();
const inFlightLoads = new Map<string, Promise<void>>();

async function loadLanguage(lng: string): Promise<void> {
  const loader = localeLoaders[`./locales/${lng}.json`];
  if (!loader) return;

  const translation = await loader();
  const pack = editionPackByLanguage[lng];
  i18next.addResourceBundle(
    lng,
    'translation',
    pack ? { ...translation, ...pack } : translation,
    true,
    true
  );
  loadedLanguages.add(lng);
}

/**
 * Fetch and register one locale's strings, once. Safe to call repeatedly and
 * concurrently — the first call owns the fetch, later callers await the same
 * promise. Callers MUST await this before switching i18next to `lng`, otherwise
 * the UI renders raw dot-notation keys until the chunk lands.
 *
 * Only the active language is ever loaded — not the `en` fallback. That is safe
 * because `npm run check:i18n` gates every locale on exact key parity with
 * en.json, so there is nothing for the fallback chain to resolve.
 *
 * @param lng - Language code (BCP-47 tags are collapsed to their base code).
 * @returns The normalized language code that was loaded.
 */
export async function ensureLocaleLoaded(lng: string): Promise<string> {
  const code = normalizeLanguage(lng);
  if (loadedLanguages.has(code)) return code;

  let pending = inFlightLoads.get(code);
  if (!pending) {
    pending = loadLanguage(code).finally(() => inFlightLoads.delete(code));
    inFlightLoads.set(code, pending);
  }
  await pending;
  return code;
}

export async function initI18n(savedLanguage?: string): Promise<typeof i18next> {
  await i18next.use(LanguageDetector).init({
    // Empty on purpose: the active locale is added by `ensureLocaleLoaded`
    // below, before this promise resolves. `partialBundledLanguages` tells
    // i18next that resources arrive incrementally so it doesn't treat the empty
    // map as "this language has no strings".
    resources: {},
    partialBundledLanguages: true,
    fallbackLng: DEFAULT_LANGUAGE,
    supportedLngs: SUPPORTED_LANGUAGES.map((l) => l.code),
    // Collapse "en-US" → "en" so the resolved language always matches a locale
    // file name; otherwise the lazy loader would find no chunk to fetch.
    load: 'languageOnly',
    detection: {
      order: ['localStorage', 'navigator'],
      lookupLocalStorage: 'locale',
      caches: ['localStorage'],
    },
    lng: savedLanguage,
    interpolation: {
      escapeValue: false,
    },
    debug: false,
  });

  // Awaited before init resolves — callers (the layout gates all rendering on
  // it) never paint against an empty resource store, so there is no FOUC.
  const code = await ensureLocaleLoaded(i18next.resolvedLanguage || savedLanguage || '');
  if (i18next.resolvedLanguage !== code) {
    await i18next.changeLanguage(code);
  }

  return i18next;
}

/**
 * Translate speaker labels from SPEAKER_XX format to localized format.
 * Used to display speaker names in the user's preferred UI language.
 *
 * @param name - The speaker name (e.g., "SPEAKER_01", "John", etc.)
 * @returns The localized speaker name (e.g., "Hablante 1" in Spanish, or the original name if not a generic speaker label)
 */
export function translateSpeakerLabel(name: string): string {
  if (!name) return name;

  // Check if the name matches the SPEAKER_XX pattern
  const speakerMatch = name.match(/^SPEAKER[_-]?(\d+)$/i);
  if (speakerMatch) {
    const number = speakerMatch[1];
    // Return the localized speaker label with the number
    return i18next.t('speaker.localizedLabel', { number });
  }

  // Return the original name if it's not a generic speaker label
  return name;
}
