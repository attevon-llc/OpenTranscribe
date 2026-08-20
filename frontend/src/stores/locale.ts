import { writable, derived, get } from 'svelte/store';
import i18next from 'i18next';
import {
  DEFAULT_LANGUAGE,
  getLanguageDirection,
  isValidLanguageCode,
  SUPPORTED_LANGUAGES,
} from '$lib/i18n/languages';

// Get initial locale (mirrors theme.js pattern)
const getInitialLocale = (): string => {
  if (typeof window !== 'undefined') {
    const savedLocale = localStorage.getItem('locale');
    if (savedLocale && isValidLanguageCode(savedLocale)) {
      return savedLocale;
    }

    // Check browser preference
    const browserLang = navigator.language?.split('-')[0];
    if (browserLang && isValidLanguageCode(browserLang)) {
      return browserLang;
    }
  }

  return DEFAULT_LANGUAGE;
};

// Immediately apply locale before DOM is fully loaded (prevent flash)
if (typeof window !== 'undefined') {
  const initialLocale = getInitialLocale();
  document.documentElement.lang = initialLocale;
  // #453/ML4: `dir` must be set before first paint the same way `lang` is above — an
  // Arabic session that only gets `dir="rtl"` after i18next finishes initializing would
  // render an LTR flash of the whole shell (nav, sidebar, chat panes) before flipping.
  document.documentElement.dir = getLanguageDirection(initialLocale);
}

// Create the locale store
/**
 * Switch i18next to `newLocale`, fetching that locale's chunk first.
 *
 * Locale strings are code-split (one chunk per language), so `changeLanguage`
 * must not run before the chunk lands or every `$t(...)` renders its raw
 * dot-notation key. The UI keeps showing the previous language for the duration
 * of the fetch instead of flashing keys.
 */
const applyLanguage = async (newLocale: string): Promise<void> => {
  const { ensureLocaleLoaded } = await import('$lib/i18n');
  await ensureLocaleLoaded(newLocale);
  await i18next.changeLanguage(newLocale);
};

const createLocaleStore = () => {
  const { subscribe, set, update } = writable<string>(getInitialLocale());
  let initialized = false;

  return {
    subscribe,

    set: (newLocale: string) => {
      if (isValidLanguageCode(newLocale)) {
        set(newLocale);

        // Persist to localStorage
        if (typeof window !== 'undefined') {
          localStorage.setItem('locale', newLocale);
        }

        // Update i18next language (loads the locale chunk first)
        if (i18next.isInitialized) {
          void applyLanguage(newLocale);
        }

        // Update document lang + dir attributes for accessibility and RTL layout
        // (#453/ML4). Both are set together — a locale switch that updated `lang`
        // without `dir` would leave an Arabic UI rendering left-to-right.
        if (typeof document !== 'undefined') {
          document.documentElement.lang = newLocale;
          document.documentElement.dir = getLanguageDirection(newLocale);
        }
      }
    },

    // Initialize store with i18next
    initialize: async () => {
      if (initialized) {
        return;
      }

      const currentLocale = get({ subscribe });

      // Import and initialize i18n
      const { initI18n } = await import('$lib/i18n');
      await initI18n(currentLocale);

      // Set up listener for i18next language changes
      i18next.on('languageChanged', (lng) => {
        update(() => lng);
      });

      initialized = true;
    },
  };
};

export const locale = createLocaleStore();

// Derived store for translation function
// Usage: $t('key') or $t('key', { name: 'value' })
export const t = derived(locale, () => {
  return (key: string, options?: Record<string, unknown>): string => {
    if (!i18next.isInitialized) {
      return key;
    }
    return i18next.t(key, options as Record<string, string>) || key;
  };
});

// Export supported languages for UI
export { SUPPORTED_LANGUAGES };
