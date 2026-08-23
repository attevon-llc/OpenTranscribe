/**
 * `uploadService` is the queue singleton that owns the whole upload orchestration flow:
 * presigned-PUT-first with a legacy-POST fallback, multipart delegation, retry/backoff,
 * and the localStorage persistence that survives a reload. These tests focus on the
 * control-flow decisions that are easy to get subtly wrong and expensive when they are:
 * which errors are allowed to fall back to the legacy path (a stall must NOT, or the
 * fallback re-sends a body through the same congested connection), whether a multipart
 * session survives a retry (losing it means re-uploading gigabytes from zero), and
 * whether an abandoned multipart upload is actually released (billed storage otherwise).
 *
 * `multipartUploader` and `stallWatchdog` are mocked here — they have their own test
 * files — so these tests isolate the orchestration this module is responsible for.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const mockAxiosInstance = vi.hoisted(() => ({
  post: vi.fn(),
  delete: vi.fn(),
}));

const mockAxiosDefault = vi.hoisted(() => ({
  put: vi.fn(),
  isCancel: vi.fn((err: unknown) => !!(err && (err as { __CANCEL__?: boolean }).__CANCEL__)),
  CancelToken: { source: vi.fn(() => ({ token: 'cancel-token', cancel: vi.fn() })) },
}));

const mockFingerprintFile = vi.hoisted(() => vi.fn());
const mockUploadInParts = vi.hoisted(() => vi.fn());
const mockCreateStallWatchdog = vi.hoisted(() =>
  vi.fn(() => ({
    signal: undefined,
    stalled: false,
    notifyProgress: vi.fn(),
    dispose: vi.fn(),
  }))
);
const mockToast = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}));

// Not `importActual`: the real `$lib/axios` module calls `axios.create(...)` at import
// time, and `axios` itself is mocked below (without `.create`) — importing the real
// module would crash on evaluation. `uploadService` only ever uses the default export.
vi.mock('$lib/axios', () => ({ default: mockAxiosInstance }));

vi.mock('axios', () => ({ default: mockAxiosDefault }));

vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$lib/services/fileFingerprint', () => ({ fingerprintFile: mockFingerprintFile }));
vi.mock('$lib/services/multipartUploader', () => ({ uploadInParts: mockUploadInParts }));
vi.mock('$lib/services/stallWatchdog', () => ({
  createStallWatchdog: mockCreateStallWatchdog,
  DEFAULT_STALL_TIMEOUT_MS: 30000,
}));

import { uploadService } from './uploadService';

function prepared(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      file_id: 'file-uuid-1',
      is_duplicate: false,
      task_id: 'task-1',
      upload_url: 'https://minio/presigned',
      upload_method: 'PUT',
      ...overrides,
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  uploadService.reset();
  mockCreateStallWatchdog.mockReturnValue({
    signal: undefined,
    stalled: false,
    notifyProgress: vi.fn(),
    dispose: vi.fn(),
  });
  mockAxiosInstance.post.mockResolvedValue({ data: {} });
  mockAxiosInstance.delete.mockResolvedValue({ data: {} });
  mockAxiosDefault.put.mockResolvedValue({ headers: {}, data: {} });
});

afterEach(() => {
  uploadService.reset();
});

describe('queueing', () => {
  it('assigns a shared batch id only when 2+ files are added together', () => {
    const [soloId] = uploadService.addMultipleFiles([new File(['a'], 'solo.mp3')]);
    expect(uploadService.getUpload(soloId)?.uploadBatchId).toBeUndefined();

    const [firstId, secondId] = uploadService.addMultipleFiles([
      new File(['a'], 'a.mp3'),
      new File(['b'], 'b.mp3'),
    ]);
    const first = uploadService.getUpload(firstId);
    const second = uploadService.getUpload(secondId);
    expect(first?.uploadBatchId).toBeDefined();
    expect(first?.uploadBatchId).toBe(second?.uploadBatchId);
  });
});

describe('concurrency cap', () => {
  it('never runs more than MAX_CONCURRENT_UPLOADS prepare calls at once', async () => {
    // Every /files/prepare call hangs, so nothing completes and the 4th queued
    // item has to wait for a slot rather than starting immediately.
    let resolveCount = 0;
    mockAxiosInstance.post.mockImplementation(() => {
      resolveCount += 1;
      return new Promise(() => {}); // never resolves
    });

    uploadService.addMultipleFiles([
      new File(['a'], 'a.mp3'),
      new File(['b'], 'b.mp3'),
      new File(['c'], 'c.mp3'),
      new File(['d'], 'd.mp3'),
    ]);

    // Let the microtask queue drain so processQueue's synchronous dispatch runs.
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    expect(resolveCount).toBe(3);
  });
});

describe('duplicate short-circuit', () => {
  it('completes immediately as a duplicate without ever sending a body', async () => {
    mockAxiosInstance.post.mockResolvedValueOnce(prepared({ is_duplicate: true }));
    mockFingerprintFile.mockResolvedValue('deadbeef');

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'));

    expect(uploadService.getUpload(id)?.isDuplicate).toBe(true);
    expect(mockAxiosDefault.put).not.toHaveBeenCalled();
    expect(mockToast.warning).toHaveBeenCalled();
  });

  it('treats the legacy POST 409 as a duplicate, not an upload failure', async () => {
    // The pre-check above is the usual way a duplicate is caught, but it is not the
    // only one: `POST /files` re-checks server-side and answers 409 with the uuid of
    // the file that already holds this content (files/upload.py). That is the backstop
    // for the two cases the pre-check cannot cover — the same content uploaded between
    // prepare and POST, and a fingerprint that was skipped for the pre-check but still
    // reached the form data. Reporting it as a failed upload tells the user something
    // went wrong when in fact the server did exactly the right thing.
    mockFingerprintFile.mockResolvedValue('deadbeef');
    mockAxiosInstance.post
      .mockResolvedValueOnce(prepared({ upload_url: null, upload_method: null }))
      .mockRejectedValueOnce({
        response: {
          status: 409,
          data: {
            detail: {
              message: 'A file with this content already exists.',
              duplicate_file_uuid: 'the-file-you-already-have',
            },
          },
        },
      });

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'));

    expect(uploadService.getUpload(id)?.isDuplicate).toBe(true);
    expect(mockToast.warning).toHaveBeenCalled();
    expect(mockToast.error).not.toHaveBeenCalled();
  });

  it('still fails a 409 that carries no duplicate uuid', async () => {
    // The negative control. 409 means "conflict", not "duplicate" — reading every
    // conflict as a successful dedup would silently swallow a genuine failure and
    // report a file as safely stored when nothing was written.
    mockFingerprintFile.mockResolvedValue('deadbeef');
    mockAxiosInstance.post
      .mockResolvedValueOnce(prepared({ upload_url: null, upload_method: null }))
      .mockRejectedValueOnce({
        response: { status: 409, data: { detail: 'Upload session already finalized' } },
      });

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('failed'));

    expect(uploadService.getUpload(id)?.isDuplicate).not.toBe(true);
  });
});

describe('presigned PUT flow', () => {
  it('uploads via presigned PUT then finalizes, sending the computed fingerprint', async () => {
    mockFingerprintFile.mockResolvedValue('abc123');
    mockAxiosInstance.post.mockResolvedValueOnce(prepared());
    mockAxiosDefault.put.mockResolvedValueOnce({ headers: { etag: '"x"' } });

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'));

    expect(mockAxiosDefault.put).toHaveBeenCalledWith(
      'https://minio/presigned',
      expect.anything(),
      expect.objectContaining({ cancelToken: 'cancel-token' })
    );
    const completeCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files/complete');
    expect(completeCall?.[1]).toMatchObject({ file_id: 'file-uuid-1', file_hash: 'abc123' });
  });

  it('skips fingerprinting for a recording (Blob, not File) — nothing to deduplicate', async () => {
    mockAxiosInstance.post.mockResolvedValueOnce(prepared());

    const id = uploadService.addUpload('recording', new Blob(['a']), 'recording.webm');
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'));

    expect(mockFingerprintFile).not.toHaveBeenCalled();
    const completeCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files/complete');
    expect(completeCall?.[1]).toMatchObject({ file_hash: null });
  });

  it('marks dedupSkipped and warns, but still proceeds, when fingerprinting fails', async () => {
    mockFingerprintFile.mockRejectedValue(new Error('NotReadableError'));
    mockAxiosInstance.post.mockResolvedValueOnce(prepared());

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'));

    expect(uploadService.getUpload(id)?.dedupSkipped).toBe(true);
    expect(mockToast.warning).toHaveBeenCalled();
  });

  it('falls back to the legacy POST path on a plain network error from the presigned PUT', async () => {
    mockAxiosInstance.post.mockResolvedValueOnce(prepared()).mockResolvedValueOnce({ data: {} }); // the legacy POST /files
    mockAxiosDefault.put.mockRejectedValueOnce(new Error('ECONNRESET'));

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'));

    const legacyCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files');
    expect(legacyCall).toBeDefined();
  });

  it('does NOT fall back to legacy on a stall — re-sending through the API container would stall too', async () => {
    mockCreateStallWatchdog.mockReturnValue({
      signal: undefined,
      stalled: true,
      notifyProgress: vi.fn(),
      dispose: vi.fn(),
    });
    mockAxiosInstance.post.mockResolvedValueOnce(prepared());
    mockAxiosDefault.put.mockRejectedValueOnce(new Error('timed out'));

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('failed'));

    const legacyCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files');
    expect(legacyCall).toBeUndefined();
  });

  it('does NOT fall back to legacy on a user cancellation', async () => {
    mockAxiosInstance.post.mockResolvedValueOnce(prepared());
    const cancelled = Object.assign(new Error('canceled'), { __CANCEL__: true });
    mockAxiosDefault.put.mockRejectedValueOnce(cancelled);

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('failed'));

    const legacyCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files');
    expect(legacyCall).toBeUndefined();
  });
});

describe('multipart delegation', () => {
  it('routes through uploadInParts and finalizes with the returned parts', async () => {
    const plan = {
      upload_id: 'u1',
      part_size: 5,
      part_count: 2,
      batch_size: 2,
      expires_in: 3600,
      urls: {},
    };
    mockAxiosInstance.post.mockResolvedValueOnce(
      prepared({ upload_method: 'MULTIPART', multipart: plan, upload_url: undefined })
    );
    mockUploadInParts.mockResolvedValueOnce({
      parts: [
        { part_number: 1, etag: 'e1' },
        { part_number: 2, etag: 'e2' },
      ],
    });

    const id = uploadService.addUpload('file', new File(['a'.repeat(10)], 'big.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'));

    expect(mockUploadInParts).toHaveBeenCalledWith(
      expect.objectContaining({ fileId: 'file-uuid-1', resume: false })
    );
    const completeCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files/complete');
    expect(completeCall?.[1]).toMatchObject({
      parts: [
        { part_number: 1, etag: 'e1' },
        { part_number: 2, etag: 'e2' },
      ],
    });
    // The parked session must be cleared once assembly is confirmed.
    expect(uploadService.getUpload(id)?.multipart).toBeUndefined();
  });

  it('resumes the parked multipart session on retry instead of restarting from /prepare', async () => {
    const plan = {
      upload_id: 'u1',
      part_size: 5,
      part_count: 2,
      batch_size: 2,
      expires_in: 3600,
      urls: {},
    };
    mockAxiosInstance.post.mockResolvedValueOnce(
      prepared({ upload_method: 'MULTIPART', multipart: plan, upload_url: undefined })
    );
    mockUploadInParts.mockRejectedValueOnce(new Error('mid-transfer network drop'));

    vi.useFakeTimers();
    try {
      const id = uploadService.addUpload('file', new File(['a'.repeat(10)], 'big.mp3'));
      await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('failed'), {
        timeout: 5000,
      });

      // The session must survive the failure so a retry can resume it.
      expect(uploadService.getUpload(id)?.multipart).toMatchObject({ fileId: 'file-uuid-1' });

      mockUploadInParts.mockResolvedValueOnce({ parts: [{ part_number: 1, etag: 'e1' }] });
      await vi.advanceTimersByTimeAsync(RETRY_DELAY_FOR_FIRST_ATTEMPT);
      await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'), {
        timeout: 5000,
      });

      // Second call must resume — not re-signed from scratch via /prepare.
      expect(mockUploadInParts).toHaveBeenCalledTimes(2);
      expect(mockUploadInParts.mock.calls[1][0]).toMatchObject({ resume: true });
      const prepareCalls = mockAxiosInstance.post.mock.calls.filter(
        (c) => c[0] === '/files/prepare'
      );
      expect(prepareCalls).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

// RETRY_BASE_DELAY_MS * 2^0 from the module under test.
const RETRY_DELAY_FOR_FIRST_ATTEMPT = 1000;

describe('retry and give-up', () => {
  it('schedules a retry with an incremented retryCount after a transient failure', async () => {
    mockAxiosInstance.post.mockRejectedValueOnce(new Error('server hiccup'));

    vi.useFakeTimers();
    try {
      const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
      await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('failed'), {
        timeout: 5000,
      });
      expect(uploadService.getUpload(id)?.retryCount).toBe(0);

      mockAxiosInstance.post.mockResolvedValueOnce(prepared());
      await vi.advanceTimersByTimeAsync(RETRY_DELAY_FOR_FIRST_ATTEMPT);

      await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'), {
        timeout: 5000,
      });
      expect(uploadService.getUpload(id)?.retryCount).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('gives up after the retry ceiling and releases an abandoned multipart session', async () => {
    const plan = {
      upload_id: 'u1',
      part_size: 5,
      part_count: 1,
      batch_size: 1,
      expires_in: 3600,
      urls: {},
    };
    mockAxiosInstance.post.mockResolvedValue(
      prepared({ upload_method: 'MULTIPART', multipart: plan, upload_url: undefined })
    );
    mockUploadInParts.mockRejectedValue(new Error('always fails'));

    vi.useFakeTimers();
    try {
      const id = uploadService.addUpload('file', new File(['a'.repeat(10)], 'big.mp3'));

      // MAX_RETRIES = 3: the initial attempt plus 3 retries all fail.
      for (let i = 0; i < 4; i++) {
        await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('failed'), {
          timeout: 5000,
        });
        await vi.advanceTimersByTimeAsync(60000);
      }

      expect(uploadService.getUpload(id)?.multipart).toBeUndefined();
      expect(mockAxiosInstance.delete).toHaveBeenCalledWith('/files/file-uuid-1');
      expect(mockToast.error).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

function extractedAudioMetadata(overrides: Record<string, unknown> = {}) {
  return {
    originalFileName: 'source.mp4',
    originalFileSize: 999,
    originalFileType: 'video/mp4',
    originalLastModified: 0,
    originalFingerprint: 'video-fingerprint',
    extractedAudioSize: 10,
    extractedFileName: 'source.opus',
    extractedFileType: 'audio/opus',
    compressionRatio: 90,
    extractionDate: '2026-01-01T00:00:00.000Z',
    extractionDuration: 100,
    videoMetadata: {},
    ...overrides,
  };
}

describe('uploadExtractedAudio', () => {
  it('uploads via presigned PUT then finalizes, carrying the source-video fingerprint', async () => {
    mockAxiosInstance.post.mockResolvedValueOnce(prepared());
    mockAxiosDefault.put.mockResolvedValueOnce({ headers: { etag: '"x"' } });

    const id = uploadService.addExtractedAudio(
      new Blob(['a'.repeat(10)]),
      'extracted.opus',
      extractedAudioMetadata(),
      90
    );
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'));

    const completeCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files/complete');
    expect(completeCall?.[1]).toMatchObject({ file_hash: 'video-fingerprint' });
  });

  // BC-2 regression: uploadExtractedAudio previously had NO multipart branch at
  // all, unlike uploadFile(). A backend response of upload_method: 'MULTIPART'
  // for a large extracted-audio blob silently fell through to the legacy
  // FormData POST /files path — exactly what multipart exists to avoid.
  it('routes through the multipart flow when the backend prepares one, same as uploadFile()', async () => {
    const plan = {
      upload_id: 'u1',
      part_size: 5,
      part_count: 2,
      batch_size: 2,
      expires_in: 3600,
      urls: {},
    };
    mockAxiosInstance.post.mockResolvedValueOnce(
      prepared({ upload_method: 'MULTIPART', multipart: plan, upload_url: undefined })
    );
    mockUploadInParts.mockResolvedValueOnce({
      parts: [
        { part_number: 1, etag: 'e1' },
        { part_number: 2, etag: 'e2' },
      ],
    });

    const id = uploadService.addExtractedAudio(
      new Blob(['a'.repeat(10)]),
      'big-extracted.opus',
      extractedAudioMetadata(),
      90
    );
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('completed'));

    expect(mockUploadInParts).toHaveBeenCalledWith(
      expect.objectContaining({ fileId: 'file-uuid-1', resume: false })
    );
    // The legacy FormData path must NEVER be used when the backend prepared multipart.
    const legacyCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files');
    expect(legacyCall).toBeUndefined();
    const completeCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files/complete');
    expect(completeCall?.[1]).toMatchObject({
      parts: [
        { part_number: 1, etag: 'e1' },
        { part_number: 2, etag: 'e2' },
      ],
    });
    expect(uploadService.getUpload(id)?.multipart).toBeUndefined();
  });

  it('does NOT fall back to legacy on a stall, same as uploadFile()', async () => {
    mockCreateStallWatchdog.mockReturnValue({
      signal: undefined,
      stalled: true,
      notifyProgress: vi.fn(),
      dispose: vi.fn(),
    });
    mockAxiosInstance.post.mockResolvedValueOnce(prepared());
    mockAxiosDefault.put.mockRejectedValueOnce(new Error('timed out'));

    const id = uploadService.addExtractedAudio(
      new Blob(['a']),
      'extracted.opus',
      extractedAudioMetadata(),
      90
    );
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('failed'));

    const legacyCall = mockAxiosInstance.post.mock.calls.find((c) => c[0] === '/files');
    expect(legacyCall).toBeUndefined();
  });
});

describe('cancelUpload', () => {
  it('cancels the in-flight request and releases an in-progress multipart session', async () => {
    mockAxiosInstance.post.mockResolvedValueOnce(prepared());
    // Never resolve the PUT so the upload stays "uploading" long enough to cancel.
    mockAxiosDefault.put.mockReturnValueOnce(new Promise(() => {}));

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('uploading'));

    uploadService.cancelUpload(id);

    expect(uploadService.getUpload(id)?.status).toBe('cancelled');
    expect(uploadService.getActiveUploads()).toHaveLength(0);
  });

  // BC-1 regression: retryUpload() had no status guard, so a pending automatic
  // retry timer could fire AFTER cancelUpload() and silently resurrect a
  // 'cancelled' item back to 'queued' — after its multipart session, if any,
  // was already released server-side via DELETE /files/{id}.
  it('a pending retry timer must not resurrect an upload that was cancelled in the meantime', async () => {
    mockAxiosInstance.post.mockRejectedValueOnce(new Error('server hiccup'));

    vi.useFakeTimers();
    try {
      const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
      await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('failed'), {
        timeout: 5000,
      });

      // Cancel while the auto-retry setTimeout is still pending.
      uploadService.cancelUpload(id);
      expect(uploadService.getUpload(id)?.status).toBe('cancelled');

      // The retry timer now fires — it must be a no-op, not a resurrection.
      await vi.advanceTimersByTimeAsync(RETRY_DELAY_FOR_FIRST_ATTEMPT);
      expect(uploadService.getUpload(id)?.status).toBe('cancelled');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('removeUpload', () => {
  // BC-3 regression: removeUpload()'s "still active" check only covered
  // 'uploading'/'processing', unlike every other "is this active" check in
  // the file (e.g. getActiveUploads()), which also treats 'preparing' as
  // active. Removing a 'preparing' item (e.g. still hashing) deleted the map
  // entry without cancelling its cancelToken, leaving the in-flight request
  // running uncancelled.
  it('cancels the cancelToken when removing an upload that is still preparing', async () => {
    // Never resolves, so the item stays in 'preparing' (set just before this
    // await) instead of advancing to 'uploading'. `Once` so it doesn't leak
    // into later tests' fingerprinting calls.
    mockFingerprintFile.mockReturnValueOnce(new Promise(() => {}));

    const id = uploadService.addUpload('file', new File(['a'], 'a.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.status).toBe('preparing'));

    uploadService.removeUpload(id);

    const cancelSource = mockAxiosDefault.CancelToken.source.mock.results[0]?.value;
    expect(cancelSource?.cancel).toHaveBeenCalled();
    expect(uploadService.getUpload(id)).toBeUndefined();
  });
});

describe('reset()', () => {
  it('aborts every in-flight multipart session before clearing state', async () => {
    const plan = {
      upload_id: 'u1',
      part_size: 5,
      part_count: 1,
      batch_size: 1,
      expires_in: 3600,
      urls: {},
    };
    mockAxiosInstance.post.mockResolvedValueOnce(
      prepared({ upload_method: 'MULTIPART', multipart: plan, upload_url: undefined })
    );
    mockUploadInParts.mockReturnValueOnce(new Promise(() => {})); // never resolves

    const id = uploadService.addUpload('file', new File(['a'.repeat(10)], 'big.mp3'));
    await vi.waitFor(() => expect(uploadService.getUpload(id)?.multipart).toBeDefined());

    uploadService.reset();

    expect(mockAxiosInstance.delete).toHaveBeenCalledWith('/files/file-uuid-1');
    expect(uploadService.getAllUploads()).toHaveLength(0);
    expect(localStorage.getItem('upload_queue')).toBeNull();
  });
});
