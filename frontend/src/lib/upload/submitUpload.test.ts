import { describe, it, expect, vi } from 'vitest';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (v: (k: string, v?: Record<string, unknown>) => string) => void) => {
      run((k: string) => k);
      return () => {};
    },
  },
}));
vi.mock('$lib/utils/uploadLimits', () => ({
  getMaxUploadBytes: () => 1000,
  exceedsUploadLimit: (n: number) => n > 1000,
  warrantsLargeUploadWarning: (n: number) => n > 500,
}));

import {
  buildUrlPayload,
  partitionMultipleFiles,
  urlErrorToast,
  validateSelectedFile,
} from './submitUpload';

const file = (name: string, type: string, size: number) => {
  const f = new File(['x'], name, { type });
  Object.defineProperty(f, 'size', { value: size });
  return f;
};

describe('validateSelectedFile', () => {
  it('rejects non-media and oversize, warns on large', () => {
    expect(validateSelectedFile(file('a.txt', 'text/plain', 1))).toEqual({
      ok: false,
      error: 'uploader.fileTypeError',
    });
    expect(validateSelectedFile(file('a.mp3', 'audio/mpeg', 2000)).error).toBe(
      'uploader.fileTooLargeError'
    );
    expect(validateSelectedFile(file('a.mp3', 'audio/mpeg', 600))).toEqual({
      ok: true,
      error: 'uploader.largeFileWarning',
    });
    expect(validateSelectedFile(file('a.mp3', 'audio/mpeg', 10))).toEqual({ ok: true, error: '' });
  });
});

describe('partitionMultipleFiles', () => {
  it('accepts by mime or known extension and reports the rest', () => {
    const { validFiles, invalidFiles } = partitionMultipleFiles([
      file('a.mp3', 'audio/mpeg', 1),
      file('b.mp3', '', 1),
      file('c.xyz', '', 1),
      file('d.mp3', 'audio/mpeg', 5000),
    ]);
    expect(validFiles.map((f) => f.name)).toEqual(['a.mp3', 'b.mp3']);
    expect(invalidFiles).toEqual([
      'c.xyz (uploader.unsupportedFormat)',
      'd.mp3 (uploader.tooLarge)',
    ]);
  });
});

describe('buildUrlPayload', () => {
  it('includes only values that were set', () => {
    const p = buildUrlPayload(
      ' https://x.test/v ',
      { quality: 'best' },
      { collectionIds: ['c'], tagNames: undefined },
      true,
      { minSpeakers: 0, maxSpeakers: null, numSpeakers: undefined, whisperModel: 'tiny' }
    );
    expect(p).toEqual({
      url: 'https://x.test/v',
      quality: 'best',
      collection_ids: ['c'],
      skip_summary: true,
      min_speakers: 0,
      whisper_model: 'tiny',
    });
  });
});

describe('urlErrorToast', () => {
  it('maps 409 to a warning and 400 / other to errors', () => {
    expect(urlErrorToast({ response: { status: 409, data: { detail: 'dup' } } })).toEqual({
      kind: 'warning',
      message: 'dup',
    });
    expect(urlErrorToast({ response: { status: 400, data: {} } })).toEqual({
      kind: 'error',
      message: 'uploader.invalidMediaUrl',
    });
    expect(urlErrorToast(new Error('x')).message).toBe('uploader.failedToProcessUrl');
  });
});
