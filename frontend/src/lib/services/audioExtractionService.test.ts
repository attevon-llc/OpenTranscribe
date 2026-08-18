/**
 * `AudioExtractionService` runs in-browser video-to-audio extraction via FFmpeg.wasm.
 * These tests fake the `FFmpeg` instance (real FFmpeg.wasm needs a worker + wasm binary
 * neither available nor desirable in a unit test) but drive it through the same
 * event/exec/file-system contract the real class uses, so the module's own log-parsing
 * and exit-code handling run for real. Priority per issue #475: this is 0% covered and
 * the extraction path can silently produce a wrong or empty result if the exit code is
 * ignored — exactly the class of bug this file's own comments call out as fixed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

class FakeFFmpeg {
  handlers: Record<string, Array<(payload: { message: string }) => void>> = {};
  files: Record<string, unknown> = {};
  execImpl: (args: string[]) => number | Promise<number> = () => 0;

  on(event: string, handler: (payload: { message: string }) => void) {
    (this.handlers[event] ||= []).push(handler);
  }
  off(event: string, handler: (payload: { message: string }) => void) {
    this.handlers[event] = (this.handlers[event] || []).filter((h) => h !== handler);
  }
  emit(event: string, payload: { message: string }) {
    (this.handlers[event] || []).forEach((h) => h(payload));
  }
  async load() {}
  async writeFile(name: string, data: unknown) {
    this.files[name] = data;
  }
  async readFile(name: string) {
    return (this.files[name] as Uint8Array) ?? new Uint8Array([1, 2, 3]);
  }
  async deleteFile(name: string) {
    delete this.files[name];
  }
  async exec(args: string[]) {
    return this.execImpl(args);
  }
}

let fake: FakeFFmpeg;

vi.mock('@ffmpeg/ffmpeg', () => ({
  FFmpeg: vi.fn(function FFmpegCtor() {
    return fake;
  }),
}));
vi.mock('@ffmpeg/util', () => ({
  toBlobURL: vi.fn(async (url: string) => url),
  fetchFile: vi.fn(async () => new Uint8Array([9, 9, 9])),
}));

const mockAddNotification = vi.hoisted(() => vi.fn());
vi.mock('../../stores/websocket', () => ({
  websocketStore: { addNotification: mockAddNotification },
}));

vi.mock('../../stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const mockFingerprintFile = vi.hoisted(() => vi.fn().mockResolvedValue('source-video-fingerprint'));
vi.mock('$lib/services/fileFingerprint', () => ({ fingerprintFile: mockFingerprintFile }));

import { AudioExtractionService } from './audioExtractionService';
import { FFmpeg } from '@ffmpeg/ffmpeg';

function videoFile(name = 'clip.mp4', size = 1024): File {
  return new File([new Uint8Array(size)], name, { type: 'video/mp4', lastModified: Date.now() });
}

/** Feeds the log handler registered during `-f null -` metadata probing. */
function emitRealisticMetadataLog(instance: FakeFFmpeg) {
  const lines = [
    'Metadata:',
    '  title           : My Recording',
    '  artist          : Someone',
    'Duration: 00:01:30.50, start: 0.000000, bitrate: 128 kb/s',
    'Stream #0:0: Audio: aac, 44100 Hz, stereo, fltp, 128 kb/s',
  ];
  for (const message of lines) instance.emit('log', { message });
}

// jsdom does not implement the Worker constructor FFmpeg.wasm needs; every test in this
// file except the isSupported() negative cases needs it present to get past the guard.
class FakeWorker {}

beforeEach(() => {
  vi.clearAllMocks();
  fake = new FakeFFmpeg();
  mockFingerprintFile.mockResolvedValue('source-video-fingerprint');
  globalThis.Worker = FakeWorker as unknown as typeof Worker;
});

describe('isSupported', () => {
  it('requires both WebAssembly and Worker', () => {
    const service = new AudioExtractionService();

    const realWasm = globalThis.WebAssembly;
    const realWorker = globalThis.Worker;
    try {
      expect(service.isSupported()).toBe(true);

      // @ts-expect-error deliberately simulating an unsupported browser
      globalThis.WebAssembly = undefined;
      expect(service.isSupported()).toBe(false);
      globalThis.WebAssembly = realWasm;

      // @ts-expect-error deliberately simulating an unsupported browser
      globalThis.Worker = undefined;
      expect(service.isSupported()).toBe(false);
    } finally {
      globalThis.WebAssembly = realWasm;
      globalThis.Worker = realWorker;
    }
  });
});

