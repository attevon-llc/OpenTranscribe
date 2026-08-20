import { describe, it, expect } from 'vitest';
import {
  SUPPORTED_LANGUAGES,
  DEFAULT_LANGUAGE,
  isValidLanguageCode,
  getLanguageDirection,
} from './languages';

describe('languages', () => {
  it('includes nl, ko and ar (#453/ML4)', () => {
    const codes = SUPPORTED_LANGUAGES.map((l) => l.code);
    expect(codes).toContain('nl');
    expect(codes).toContain('ko');
    expect(codes).toContain('ar');
  });

  it('every entry has a non-empty name, nativeName and a valid direction', () => {
    for (const lang of SUPPORTED_LANGUAGES) {
      expect(lang.name.length).toBeGreaterThan(0);
      expect(lang.nativeName.length).toBeGreaterThan(0);
      expect(['ltr', 'rtl']).toContain(lang.direction);
    }
  });

  it('DEFAULT_LANGUAGE is a member of SUPPORTED_LANGUAGES', () => {
    expect(isValidLanguageCode(DEFAULT_LANGUAGE)).toBe(true);
  });

  describe('getLanguageDirection', () => {
    it('reports Arabic as rtl', () => {
      expect(getLanguageDirection('ar')).toBe('rtl');
    });

    it('reports every other configured language as ltr', () => {
      for (const lang of SUPPORTED_LANGUAGES) {
        if (lang.code === 'ar') continue;
        expect(getLanguageDirection(lang.code)).toBe('ltr');
      }
    });

    it('falls back to ltr for an unknown/invalid code rather than throwing', () => {
      expect(getLanguageDirection('not-a-real-code')).toBe('ltr');
      expect(getLanguageDirection('')).toBe('ltr');
    });
  });
});
