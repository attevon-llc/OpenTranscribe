export interface Language {
  code: string;
  name: string;
  nativeName: string;
  direction: 'ltr' | 'rtl';
}

// Kept in sync with the backend's `LLM_OUTPUT_LANGUAGES` (core/constants.py) — that list
// currently also has `it` (Italian), which is NOT added here: #453/ML4 scoped this addition
// to closing the nl/ko/ar gap specifically (see backend/app/services/CLAUDE.md's LLM features
// section for the full 12-language list). Adding `it` is a follow-on, not done here.
export const SUPPORTED_LANGUAGES: Language[] = [
  { code: 'en', name: 'English', nativeName: 'English', direction: 'ltr' },
  { code: 'es', name: 'Spanish', nativeName: 'Español', direction: 'ltr' },
  { code: 'fr', name: 'French', nativeName: 'Français', direction: 'ltr' },
  { code: 'de', name: 'German', nativeName: 'Deutsch', direction: 'ltr' },
  { code: 'pt', name: 'Portuguese', nativeName: 'Português', direction: 'ltr' },
  { code: 'nl', name: 'Dutch', nativeName: 'Nederlands', direction: 'ltr' },
  { code: 'zh', name: 'Chinese', nativeName: '中文', direction: 'ltr' },
  { code: 'ja', name: 'Japanese', nativeName: '日本語', direction: 'ltr' },
  { code: 'ko', name: 'Korean', nativeName: '한국어', direction: 'ltr' },
  { code: 'ru', name: 'Russian', nativeName: 'Русский', direction: 'ltr' },
  { code: 'ar', name: 'Arabic', nativeName: 'العربية', direction: 'rtl' },
];

export const DEFAULT_LANGUAGE = 'en';

export function isValidLanguageCode(code: string): boolean {
  return SUPPORTED_LANGUAGES.some((lang) => lang.code === code);
}

/**
 * Reading direction for a locale code (#453/ML4 — Arabic RTL support).
 *
 * Falls back to `'ltr'` for an unknown/invalid code rather than throwing: this is read
 * on every locale change (`$stores/locale.ts`) to set `<html dir>`, and a diagnostic-only
 * failure there must not leave the document direction unset.
 */
export function getLanguageDirection(code: string): 'ltr' | 'rtl' {
  return SUPPORTED_LANGUAGES.find((lang) => lang.code === code)?.direction ?? 'ltr';
}
