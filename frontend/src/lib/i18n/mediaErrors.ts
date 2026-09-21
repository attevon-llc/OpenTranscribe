/**
 * GH #960, reusing #964's `chatErrors.ts` pattern rather than inventing a second one.
 *
 * `MediaErrorReason` (the wire vocabulary, snake_case, mirrors the backend's
 * `UserErrorReason`) and the `errors.media.*` i18n namespace (camelCase) are independently
 * free to change — this map is the one place that has to know both.
 * `Record<MediaErrorReason, string>` has no index signature, so TypeScript refuses to
 * compile if a `MediaErrorReason` member is added here without a translated key: that is
 * what keeps this exhaustive, not a runtime check. See `chatErrors.ts` for the sibling.
 */
import type { MediaErrorReason } from '$lib/types/media';

const MEDIA_ERROR_REASON_I18N_KEY: Record<MediaErrorReason, string> = {
  file_quality: 'errors.media.fileQuality',
  no_audio_track: 'errors.media.noAudioTrack',
  no_speech: 'errors.media.noSpeech',
  format_issue: 'errors.media.formatIssue',
  processing_error: 'errors.media.processingError',
  network_error: 'errors.media.networkError',
  permission_error: 'errors.media.permissionError',
  unclassified: 'errors.media.unclassified',
};

const MEDIA_ERROR_REASONS = new Set<string>(Object.keys(MEDIA_ERROR_REASON_I18N_KEY));

/**
 * Resolve a backend `error_reason` string to its translated i18n key.
 *
 * Returns `undefined` for any value that is not a recognised `MediaErrorReason` — an old
 * client talking to a newer backend, or a `file.user_message` fallback already being used
 * instead. Callers fall back to `file.user_message` (server-authored English) in that case,
 * exactly as before this fix; that fallback is what GH #960's issue body's option (a)
 * requires, so an unrecognized reason degrades instead of rendering a raw key.
 */
export function resolveMediaErrorI18nKey(reason: string | null | undefined): string | undefined {
  if (!reason || !MEDIA_ERROR_REASONS.has(reason)) {
    return undefined;
  }
  return MEDIA_ERROR_REASON_I18N_KEY[reason as MediaErrorReason];
}
