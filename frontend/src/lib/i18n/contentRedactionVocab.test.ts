/**
 * GH #970. Companion to `contentRedactionVocab.ts`'s three free-form-vocabulary resolvers.
 * Unlike the exhaustive-union tests (`chatErrors.test.ts`, `mediaErrors.test.ts`,
 * `provenanceSource.test.ts`), there is no TypeScript union to iterate — these test the
 * every-currently-known-value case AND the must-see-red case together: a value the backend
 * could plausibly add without a matching frontend key must resolve through the generic
 * fallback, never render a raw dotted identifier.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import i18next from 'i18next';
import { initI18n } from './index';
import {
  resolveRedactionStyleI18nKey,
  resolveRedactionDetectorI18nKey,
  resolveRedactionCategoryI18nKey,
} from './contentRedactionVocab';

describe('content redaction option copy (#970)', () => {
  beforeAll(async () => {
    await initI18n('en');
  });

  describe('known values resolve to real translated copy', () => {
    it.each(['label', 'asterisks', 'first_letter', 'blur'])('style "%s"', (style) => {
      const key = resolveRedactionStyleI18nKey(style);
      const text = i18next.t(key);
      expect(text).not.toBe(key);
      expect(text.length).toBeGreaterThan(0);
    });

    it.each(['profanity', 'pii', 'toxicity', 'llm'])('detector "%s"', (detector) => {
      const key = resolveRedactionDetectorI18nKey(detector);
      const text = i18next.t(key);
      expect(text).not.toBe(key);
      expect(text.length).toBeGreaterThan(0);
    });

    it.each(['profanity', 'pii', 'toxicity', 'custom'])('category "%s"', (category) => {
      const key = resolveRedactionCategoryI18nKey(category);
      const text = i18next.t(key);
      expect(text).not.toBe(key);
      expect(text.length).toBeGreaterThan(0);
    });
  });

  describe('a backend value with no matching frontend key never renders a raw dot-key', () => {
    it('unrecognized style falls back to the generic option key', () => {
      const key = resolveRedactionStyleI18nKey('future_style');
      expect(key).toBe('settings.contentRedaction.unrecognizedOption');
      const text = i18next.t(key, { value: 'future_style' });
      expect(text).not.toBe(key);
      expect(text).not.toBe('settings.contentRedaction.styleOption.future_style');
      expect(text).toContain('future_style');
    });

    it('unrecognized detector falls back to the generic option key', () => {
      const key = resolveRedactionDetectorI18nKey('future_detector');
      expect(key).toBe('settings.contentRedaction.unrecognizedOption');
      const text = i18next.t(key, { value: 'future_detector' });
      expect(text).not.toBe('settings.contentRedaction.detector.future_detector');
    });

    it('unrecognized category falls back to the generic option key', () => {
      const key = resolveRedactionCategoryI18nKey('future_category');
      expect(key).toBe('settings.contentRedaction.unrecognizedOption');
      const text = i18next.t(key, { value: 'future_category' });
      expect(text).not.toBe('settings.contentRedaction.category.future_category');
    });
  });
});
