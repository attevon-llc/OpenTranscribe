/**
 * DEFECT THIS CATCHES (issue #1044): the player rendered the stored content type
 * verbatim as `<source type>`. A WAV uploaded from Firefox on Linux is declared
 * `audio/vnd.wave`, which Firefox and Chromium both reject (`canPlayType` returns ""),
 * so the media never loaded and the player sat at 00:00. Rows stored before the
 * backend fix still carry these aliases, so the player has to cope with them.
 *
 * The matrix records what Playwright Firefox and Chromium actually did with a real
 * ffmpeg-generated fixture of each format. `hint` is the `<source type>` the player
 * may emit, and `undefined` means omit it. Omission is always safe: every playable
 * fixture loaded with no type hint in both browsers, but a hint the browser rejects
 * means it never fetches the bytes. `needsTranscode` marks formats neither browser
 * decodes at all. Those get no hint, and only a transcode would make them play.
 */
import { describe, expect, it } from 'vitest';
import { normalizeMediaType, playableSourceType } from './mediaType';

interface FormatCase {
  ext: string;
  declared: string[];
  canonical: string;
  hint: string | undefined;
  needsTranscode?: boolean;
}

const MATRIX: FormatCase[] = [
  {
    ext: 'mp3',
    declared: ['audio/mpeg', 'audio/mp3'],
    canonical: 'audio/mpeg',
    hint: 'audio/mpeg',
  },
  {
    ext: 'wav',
    declared: ['audio/wav', 'audio/vnd.wave', 'audio/x-wav', 'audio/wave'],
    canonical: 'audio/wav',
    hint: 'audio/wav',
  },
  { ext: 'ogg', declared: ['audio/ogg'], canonical: 'audio/ogg', hint: 'audio/ogg' },
  { ext: 'opus', declared: ['audio/ogg', 'audio/opus'], canonical: 'audio/ogg', hint: 'audio/ogg' },
  {
    ext: 'flac',
    declared: ['audio/flac', 'audio/x-flac'],
    canonical: 'audio/flac',
    hint: 'audio/flac',
  },
  {
    ext: 'aac',
    declared: ['audio/aac', 'audio/x-aac', 'audio/aacp'],
    canonical: 'audio/aac',
    hint: 'audio/aac',
  },
  {
    ext: 'm4a',
    declared: ['audio/mp4', 'audio/x-m4a', 'audio/m4a', 'audio/mp4a-latm'],
    canonical: 'audio/mp4',
    hint: 'audio/mp4',
  },
  { ext: 'mp4', declared: ['video/mp4'], canonical: 'video/mp4', hint: 'video/mp4' },
  { ext: 'webm', declared: ['video/webm'], canonical: 'video/webm', hint: 'video/webm' },
  { ext: 'weba', declared: ['audio/webm'], canonical: 'audio/webm', hint: 'audio/webm' },
  // Chromium rejects `video/quicktime` as a hint but plays an H.264 .mov without one.
  { ext: 'mov', declared: ['video/quicktime'], canonical: 'video/quicktime', hint: undefined },
  {
    ext: 'mkv',
    declared: ['video/x-matroska', 'video/matroska'],
    canonical: 'video/x-matroska',
    hint: 'video/x-matroska',
  },
  {
    ext: 'avi',
    declared: ['video/x-msvideo', 'video/vnd.avi', 'video/avi'],
    canonical: 'video/x-msvideo',
    hint: undefined,
    needsTranscode: true,
  },
  {
    ext: 'aiff',
    declared: ['audio/x-aiff', 'audio/aiff'],
    canonical: 'audio/x-aiff',
    hint: undefined,
    needsTranscode: true,
  },
];

describe('media format matrix', () => {
  for (const fmt of MATRIX) {
    for (const declared of fmt.declared) {
      it(`${fmt.ext}: ${declared} → ${fmt.canonical}, hint ${fmt.hint ?? '(omitted)'}`, () => {
        expect(normalizeMediaType(declared)).toBe(fmt.canonical);
        expect(playableSourceType(declared)).toBe(fmt.hint);
      });
    }
  }

  it('never hints a format that needs a transcode', () => {
    const unplayable = MATRIX.filter((f) => f.needsTranscode);
    expect(unplayable.map((f) => f.ext)).toEqual(['avi', 'aiff']);
    for (const fmt of unplayable) {
      for (const declared of fmt.declared) {
        expect(playableSourceType(declared)).toBeUndefined();
      }
    }
  });
});

describe('normalizeMediaType edge cases', () => {
  it('matches aliases case-insensitively and drops their parameters', () => {
    expect(normalizeMediaType('Audio/VND.Wave; rate=16000')).toBe('audio/wav');
  });

  it('keeps codec parameters on a type that is already canonical', () => {
    expect(normalizeMediaType('audio/webm;codecs=opus')).toBe('audio/webm;codecs=opus');
    expect(playableSourceType('audio/webm;codecs=opus')).toBe('audio/webm;codecs=opus');
  });

  it('passes empty / missing types through and never hints them', () => {
    expect(normalizeMediaType('')).toBe('');
    expect(normalizeMediaType(null)).toBeUndefined();
    expect(playableSourceType(undefined)).toBeUndefined();
    expect(playableSourceType('application/octet-stream')).toBeUndefined();
  });
});
