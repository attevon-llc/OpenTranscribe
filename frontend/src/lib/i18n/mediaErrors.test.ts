/**
 * GH #960. Modeled on `chatErrors.test.ts` (#964): exhaustive over the `MediaErrorReason`
 * union (typed as `Record<MediaErrorReason, string>` in `mediaErrors.ts`, so a ninth reason
 * fails to compile without an entry here too) and loads the REAL `en` locale bundle, so it
 * fails if a key is missing from the JSON just as much as if the mapper is wrong.
 *
 * This is the must-see-red case from the issue: a backend reason with no frontend copy must
 * never render as a raw i18n key.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import i18next from 'i18next';
import { initI18n } from './index';
import { resolveMediaErrorI18nKey } from './mediaErrors';
import type { MediaErrorReason } from '$lib/types/media';

const ALL_MEDIA_ERROR_REASONS: MediaErrorReason[] = [
  'file_quality',
  'no_audio_track',
  'no_speech',
  'format_issue',
  'processing_error',
  'network_error',
  'permission_error',
  'unclassified',
];

describe('media error banner copy for every MediaErrorReason (#960)', () => {
  beforeAll(async () => {
    await initI18n('en');
  });

  it.each(ALL_MEDIA_ERROR_REASONS)('renders human text for MediaErrorReason "%s"', (reason) => {
    const key = resolveMediaErrorI18nKey(reason);
    expect(key).toBeDefined();
    const text = i18next.t(key as string);

    // A raw dot-notation key coming back means there is no translation for it — the
    // pre-#960 shape this test exists to catch.
    expect(text).not.toBe(key);
    expect(text.length).toBeGreaterThan(0);
  });

  it('returns undefined for a reason it does not recognize, so callers fall back to user_message', () => {
    expect(resolveMediaErrorI18nKey('some_future_backend_reason')).toBeUndefined();
    expect(resolveMediaErrorI18nKey(null)).toBeUndefined();
    expect(resolveMediaErrorI18nKey(undefined)).toBeUndefined();
  });
});
