/**
 * Tests for the client-side imohash fingerprint (issue #342).
 *
 * Every `expect(...).toBe('…')` below is a vector produced by the **backend's own**
 * hasher — the `imohash` PyPI package driven through
 * `app/services/imohash_service.py`'s parameters (`sample_threshhold=128 KiB`,
 * `sample_size=16 KiB`). If this file ever goes red, the browser and the server
 * have stopped agreeing on what "same file" means and duplicate detection is
 * broken in one direction or the other.
 *
 * Regenerate with:
 *   backend/venv/bin/python -c "import io; from imohash import hashfileobject; \
 *     print(hashfileobject(io.BytesIO(b'abc'), sample_threshhold=128*1024, \
 *     sample_size=16*1024, hexdigest=True))"
 */
import { describe, expect, it, vi } from 'vitest';
import { fingerprintFile, FingerprintError, SAMPLE_SIZE } from './fileFingerprint';

/**
 * `(i * 31 + 7) % 256` — the pattern the Python vectors below were generated from.
 *
 * Returns a plain `ArrayBuffer`: `Uint8Array` is generic over `ArrayBufferLike`
 * since TS 5.7, so handing one straight to the `Blob` constructor fails to
 * type-check on the `SharedArrayBuffer` arm.
 */
function pattern(length: number): ArrayBuffer {
  const buffer = new ArrayBuffer(length);
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < length; i++) {
    bytes[i] = (i * 31 + 7) % 256;
  }
  return buffer;
}

/**
 * A zero-filled blob that reports `size` but only ever materialises the windows
 * asked for — the browser equivalent of a sparse file.
 *
 * Multi-gigabyte inputs are the whole point of this module and cannot be
 * allocated in a test process, so this stands in for one. The vectors it is
 * checked against come from real sparse files of the same sizes on disk.
 */
function sparseZeroBlob(size: number): Blob {
  return {
    size,
    slice: (start: number, end: number) => new Blob([new ArrayBuffer(end - start)]),
  } as unknown as Blob;
}

describe('fingerprintFile', () => {
  it('matches the backend for an empty input', async () => {
    expect(await fingerprintFile(new Blob([]))).toBe('00000000000000000000000000000000');
  });

  it('matches the backend for a tiny input', async () => {
    const abc = new Blob([new TextEncoder().encode('abc')]);
    expect(await fingerprintFile(abc)).toBe('03963f3f3fad78673ba2744126ca2d52');
  });

  it('matches the backend below the sample threshold (whole-file path)', async () => {
    const buffer = new ArrayBuffer(4096);
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < bytes.length; i++) bytes[i] = i % 256;
    expect(await fingerprintFile(new Blob([buffer]))).toBe('8020a803a564957a836898c60fbb77bb');
  });

  it('matches the backend exactly at the sample threshold', async () => {
    expect(await fingerprintFile(new Blob([pattern(128 * 1024)]))).toBe(
      '80800833394f6067f0a5e566b8d64210'
    );
  });

  it('matches the backend above the sample threshold (sampled path)', async () => {
    expect(await fingerprintFile(new Blob([pattern(200 * 1024)]))).toBe(
      '80c00c33394f6067f0a5e566b8d64210'
    );
    expect(await fingerprintFile(new Blob([pattern(8 * 1024 * 1024)]))).toBe(
      '80808004394f6067f0a5e566b8d64210'
    );
  });

  it('encodes sizes past the 32-bit boundary — the range that broke SHA-256', async () => {
    // Vectors from real sparse files: `truncate -s <size> f && imosum f`.
    // Bitwise varint encoding would silently corrupt both of these.
    expect(await fingerprintFile(sparseZeroBlob(6442450944))).toBe(
      '8080808018f5f56d948936e07fad6ae3'
    );
    expect(await fingerprintFile(sparseZeroBlob(5000000000))).toBe(
      '80e497d012f5f56d948936e07fad6ae3'
    );
  });

  it('reads a bounded 48 KiB regardless of file size', async () => {
    let bytesRead = 0;
    const huge = {
      size: 15 * 1024 * 1024 * 1024,
      slice: (start: number, end: number) => {
        bytesRead += end - start;
        return new Blob([new ArrayBuffer(end - start)]);
      },
    } as unknown as Blob;

    await fingerprintFile(huge);

    expect(bytesRead).toBe(3 * SAMPLE_SIZE);
  });

  it('emits 32 lowercase hex chars', async () => {
    expect(await fingerprintFile(new Blob([pattern(300 * 1024)]))).toMatch(/^[0-9a-f]{32}$/);
  });

  it('is stable across repeated calls on equal content', async () => {
    const bytes = pattern(300 * 1024);
    const a = await fingerprintFile(new File([bytes], 'a.mp4', { type: 'video/mp4' }));
    const b = await fingerprintFile(new File([bytes], 'b.mp4', { type: 'video/mp4' }));
    expect(a).toBe(b);
  });

  it('distinguishes content that differs inside a sampled window', async () => {
    const a = pattern(300 * 1024);
    const b = pattern(300 * 1024);
    const bBytes = new Uint8Array(b);
    bBytes[bBytes.length - 1] ^= 0xff;
    expect(await fingerprintFile(new Blob([a]))).not.toBe(await fingerprintFile(new Blob([b])));
  });

  it('distinguishes files of different size with identical samples', async () => {
    expect(await fingerprintFile(sparseZeroBlob(6442450944))).not.toBe(
      await fingerprintFile(sparseZeroBlob(5000000000))
    );
  });

  it('throws FingerprintError when the bytes cannot be read', async () => {
    const unreadable = {
      size: 10 * 1024 * 1024 * 1024,
      slice: () => ({
        arrayBuffer: () => Promise.reject(new DOMException('The file could not be read')),
      }),
    } as unknown as Blob;

    await expect(fingerprintFile(unreadable)).rejects.toBeInstanceOf(FingerprintError);
  });

  it('does not swallow a read failure into a fake fingerprint', async () => {
    const spy = vi.fn();
    const unreadable = {
      size: 1024,
      slice: () => ({ arrayBuffer: () => Promise.reject(new Error('NotReadableError')) }),
    } as unknown as Blob;

    await fingerprintFile(unreadable).catch(spy);

    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0][0]).toBeInstanceOf(FingerprintError);
  });
});
