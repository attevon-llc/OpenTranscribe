/**
 * Tests for the shared SHA-256 hasher (issue #302).
 *
 * There used to be two hashing paths: `uploadService` deliberately hashed in a Web Worker
 * to keep the UI responsive on multi-GB files, while `audioExtractionService` buffered the
 * whole file with `file.arrayBuffer()` and digested it on the main thread — on *video*
 * files, the largest inputs the app accepts. Both now call `hashFileSHA256`.
 *
 * Under jsdom there is no `Worker`, so these exercise the main-thread fallback path. That
 * fallback must produce byte-identical output to the worker, or a file hashed on one path
 * would not dedupe against the same file hashed on the other.
 */
import { describe, expect, it } from 'vitest';
import { hashFileSHA256 } from './sha256Hasher';

/**
 * Copy into a plain ArrayBuffer. `Uint8Array` is generic over `ArrayBufferLike` since
 * TS 5.7, so passing one straight to `crypto.subtle.digest` (which wants an
 * `ArrayBufferView<ArrayBuffer>`) fails to type-check on the SharedArrayBuffer arm.
 */
function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const out = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(out).set(bytes);
  return out;
}

/** Reference digest computed the same way the worker does. */
async function referenceHash(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', toArrayBuffer(bytes));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

describe('hashFileSHA256', () => {
  it('returns the known SHA-256 of an empty input', async () => {
    const hash = await hashFileSHA256(new Blob([]));
    expect(hash).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  });

  it('returns the known SHA-256 of "abc"', async () => {
    const hash = await hashFileSHA256(new Blob([new TextEncoder().encode('abc')]));
    expect(hash).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  });

  it('emits lowercase hex, zero-padded, 64 chars', async () => {
    const hash = await hashFileSHA256(new Blob([new Uint8Array([0, 1, 2, 3])]));
    expect(hash).toMatch(/^[0-9a-f]{64}$/);
  });

  it('matches a direct crypto.subtle digest of the same bytes', async () => {
    const bytes = new Uint8Array(4096).map((_, i) => i % 256);
    const file = new File([bytes], 'clip.mp4', { type: 'video/mp4' });

    expect(await hashFileSHA256(file)).toBe(await referenceHash(bytes));
  });

  it('is stable across repeated calls on equal content', async () => {
    const bytes = new Uint8Array(1024).fill(7);
    const a = await hashFileSHA256(new File([bytes], 'a.mp4', { type: 'video/mp4' }));
    const b = await hashFileSHA256(new File([bytes], 'b.mp4', { type: 'video/mp4' }));

    expect(a).toBe(b);
  });

  it('distinguishes content that differs by one byte', async () => {
    const a = new Uint8Array(512).fill(1);
    const b = new Uint8Array(512).fill(1);
    b[511] = 2;

    expect(await hashFileSHA256(new Blob([a]))).not.toBe(await hashFileSHA256(new Blob([b])));
  });

  it('handles concurrent requests without cross-talk', async () => {
    const inputs = [
      new Uint8Array(64).fill(1),
      new Uint8Array(64).fill(2),
      new Uint8Array(64).fill(3),
    ];

    const [viaHasher, viaReference] = await Promise.all([
      Promise.all(inputs.map((b) => hashFileSHA256(new Blob([b])))),
      Promise.all(inputs.map((b) => referenceHash(b))),
    ]);

    expect(viaHasher).toEqual(viaReference);
    expect(new Set(viaHasher).size).toBe(3);
  });
});
