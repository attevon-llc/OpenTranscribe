/**
 * What each accepted upload format does in the browser player.
 *
 * Everything listed is accepted and transcribed. The three groups say what the player
 * can do with it. They follow the backend's measured playability tables
 * (`backend/app/services/playback_rendition.py`): a format is "playable" only if
 * Playwright Firefox and Chromium both played it, with sound and, for video, a picture.
 * Keep the two in step. The upload panel lists these groups, so a format in the wrong
 * group advertises playback the user won't get.
 */

/** Plays as uploaded. */
export const BROWSER_PLAYABLE_FORMATS = [
  'MP3',
  'WAV',
  'FLAC',
  'OGG',
  'Opus',
  'AAC',
  'M4A',
  'WebM',
  'MP4',
  'MOV',
  'MKV',
] as const;

/** Audio no browser decodes. A playable AAC copy is made automatically. */
export const CONVERTED_AUDIO_FORMATS = ['AIFF', 'WMA', 'ALAC', 'AMR', 'AC-3', 'MP2'] as const;

/** Video no browser shows. Transcribed; the preview plays the audio track only. */
export const AUDIO_ONLY_PREVIEW_FORMATS = ['AVI', 'WMV', 'MPEG', 'MPEG-TS', 'FLV', '3GP'] as const;

/**
 * MIME type by file extension, for files the browser hands over with an empty `type`.
 * Also the extension allowlist for multi-file drops.
 */
export const MEDIA_TYPE_BY_EXTENSION: Record<string, string> = {
  mp3: 'audio/mpeg',
  wav: 'audio/wav',
  ogg: 'audio/ogg',
  oga: 'audio/ogg',
  opus: 'audio/ogg',
  flac: 'audio/flac',
  aac: 'audio/aac',
  m4a: 'audio/mp4',
  aif: 'audio/x-aiff',
  aiff: 'audio/x-aiff',
  aifc: 'audio/x-aiff',
  wma: 'audio/x-ms-wma',
  amr: 'audio/amr',
  ac3: 'audio/ac3',
  mp2: 'audio/mpeg',
  mka: 'audio/x-matroska',
  ra: 'audio/vnd.rn-realaudio',
  ram: 'audio/vnd.rn-realaudio',
  weba: 'audio/webm',
  '3ga': 'audio/3gpp',
  '3gp': 'audio/3gpp',
  '3g2': 'audio/3gpp2',
  mp4: 'video/mp4',
  f4v: 'video/mp4',
  webm: 'video/webm',
  ogv: 'video/ogg',
  mov: 'video/quicktime',
  avi: 'video/x-msvideo',
  wmv: 'video/x-ms-wmv',
  asf: 'video/x-ms-asf',
  mkv: 'video/x-matroska',
  m4v: 'video/x-m4v',
  mpeg: 'video/mpeg',
  mpg: 'video/mpeg',
  ts: 'video/mp2t',
  flv: 'video/x-flv',
};

/** The file's extension, lowercased, without the dot ('' when it has none). */
export function fileExtension(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot < 0 ? '' : name.slice(dot + 1).toLowerCase();
}
