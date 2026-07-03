import i18next from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE } from './languages';

import en from './locales/en.json';
import es from './locales/es.json';
import fr from './locales/fr.json';
import de from './locales/de.json';
import pt from './locales/pt.json';
import zh from './locales/zh.json';
import ja from './locales/ja.json';
import ru from './locales/ru.json';

const resources: Record<string, { translation: Record<string, string> }> = {
  en: { translation: en },
  es: { translation: es },
  fr: { translation: fr },
  de: { translation: de },
  pt: { translation: pt },
  zh: { translation: zh },
  ja: { translation: ja },
  ru: { translation: ru },
};

// Managed-edition string packs: the commercial overlay drops per-locale flat
// JSON files into $lib/cloud/locales/ at image-build time; the community build
// has none, so this glob is empty and the merge is a no-op. Locale files are
// flat dot-notation key maps, so a shallow spread is a correct merge.
const editionPacks = import.meta.glob('../cloud/locales/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, Record<string, string>>;
for (const [path, pack] of Object.entries(editionPacks)) {
  const lng = path.replace(/^.*\/([a-z]{2})\.json$/, '$1');
  if (resources[lng]) {
    resources[lng].translation = { ...resources[lng].translation, ...pack };
  }
}

export async function initI18n(savedLanguage?: string): Promise<typeof i18next> {
  await i18next.use(LanguageDetector).init({
    resources,
    fallbackLng: DEFAULT_LANGUAGE,
    supportedLngs: SUPPORTED_LANGUAGES.map((l) => l.code),
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

/**
 * Get the localized speaker label prefix (e.g., "SPEAKER", "HABLANTE", "LOCUTEUR")
 * Useful for creating new speaker labels in the user's language.
 */
export function getSpeakerLabelPrefix(): string {
  return i18next.t('speaker.labelPrefix');
}

export default i18next;