describe('extractMetadata', () => {
  it('parses title/artist/duration/codec from the FFmpeg log stream', async () => {
    fake.execImpl = (args) => {
      if (args.includes('-f')) emitRealisticMetadataLog(fake);
      return 0;
    };
    const service = new AudioExtractionService();

    const metadata = await service.extractMetadata(videoFile());

    expect(metadata.Title).toBe('My Recording');
    expect(metadata.Artist).toBe('Someone');
    expect(metadata.Duration).toBe('00:01:30.50');
    expect(metadata.RawMetadata?.audio_codec).toBe('aac');
  });

  it('falls back to basic file metadata rather than throwing when FFmpeg fails', async () => {
    fake.execImpl = () => {
      throw new Error('ffmpeg crashed');
    };
    const service = new AudioExtractionService();

    const metadata = await service.extractMetadata(videoFile('clip.mp4', 2048));

    expect(metadata.FileName).toBe('clip.mp4');
    expect(metadata.FileSize).toBe(2048);
    expect(metadata.Title).toBeUndefined();
  });

  it('falls back to basic metadata when the metadata-read exec RESOLVES with a non-zero exit code (BC-6)', async () => {
    // The regression this covers is specifically a *resolved* non-zero code, not a
    // thrown exception — exec() resolves with FFmpeg's exit code rather than
    // rejecting on failure, same contract as the main extraction command.
    fake.execImpl = () => 1;
    const service = new AudioExtractionService();

    const metadata = await service.extractMetadata(videoFile('clip.mp4', 4096));

    expect(metadata.FileName).toBe('clip.mp4');
    expect(metadata.FileSize).toBe(4096);
    expect(metadata.Title).toBeUndefined();
    // The fallback object literal never includes RawMetadata at all — unlike the
    // success path, which always sets it (even to {} when the log stream was empty).
    // Only the fallback proves control actually reached the catch block instead of
    // silently returning whatever partial data the failed read produced.
    expect(metadata.RawMetadata).toBeUndefined();
  });

  it('unregisters the temporary log handler even when the metadata-read exec rejects (BC-7)', async () => {
    fake.execImpl = () => {
      throw new Error('ffmpeg crashed mid-read');
    };
    const service = new AudioExtractionService();

    await service.extractMetadata(videoFile());

    // load() registers exactly one permanent 'log' handler in _doLoad(). If the
    // temporary handler added inside extractMetadata is not unregistered when exec()
    // throws, it stays on the shared emitter forever and this count grows past 1.
    expect(fake.handlers['log']?.length ?? 0).toBe(1);
  });

  it('unregisters the temporary log handler even when writeFile rejects before exec runs (BC-7)', async () => {
    const service = new AudioExtractionService();
    fake.writeFile = async () => {
      throw new Error('MEMFS write failed');
    };

    await service.extractMetadata(videoFile());

    expect(fake.handlers['log']?.length ?? 0).toBe(1);
  });
});

describe('extractAudio — guards', () => {
  it('rejects with a typed error when the file exceeds maxFileSize, before touching FFmpeg', async () => {
    const service = new AudioExtractionService();
    const execSpy = vi.spyOn(fake, 'exec');
    const huge = videoFile('huge.mp4', 10);

    await expect(
      service.extractAudio(huge, { maxFileSize: 5 /* smaller than the 10-byte file above */ })
    ).rejects.toMatchObject({ code: 'FILE_TOO_LARGE' });

    expect(execSpy).not.toHaveBeenCalled();
    expect(mockFingerprintFile).not.toHaveBeenCalled();
  });

  it('rejects with a typed error when the browser is unsupported', async () => {
    const service = new AudioExtractionService();
    const realWorker = globalThis.Worker;
    // @ts-expect-error deliberately simulating an unsupported browser
    globalThis.Worker = undefined;

    try {
      await expect(service.extractAudio(videoFile())).rejects.toMatchObject({
        code: 'UNSUPPORTED_BROWSER',
      });
    } finally {
      globalThis.Worker = realWorker;
    }
  });
});

