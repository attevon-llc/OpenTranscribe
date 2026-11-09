import { describe, it, expect, vi } from 'vitest';

vi.mock('axios', () => ({
  default: { isCancel: (e: unknown) => (e as { cancel?: boolean })?.cancel === true },
}));
vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import { formatTimeRemaining, getErrorMessage, getSourceName } from './format';

describe('formatTimeRemaining', () => {
  it.each([
    [0, ''],
    [-5, ''],
    [1500, '2s'],
    [60_000, '1m'],
    [90_000, '1m 30s'],
    [3_660_000, '1h 1m'],
  ])('%i ms -> %s', (ms, expected) => {
    expect(formatTimeRemaining(ms)).toBe(expected);
  });
});

describe('getSourceName', () => {
  it('uses the file name', () => {
    expect(getSourceName(new File(['a'], 'talk.mp3'))).toBe('talk.mp3');
  });
  it('names YouTube urls generically and others by last path segment', () => {
    expect(getSourceName('https://youtu.be/abc')).toBe('upload.youtubeVideo');
    expect(getSourceName('https://example.com/a/clip.mp4')).toBe('clip.mp4');
    expect(getSourceName('not a url')).toBe('upload.videoUrl');
  });
  it('timestamps a recording blob', () => {
    expect(getSourceName(new Blob(['x']))).toMatch(/^recording_.*\.webm$/);
  });
});

describe('getErrorMessage', () => {
  it('prefers cancel, then server detail, then message, then fallback', () => {
    expect(getErrorMessage({ cancel: true })).toBe('upload.cancelled');
    expect(getErrorMessage({ response: { data: { detail: 'boom' } }, message: 'm' })).toBe('boom');
    expect(getErrorMessage({ message: 'm' })).toBe('m');
    expect(getErrorMessage({})).toBe('common.unknownError');
  });
});
