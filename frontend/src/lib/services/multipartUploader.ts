import axios from 'axios';
import axiosInstance from '$lib/axios';

/**
 * Executes a presigned multipart upload planned by the backend (issue #327).
 *
 * A per-upload factory rather than a singleton, like `stallWatchdog`. It owns no
 * policy: part size, part count and batch size all come from `/files/prepare`,
 * and the part bodies are sent by a `putPart` callback that `uploadService`
 * supplies so the stall watchdog and cancel token stay in one place.
 *
 * What it does own is the part pipeline — concurrency, per-part retry, fetching
 * the next batch of signed URLs before it is needed, and resuming from the parts
 * the bucket already holds.
 */

/** Upload plan returned by `/files/prepare` when the object needs multipart. */
export interface MultipartPlan {
  upload_id: string;
  part_size: number;
  part_count: number;
  batch_size: number;
  expires_in: number;
  urls: Record<string, string>;
}

/** A part the backend has confirmed, as reported to `/files/complete`. */
export interface CompletedPart {
  part_number: number;
  etag: string;
}

interface UploadedPartInfo {
  part_number: number;
  etag: string;
  size: number;
}

/** Sends one part body and resolves with its ETag, or null if unreadable. */
export type PutPart = (
  url: string,
  body: Blob,
  onProgress: (loaded: number) => void
) => Promise<string | null>;

export interface MultipartUploadOptions {
  /** MediaFile UUID from `/files/prepare` — authorizes the part signing. */
  fileId: string;
  body: Blob;
  plan: MultipartPlan;
  /** Ask storage which parts already landed before sending anything. */
  resume?: boolean;
  putPart: PutPart;
  onProgress: (loadedBytes: number) => void;
}

export interface MultipartUploadResult {
  /** Null when any ETag was unreadable — `/files/complete` reads them back instead. */
  parts: CompletedPart[] | null;
}

/** Parts in flight at once. Uploads themselves already run 3-wide. */
const PART_CONCURRENCY = 3;

/** Attempts per part before the whole upload fails and the queue retries it. */
const PART_MAX_ATTEMPTS = 3;

const PART_RETRY_BASE_DELAY_MS = 500;

/**
 * Treat a signed URL as spent this long before it actually lapses, so a part is
 * never started with a URL that will expire mid-transfer.
 */
const URL_EXPIRY_MARGIN_MS = 120000;

interface SignedUrl {
  url: string;
  expiresAt: number;
}

/** Client for the part-signing endpoint. */
async function requestParts(
  fileId: string,
  uploadId: string,
  partNumbers: number[],
  includeUploaded: boolean
): Promise<{
  urls: Record<string, string>;
  expires_in: number;
  uploaded_parts?: UploadedPartInfo[];
}> {
  const response = await axiosInstance.post('/files/multipart/parts', {
    file_id: fileId,
    upload_id: uploadId,
    part_numbers: partNumbers,
    include_uploaded: includeUploaded,
  });
  return response.data;
}