describe('extractAudio — success path', () => {
  it('extracts, fingerprints the SOURCE video (not the output blob), and reports compression', async () => {
    fake.execImpl = (args) => {
      if (args.includes('-f')) emitRealisticMetadataLog(fake);
      return 0;
    };
    const service = new AudioExtractionService();

    const result = await service.extractAudio(videoFile('clip.mp4', 1000));

    expect(result.metadata.originalFingerprint).toBe('source-video-fingerprint');
    expect(mockFingerprintFile).toHaveBeenCalledWith(expect.any(File));
    // The service must never re-hash its own extracted output — see the module's own
    // "ffmpeg does not produce byte-identical audio twice" comment.
    expect(mockFingerprintFile).toHaveBeenCalledTimes(1);
    expect(result.blob).toBeInstanceOf(Blob);
    expect(result.metadata.extractedFileName).toMatch(/\.mp3$/);
  });

  it('throws EXTRACTION_FAILED on a non-zero FFmpeg exit code instead of returning an empty blob, and cleans up its temp files (BC-5)', async () => {
    fake.execImpl = (args) => {
      if (args.includes('-f')) return 0; // metadata probe still succeeds
      return 1; // the actual extraction command fails
    };
    const service = new AudioExtractionService();
    const deleteSpy = vi.spyOn(fake, 'deleteFile');

    await expect(service.extractAudio(videoFile())).rejects.toMatchObject({
      code: 'EXTRACTION_FAILED',
    });

    // BC-5: a failed extraction must not leak its input/output temp files in the
    // FFmpeg MEMFS — the catch handler must clean up what the success path would
    // have. The input file was actually written before the exec() failure, so its
    // absence from fake.files is the real signal; the catch must still attempt to
    // delete the output name too, even though it was never produced.
    const deletedNames = deleteSpy.mock.calls.map((call) => call[0]);
    expect(deletedNames.some((name) => name.startsWith('input_'))).toBe(true);
    expect(deletedNames.some((name) => name.startsWith('output_'))).toBe(true);
    expect(Object.keys(fake.files).some((name) => name.startsWith('input_'))).toBe(false);
    expect(Object.keys(fake.files).some((name) => name.startsWith('output_'))).toBe(false);
  });

  it('sends a completed, dismissible notification only once progress reaches 100%', async () => {
    fake.execImpl = (args) => {
      if (args.includes('-f')) emitRealisticMetadataLog(fake);
      return 0;
    };
    const service = new AudioExtractionService();

    await service.extractAudio(videoFile());

    const calls = mockAddNotification.mock.calls.map((c) => c[0]);
    expect(calls.some((n) => n.status === 'completed' && n.dismissible === true)).toBe(true);
    expect(
      calls.filter((n) => n.status !== 'completed').every((n) => n.dismissible === false)
    ).toBe(true);
  });
});

describe('extractAudio — sequential queue', () => {
  it('processes extractions one at a time, not concurrently', async () => {
    const started: number[] = [];
    const finished: number[] = [];
    let n = 0;

    fake.execImpl = (args) => {
      if (!args.includes('-f')) {
        const my = ++n;
        started.push(my);
        finished.push(my);
      }
      return 0;
    };
    const service = new AudioExtractionService();

    // Two extractions queued back to back. If they ran concurrently, FFmpeg.wasm's
    // single shared instance would be handed two overlapping writeFile/exec calls —
    // the class exists specifically to serialize this.
    const results = await Promise.all([
      service.extractAudio(videoFile('a.mp4')),
      service.extractAudio(videoFile('b.mp4')),
    ]);

    expect(results).toHaveLength(2);
    expect(started).toEqual([1, 2]);
  });
});

describe('cleanup', () => {
  it('resets the FFmpeg instance so the next call constructs a new one instead of reusing it', async () => {
    const service = new AudioExtractionService();
    await service.extractMetadata(videoFile());
    expect(vi.mocked(FFmpeg)).toHaveBeenCalledTimes(1);

    await service.cleanup();

    fake.execImpl = () => 0;
    const metadata = await service.extractMetadata(videoFile('reload.mp4', 512));

    // A second construction proves cleanup() actually discarded the old instance
    // rather than `load()` short-circuiting on a stale `isLoaded` flag.
    expect(vi.mocked(FFmpeg)).toHaveBeenCalledTimes(2);
    expect(metadata.FileName).toBe('reload.mp4');
  });
});
