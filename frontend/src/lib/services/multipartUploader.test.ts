/**
 * `uploadInParts` assembles a multi-GB object from independently-signed parts (issue #327).
 * These tests pin the correctness-critical paths: truncated-part detection on resume (a
 * silent miss here reassembles a corrupt object), terminal-vs-retryable error handling, and
 * the "unreadable ETag" safety net that hands assembly back to the backend instead of
 * shipping a client-built part list it cannot vouch for.
 */
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  post: vi.fn(),
}));

vi.mock('$lib/axios', async () => {
  const actual = await vi.importActual<typeof import('$lib/axios')>('$lib/axios');
  return { ...actual, default: mockInstance };
});

// H4a: part retries used to compute their own delay inline (500ms base, uncapped,
// unjittered) instead of going through the shared `backoff.ts` policy. Spy on the
// real implementation to prove the module now calls through it, with 1-based
// attempt numbers, rather than reimplementing the math locally.
const reconnectDelayMs = vi.hoisted(() => vi.fn());
vi.mock('$lib/utils/backoff', async () => {
  const actual = await vi.importActual<typeof import('$lib/utils/backoff')>('$lib/utils/backoff');
  reconnectDelayMs.mockImplementation(actual.reconnectDelayMs);
  return { ...actual, reconnectDelayMs };
});

import { uploadInParts, type MultipartPlan, type PutPart } from './multipartUploader';

/** A part-shaped stub: only `.size` is read by the module under test. */
function fakeBody(size: number) {
  return {
    size,
    slice: (start: number, end: number) => ({ size: end - start }),
  } as unknown as Blob;
}

