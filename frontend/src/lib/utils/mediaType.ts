/**
 * Browser-playable media MIME types (issue #1044).
 *
 * The declared type of an upload is the OS's name for it. On Linux,
 * shared-mime-info 2.4+ names `*.wav` as `audio/vnd.wave`, and Firefox and Chromium
 * both reject that name in `<source type>`. This mirrors the backend's
 * `app/utils/media_types.py`. Keep the two alias tables in sync: the backend
 * normalises new rows, and this module handles rows stored before that fix.
 */

const CANONICAL_BY_ALIAS: Record<string, string> = {
  'audio/vnd.wave': 'audio/wav',
  'audio/wave': 'audio/wav',
  'audio/x-wav': 'audio/wav',
  'audio/x-pn-wav': 'audio/wav',
  'audio/x-flac': 'audio/flac',
  'audio/x-aac': 'audio/aac',
  'audio/aacp': 'audio/aac',
  'audio/m4a': 'audio/mp4',
  'audio/x-m4a': 'audio/mp4',
  'audio/mp4a-latm': 'audio/mp4',
  'audio/mp3': 'audio/mpeg',
  'audio/x-mp3': 'audio/mpeg',
  'audio/mpeg3': 'audio/mpeg',
  'audio/x-mpeg': 'audio/mpeg',
  'audio/opus': 'audio/ogg',
  'video/matroska': 'video/x-matroska',
  'video/vnd.avi': 'video/x-msvideo',
  'video/avi': 'video/x-msvideo',
  'video/msvideo': 'video/x-msvideo',
  'audio/aiff': 'audio/x-aiff',
};

/**
 * Types that Firefox and Chromium were both measured to accept as a `<source type>`
 * hint for a real file of that format. Anything else gets no hint. A missing hint is
 * harmless because the browser sniffs the bytes, but a rejected hint stops playback.
 */
const SOURCE_TYPE_HINTS = new Set([
  'audio/mpeg',
  'audio/wav',
  'audio/ogg',
  'audio/flac',
  'audio/aac',
  'audio/mp4',
  'audio/webm',
  'video/mp4',
  'video/webm',
  'video/x-matroska',
]);

function baseType(contentType: string): string {
  return contentType.split(';', 1)[0].trim().toLowerCase();
}

/** Canonical spelling of a media MIME type. Unknown types pass through unchanged. */
export function normalizeMediaType(contentType: string | null | undefined): string | undefined {
  if (contentType == null) return undefined;
  if (!contentType) return contentType;
  return CANONICAL_BY_ALIAS[baseType(contentType)] ?? contentType;
}

/** The `<source type>` hint to render, or `undefined` to omit the attribute. */
export function playableSourceType(contentType: string | null | undefined): string | undefined {
  const normalized = normalizeMediaType(contentType);
  if (!normalized) return undefined;
  return SOURCE_TYPE_HINTS.has(baseType(normalized)) ? normalized : undefined;
}