export async function uploadInParts(
  options: MultipartUploadOptions
): Promise<MultipartUploadResult> {
  const { fileId, body, plan, putPart, onProgress } = options;
  const partSize = plan.part_size;
  const partCount = plan.part_count;
  const batchSize = Math.max(1, plan.batch_size || 1);

  /** Byte length of a given 1-based part; the last one is short. */
  const sizeOfPart = (n: number) => Math.min(partSize, body.size - (n - 1) * partSize);

  const etags = new Map<number, string | null>();
  const loaded = new Map<number, number>();
  const urls = new Map<number, SignedUrl>();

  const seedUrls = (map: Record<string, string>, expiresIn: number) => {
    const expiresAt = Date.now() + Math.max(0, expiresIn) * 1000;
    for (const [number, url] of Object.entries(map || {})) {
      urls.set(Number(number), { url, expiresAt });
    }
  };
  seedUrls(plan.urls, plan.expires_in);

  const pending: number[] = [];
  for (let n = 1; n <= partCount; n++) pending.push(n);

  if (options.resume) {
    const state = await requestParts(fileId, plan.upload_id, [], true);
    for (const part of state.uploaded_parts || []) {
      // A part whose stored length differs from what we would send is a
      // truncated write; re-send it rather than assembling a corrupt object.
      if (part.size !== sizeOfPart(part.part_number)) continue;
      etags.set(part.part_number, part.etag);
      loaded.set(part.part_number, part.size);
      const index = pending.indexOf(part.part_number);
      if (index > -1) pending.splice(index, 1);
    }
  }

  const reportProgress = () => {
    let total = 0;
    for (const value of loaded.values()) total += value;
    onProgress(total);
  };
  reportProgress();

  let signing: Promise<void> | null = null;

  /**
   * Resolve a usable URL for a part, signing the next batch when the cache has
   * none or the cached one is close enough to expiry to be risky.
   *
   * Expiry is the reason URLs are minted a batch at a time: every presigned URL
   * is clamped to `PRESIGNED_URL_MAX_SECONDS` server-side, and a multi-gigabyte
   * upload can easily outlive that clamp.
   */
  const urlFor = async (part: number, forceRefresh: boolean): Promise<string> => {
    const cached = urls.get(part);
    if (!forceRefresh && cached && cached.expiresAt - URL_EXPIRY_MARGIN_MS > Date.now()) {
      return cached.url;
    }
    if (forceRefresh) urls.delete(part);

    while (signing) {
      await signing;
      const refreshed = urls.get(part);
      if (refreshed && refreshed.expiresAt - URL_EXPIRY_MARGIN_MS > Date.now()) {
        return refreshed.url;
      }
    }

    const wanted = [part];
    for (const candidate of pending) {
      if (wanted.length >= batchSize) break;
      if (candidate !== part && !urls.has(candidate)) wanted.push(candidate);
    }
    signing = (async () => {
      const batch = await requestParts(
        fileId,
        plan.upload_id,
        wanted.sort((a, b) => a - b),
        false
      );
      seedUrls(batch.urls, batch.expires_in);
    })();
    try {
      await signing;
    } finally {
      signing = null;
    }

    const signed = urls.get(part);
    if (!signed) throw new Error(`No signed URL for part ${part}`);
    return signed.url;
  };

  const sendPart = async (part: number) => {
    const start = (part - 1) * partSize;
    const chunk = body.slice(start, start + sizeOfPart(part));

    let lastError: unknown;
    for (let attempt = 0; attempt < PART_MAX_ATTEMPTS; attempt++) {
      try {
        // Re-sign from the second attempt on: an expired URL is the most likely
        // reason a part that was fine a moment ago suddenly 403s.
        const url = await urlFor(part, attempt > 0);
        const etag = await putPart(url, chunk, (bytes) => {
          loaded.set(part, bytes);
          reportProgress();
        });
        etags.set(part, etag);
        loaded.set(part, chunk.size);
        reportProgress();
        return;
      } catch (error: unknown) {
        lastError = error;
        loaded.set(part, 0);
        reportProgress();
        // Cancellation and stalls are the caller's to interpret — a stalled
        // link will stall the retry too, and the queue owns that decision.
        if (isTerminal(error)) throw error;
        if (attempt < PART_MAX_ATTEMPTS - 1) {
          await delay(PART_RETRY_BASE_DELAY_MS * Math.pow(2, attempt));
        }
      }
    }
    throw lastError;
  };

  const workers: Promise<void>[] = [];
  for (let i = 0; i < Math.min(PART_CONCURRENCY, pending.length); i++) {
    workers.push(
      (async () => {
        for (;;) {
          const part = pending.shift();
          if (part === undefined) return;
          await sendPart(part);
        }
      })()
    );
  }
  await Promise.all(workers);

  const collected: CompletedPart[] = [];
  for (let n = 1; n <= partCount; n++) {
    const etag = etags.get(n);
    // One unreadable ETag (a bucket that does not expose the header
    // cross-origin) makes the whole client-side list useless; the backend
    // reads the authoritative list from storage instead.
    if (!etag) return { parts: null };
    collected.push({ part_number: n, etag });
  }
  return { parts: collected };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Errors a part-level retry cannot help with.
 *
 * `UploadStalledError` is matched by name rather than `instanceof`: it is
 * declared in `uploadService`, which imports this module, and importing it back
 * would make the cycle real.
 */
function isTerminal(error: unknown): boolean {
  return axios.isCancel(error) || (error as { name?: string })?.name === 'UploadStalledError';
}