function planFor(overrides: Partial<MultipartPlan> = {}): MultipartPlan {
  return {
    upload_id: 'upload-1',
    part_size: 10,
    part_count: 3,
    batch_size: 3,
    expires_in: 3600,
    urls: { 1: 'https://bucket/part1', 2: 'https://bucket/part2', 3: 'https://bucket/part3' },
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('uploadInParts — happy path', () => {
  it('sends every part exactly once, in order, without re-signing already-fresh URLs', async () => {
    const putPart = vi.fn().mockResolvedValue('etag-x');
    const onProgress = vi.fn();

    const result = await uploadInParts({
      fileId: 'file-1',
      body: fakeBody(25), // parts of 10, 10, 5
      plan: planFor(),
      putPart,
      onProgress,
    });

    expect(putPart).toHaveBeenCalledTimes(3);
    const urls = putPart.mock.calls.map((c) => c[0]).sort();
    expect(urls).toEqual(['https://bucket/part1', 'https://bucket/part2', 'https://bucket/part3']);
    expect(mockInstance.post).not.toHaveBeenCalled();

    expect(result.parts).toEqual([
      { part_number: 1, etag: 'etag-x' },
      { part_number: 2, etag: 'etag-x' },
      { part_number: 3, etag: 'etag-x' },
    ]);
  });

  it('reports the cumulative bytes loaded across all parts', async () => {
    const putPart = vi.fn(async (_url, chunk: Blob, onPartProgress) => {
      onPartProgress(chunk.size);
      return 'etag-x';
    });
    const onProgress = vi.fn();

    await uploadInParts({
      fileId: 'file-1',
      body: fakeBody(25),
      plan: planFor(),
      putPart,
      onProgress,
    });

    expect(onProgress).toHaveBeenCalledWith(25);
  });
});

describe('uploadInParts — resume', () => {
  it('skips a part only when the stored size matches what would be sent, and re-sends a truncated one', async () => {
    // Part 1 fully landed (size 10, matches). Part 2 reports a short write (5 of 10) — a
    // truncated upload the client must not trust, so it has to be re-sent from scratch.
    mockInstance.post.mockResolvedValueOnce({
      data: {
        urls: {},
        expires_in: 3600,
        uploaded_parts: [
          { part_number: 1, etag: 'resumed-etag-1', size: 10 },
          { part_number: 2, etag: 'stale-etag-2', size: 5 },
        ],
      },
    });
    const putPart = vi.fn().mockResolvedValue('fresh-etag');

    const result = await uploadInParts({
      fileId: 'file-1',
      body: fakeBody(25),
      plan: planFor(),
      resume: true,
      putPart,
      onProgress: vi.fn(),
    });

    // Only the unresolved/truncated parts (2 and 3) go over the wire again.
    expect(putPart).toHaveBeenCalledTimes(2);
    const sentUrls = putPart.mock.calls.map((c) => c[0]).sort();
    expect(sentUrls).toEqual(['https://bucket/part2', 'https://bucket/part3']);

    expect(result.parts).toEqual([
      { part_number: 1, etag: 'resumed-etag-1' },
      { part_number: 2, etag: 'fresh-etag' },
      { part_number: 3, etag: 'fresh-etag' },
    ]);

    // The resume probe asked for uploaded state, not fresh signed URLs.
    expect(mockInstance.post).toHaveBeenCalledWith('/files/multipart/parts', {
      file_id: 'file-1',
      upload_id: 'upload-1',
      part_numbers: [],
      include_uploaded: true,
    });
  });
});

describe('uploadInParts — retry and terminal errors', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('retries a part after a transient failure, re-signing the URL on the retry', async () => {
    mockInstance.post.mockResolvedValue({
      data: { urls: { 1: 'https://bucket/part1-refreshed' }, expires_in: 3600 },
    });
    let attempt = 0;
    const putPart: Mock<PutPart> = vi.fn(async (_url, _chunk, _onProgress) => {
      attempt += 1;
      if (attempt === 1) throw new Error('ECONNRESET');
      return 'etag-after-retry';
    });

    const promise = uploadInParts({
      fileId: 'file-1',
      body: fakeBody(10), // single part, so retry behavior is isolated
      plan: planFor({ part_count: 1, urls: { 1: 'https://bucket/part1' } }),
      putPart,
      onProgress: vi.fn(),
    });

    await vi.runAllTimersAsync();
    const result = await promise;

    expect(putPart).toHaveBeenCalledTimes(2);
    // The retry must not reuse the URL that just failed.
    expect(putPart.mock.calls[1][0]).toBe('https://bucket/part1-refreshed');
    expect(result.parts).toEqual([{ part_number: 1, etag: 'etag-after-retry' }]);

    // The retry delay comes from the shared backoff policy (H4a), not a local
    // reimplementation — called once, for the 1st retry.
    expect(reconnectDelayMs).toHaveBeenCalledTimes(1);
    expect(reconnectDelayMs).toHaveBeenCalledWith(1);
  });

  it('does not retry a cancellation — it propagates immediately', async () => {
    const cancelled = Object.assign(new Error('canceled'), { __CANCEL__: true });
    const putPart = vi.fn().mockRejectedValue(cancelled);

    await expect(
      uploadInParts({
        fileId: 'file-1',
        body: fakeBody(10),
        plan: planFor({ part_count: 1, urls: { 1: 'https://bucket/part1' } }),
        putPart,
        onProgress: vi.fn(),
      })
    ).rejects.toBe(cancelled);

    expect(putPart).toHaveBeenCalledTimes(1);
  });

  it('does not retry a stall — it propagates immediately without a second attempt', async () => {
    const stalled = Object.assign(new Error('stalled'), { name: 'UploadStalledError' });
    const putPart = vi.fn().mockRejectedValue(stalled);

    await expect(
      uploadInParts({
        fileId: 'file-1',
        body: fakeBody(10),
        plan: planFor({ part_count: 1, urls: { 1: 'https://bucket/part1' } }),
        putPart,
        onProgress: vi.fn(),
      })
    ).rejects.toBe(stalled);

    expect(putPart).toHaveBeenCalledTimes(1);
  });

  it('gives up after the retry ceiling and surfaces the last error', async () => {
    mockInstance.post.mockResolvedValue({
      data: { urls: { 1: 'https://bucket/part1-retry' }, expires_in: 3600 },
    });
    const persistent = new Error('always fails');
    const putPart = vi.fn().mockRejectedValue(persistent);

    const promise = uploadInParts({
      fileId: 'file-1',
      body: fakeBody(10),
      plan: planFor({ part_count: 1, urls: { 1: 'https://bucket/part1' } }),
      putPart,
      onProgress: vi.fn(),
    });
    promise.catch(() => {});

    await vi.runAllTimersAsync();
    await expect(promise).rejects.toBe(persistent);
    expect(putPart).toHaveBeenCalledTimes(3); // PART_MAX_ATTEMPTS
    // Two retries (after attempts 1 and 2; the 3rd attempt is the final failure
    // and schedules no further wait) via the shared backoff policy (H4a).
    expect(reconnectDelayMs).toHaveBeenNthCalledWith(1, 1);
    expect(reconnectDelayMs).toHaveBeenNthCalledWith(2, 2);
  });
});

describe('uploadInParts — concurrent re-signing dedup', () => {
  it('shares one signing POST across parts that need a fresh URL at the same time', async () => {
    // Part 1's URL is still fresh from the initial plan; parts 2 and 3 have none cached at
    // all (as if only the first batch came back from /prepare). PART_CONCURRENCY is 3, so
    // all three parts are picked up by workers in the same synchronous tick — parts 2 and 3
    // both need signing before any signing request has resolved, so the second one must
    // reuse the first one's in-flight `signing` promise instead of firing its own POST.
    mockInstance.post.mockResolvedValueOnce({
      data: {
        urls: { 2: 'https://bucket/part2-signed', 3: 'https://bucket/part3-signed' },
        expires_in: 3600,
      },
    });
    const putPart = vi.fn().mockResolvedValue('etag-x');

    const result = await uploadInParts({
      fileId: 'file-1',
      body: fakeBody(30), // 3 parts of 10
      plan: planFor({
        part_count: 3,
        batch_size: 2,
        urls: { 1: 'https://bucket/part1' }, // only part 1 pre-signed
      }),
      putPart,
      onProgress: vi.fn(),
    });

    // Exactly one signing call — batched for both parts that needed it concurrently,
    // not one call per part.
    expect(mockInstance.post).toHaveBeenCalledTimes(1);
    expect(mockInstance.post).toHaveBeenCalledWith('/files/multipart/parts', {
      file_id: 'file-1',
      upload_id: 'upload-1',
      part_numbers: [2, 3],
      include_uploaded: false,
    });

    const urlsUsed = putPart.mock.calls.map((c) => c[0]).sort();
    expect(urlsUsed).toEqual([
      'https://bucket/part1',
      'https://bucket/part2-signed',
      'https://bucket/part3-signed',
    ]);

    expect(result.parts).toEqual([
      { part_number: 1, etag: 'etag-x' },
      { part_number: 2, etag: 'etag-x' },
      { part_number: 3, etag: 'etag-x' },
    ]);
  });
});

describe('uploadInParts — unreadable ETag safety net', () => {
  it('returns parts: null instead of shipping a partial/unverifiable list', async () => {
    // One part's ETag header wasn't exposed cross-origin (bucket CORS config) — the
    // backend must read the authoritative list from storage instead of trusting this one.
    const putPart = vi
      .fn()
      .mockResolvedValueOnce('etag-1')
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce('etag-3');

    const result = await uploadInParts({
      fileId: 'file-1',
      body: fakeBody(25),
      plan: planFor(),
      putPart,
      onProgress: vi.fn(),
    });

    expect(result.parts).toBeNull();
  });
});
