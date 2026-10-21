/**
 * Issue #1234: ~130 admin-settings strings shipped as English in seven locales because the
 * parity check only counts keys. `findUntranslated` is the detector; the first block proves it
 * fires and stays quiet, the second applies it to every shipped locale.
 */
import { describe, it, expect } from 'vitest';
import { findUntranslated, SAME_IN_EVERY_LANGUAGE } from './untranslatedCheck';
import en from './locales/en.json';
import ar from './locales/ar.json';
import de from './locales/de.json';
import es from './locales/es.json';
import fr from './locales/fr.json';
import itLocale from './locales/it.json';
import ja from './locales/ja.json';
import ko from './locales/ko.json';
import nl from './locales/nl.json';
import pt from './locales/pt.json';
import ru from './locales/ru.json';
import zh from './locales/zh.json';

const EN = en as Record<string, string>;
const LOCALES: Record<string, Record<string, string>> = {
  ar,
  de,
  es,
  fr,
  it: itLocale,
  ja,
  ko,
  nl,
  pt,
  ru,
  zh,
} as Record<string, Record<string, string>>;

describe('findUntranslated', () => {
  const english = {
    'a.prose': 'Save your changes before leaving this page.',
    'a.short': 'Save',
    'a.code': '/api/v1/files/{id}/download/path',
    'a.nonword': '1234567890 1234567890 1234567890',
  };

  it('fires on prose copied verbatim', () => {
    expect(findUntranslated(english, { ...english }, {})).toContain('a.prose');
  });

  it('stays quiet for a translated value, a short value, and non-prose', () => {
    const translated = {
      ...english,
      'a.prose': 'Speichern Sie Ihre Änderungen vor dem Verlassen.',
    };
    const found = findUntranslated(english, translated, {});
    expect(found).not.toContain('a.prose');
    expect(found).not.toContain('a.short');
    expect(found).not.toContain('a.nonword');
  });

  it('honours an allowlisted key', () => {
    expect(findUntranslated(english, { ...english }, { 'a.prose': 'reason' })).not.toContain(
      'a.prose'
    );
  });
});

describe('shipped locales carry no untranslated prose', () => {
  it.each(Object.keys(LOCALES))('%s', (code) => {
    expect(findUntranslated(EN, LOCALES[code])).toEqual([]);
  });

  it('every allowlist entry has a reason and names a real English key', () => {
    for (const [key, reason] of Object.entries(SAME_IN_EVERY_LANGUAGE)) {
      expect(reason.trim().length, `${key} needs a reason`).toBeGreaterThan(0);
      expect(EN[key], `${key} is not an English key (stale allowlist entry)`).toBeDefined();
    }
  });
});
