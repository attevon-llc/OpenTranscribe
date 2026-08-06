/**
 * Client-side content fingerprint for pre-upload duplicate detection (issue #342).
 *
 * This is a browser implementation of **imohash** — the same constant-time
 * fingerprint the backend already computes for every ingest path
 * (`backend/app/services/imohash_service.py`, the `imohash` PyPI package):
 * murmur3-x64-128 over the first / middle / last `SAMPLE_SIZE` windows, with the
 * low bytes of the digest replaced by a varint of the file size. Output is
 * byte-for-byte identical to the server's — `fileFingerprint.test.ts` pins the
 * vectors — so client and server share **one** definition of "same file" instead
 * of two that can disagree.
 *
 * Replaces the whole-file SHA-256 that used to live in `sha256Hasher.ts` /
 * `sha256Worker.ts`. That hashed via `file.arrayBuffer()`, which materialises the
 * entire file in memory: Chrome threw `NotReadableError` above ~4 GB, the error
 * was swallowed, and the upload silently proceeded with no duplicate detection —
 * against a UI that advertises a 15 GB limit. Here the read is bounded at
 * `3 × SAMPLE_SIZE` (48 KiB) no matter how large the file is, so the cost of
 * fingerprinting a 15 GB video equals that of a 1 MB one and memory never tracks
 * file size. No Web Worker is needed for 48 KiB.
 *
 * imohash is a **sampling** fingerprint and is NOT collision-resistant — do not
 * use it for security-sensitive equality (same caveat as the backend's).
 */

import murmur from 'murmurhash3js-revisited';

/** Bytes read from each of the head / middle / tail windows. */
export const SAMPLE_SIZE = 16 * 1024;

/** Files smaller than this are fingerprinted in full rather than sampled. */
export const SAMPLE_THRESHOLD = 128 * 1024;

/**
 * Raised when the source bytes could not be read (file moved or deleted after
 * selection, permission revoked, disk error).
 *
 * Callers must surface this — a fingerprint that silently fails to compute is
 * duplicate detection that silently stops working, which is the bug this module
 * exists to fix.
 */
export class FingerprintError extends Error {
  /** The underlying read failure (typically a `DOMException: NotReadableError`). */
  readonly reason: unknown;

  constructor(message: string, reason?: unknown) {
    super(message);
    this.name = 'FingerprintError';
    this.reason = reason;
  }
}

/**
 * Unsigned LEB128, matching Go/Python `varint.encode`.
 *
 * Deliberately arithmetic, not bitwise: JavaScript bitwise operators coerce to
 * int32, so `size & 0x7f` silently corrupts any file above 2 GiB — exactly the
 * range this module exists for.
 */
function encodeVarint(value: number): Uint8Array {
  const bytes: number[] = [];
  let remaining = value;
  while (remaining >= 0x80) {
    bytes.push((remaining % 0x80) + 0x80);
    remaining = Math.floor(remaining / 0x80);
  }
  bytes.push(remaining);
  return Uint8Array.from(bytes);
}

function toHex(bytes: Uint8Array): string {
  let out = '';
  for (const b of bytes) {
    out += b.toString(16).padStart(2, '0');
  }
  return out;
}

function fromHex(hex: string): Uint8Array {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

/** Read one bounded window. `Blob.slice` is lazy — only these bytes are loaded. */
async function readWindow(source: Blob, start: number, end: number): Promise<Uint8Array> {
  return new Uint8Array(await source.slice(start, end).arrayBuffer());
}

/**
 * Compute the imohash fingerprint of a file, reading at most 48 KiB of it.
 *
 * @param source - The file or blob to fingerprint.
 * @returns 32-char lowercase hex digest, identical to the backend's.
 * @throws {FingerprintError} If the underlying bytes cannot be read.
 */
export async function fingerprintFile(source: File | Blob): Promise<string> {
  const size = source.size;

  let sampled: Uint8Array;
  try {
    if (size < SAMPLE_THRESHOLD || size < 4 * SAMPLE_SIZE) {
      // Small enough that sampling would cover most of it anyway; the reference
      // implementation hashes these in full and we must match it exactly.
      sampled = await readWindow(source, 0, size);
    } else {
      const middle = Math.floor(size / 2);
      const [head, mid, tail] = await Promise.all([
        readWindow(source, 0, SAMPLE_SIZE),
        readWindow(source, middle, middle + SAMPLE_SIZE),
        readWindow(source, size - SAMPLE_SIZE, size),
      ]);
      sampled = new Uint8Array(3 * SAMPLE_SIZE);
      sampled.set(head, 0);
      sampled.set(mid, SAMPLE_SIZE);
      sampled.set(tail, 2 * SAMPLE_SIZE);
    }
  } catch (err) {
    throw new FingerprintError(
      err instanceof Error ? err.message : 'could not read file for fingerprinting',
      err
    );
  }

  const digest = fromHex(murmur.x64.hash128(sampled));
  const encodedSize = encodeVarint(size);
  const fingerprint = new Uint8Array(digest.length);
  fingerprint.set(encodedSize, 0);
  fingerprint.set(digest.subarray(encodedSize.length), encodedSize.length);
  return toHex(fingerprint);
}
