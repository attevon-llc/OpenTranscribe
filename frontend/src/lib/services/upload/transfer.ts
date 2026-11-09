import { get } from 'svelte/store';
import axios, { type AxiosProgressEvent, type CancelTokenSource } from 'axios';
import { t } from '$stores/locale';
import { capabilities } from '$stores/capabilities';
import {
  createStallWatchdog,
  DEFAULT_STALL_TIMEOUT_MS,
  type StallWatchdog,
} from '$lib/services/stallWatchdog';
import type { PutPart } from '$lib/services/multipartUploader';
import { formatTimeRemaining } from './format';
import { UploadStalledError, type UploadHost, type UploadItem, type UploadResult } from './types';

/**
 * Run a whole-file body transfer under a stall watchdog.
 *
 * Replaces the total-request `timeout` that used to guard these calls: that
 * capped how long a *healthy* upload was allowed to take, which is exactly
 * wrong for a 15 GB limit. The watchdog aborts only when the byte stream goes
 * quiet, and its abort is rethrown as `UploadStalledError` so the caller can
 * tell it apart from a user cancellation.
 *
 * @param run - Issues the request; must pass `watchdog.signal` to axios and
 *   feed `watchdog` from `onUploadProgress`.
 */
export async function sendBody<T>(run: (watchdog: StallWatchdog) => Promise<T>): Promise<T> {
  const watchdog = createStallWatchdog();
  try {
    return await run(watchdog);
  } catch (err: unknown) {
    if (watchdog.stalled) {
      throw new UploadStalledError(
        get(t)('upload.stalled', { seconds: Math.round(DEFAULT_STALL_TIMEOUT_MS / 1000) })
      );
    }
    throw err;
  } finally {
    watchdog.dispose();
  }
}

/**
 * The per-file choices as legacy-multipart headers. The presigned flow carries the same
 * values in the `/files/complete` body; this is its twin for `POST /files`, shared so the
 * main-file and extracted-audio fallbacks cannot drift apart again.
 */
export function transcriptionHeaders(upload: UploadItem): Record<string, string> {
  const headers: Record<string, string> = {};
  if (upload.minSpeakers !== null && upload.minSpeakers !== undefined) {
    headers['X-Min-Speakers'] = upload.minSpeakers.toString();
  }
  if (upload.maxSpeakers !== null && upload.maxSpeakers !== undefined) {
    headers['X-Max-Speakers'] = upload.maxSpeakers.toString();
  }
  if (upload.numSpeakers !== null && upload.numSpeakers !== undefined) {
    headers['X-Num-Speakers'] = upload.numSpeakers.toString();
  }
  if (upload.skipSummary) {
    headers['X-Skip-Summary'] = 'true';
  }
  return headers;
}

/**
 * Read a server-side duplicate rejection out of a failed `POST /files`.
 *
 * `prepare` normally catches a duplicate before any body is sent (`is_duplicate`),
 * but it cannot catch every one: the same content can be uploaded between prepare
 * and the POST, and a file whose fingerprint was skipped for the pre-check still
 * carries one in the form data. For those, the backend re-checks and answers
 * `409 {detail: {message, duplicate_file_uuid}}` (`api/endpoints/files/upload.py`) —
 * a correct, deliberate refusal that pointed at the file the user already has.
 * Left unhandled it reached the generic error path and was reported as a failed
 * upload, which describes the opposite of what happened.
 *
 * @returns The duplicate result, or `null` if this is not a duplicate rejection —
 *   a 409 without the uuid is some other conflict and must still fail, or a real
 *   error would be reported as a file safely stored.
 */
export function duplicateFromConflict(err: unknown): UploadResult | null {
  const response = (
    err as {
      response?: { status?: number; data?: { detail?: { duplicate_file_uuid?: string } } };
    }
  )?.response;
  if (response?.status !== 409) return null;
  const uuid = response.data?.detail?.duplicate_file_uuid;
  return uuid ? { uuid, isDuplicate: true } : null;
}

/**
 * Whether this server accepts the API-mediated `POST /files` fallback. When it does
 * not, a failed presigned attempt is rethrown so the queue retries the presigned
 * path, instead of re-sending the body to a route that will refuse it.
 */
export function apiMediatedUploadAllowed(): boolean {
  return get(capabilities).apiMediatedUploadEnabled !== false;
}

/**
 * The legacy `POST /files` is about to be used. With the fallback disabled that can
 * only mean the server handed out no presigned plan, which it does not do in that
 * mode (it answers 503 instead) — fail retryably rather than send the body.
 */
export function assertLegacyFallbackAllowed(): void {
  if (!apiMediatedUploadAllowed()) {
    throw new Error('Direct upload is unavailable right now; the upload will be retried.');
  }
}

/**
 * Detect a deterministic, non-retryable failure from `POST /files/complete`.
 *
 * A 503 here is `main.py`'s global `OpenTranscribeError` handler surfacing
 * `ASRConfigurationError` (`backend/app/core/exceptions.py`, issue #905's fix): the
 * backend has definitively decided it cannot dispatch this upload under the current
 * deployment config (e.g. a `lite` image with no local ASR provider). Retrying via
 * the legacy `POST /files` re-upload would hit the exact same refusal, at the cost of
 * re-sending the whole file body through the API container for nothing (issue #911).
 *
 * A network-level failure (stall, dropped connection, no response at all) carries no
 * `response.status` and is unaffected — those stay eligible for the legacy fallback,
 * same as before this check existed.
 */
export function isNonRetryableCompleteFailure(err: unknown): boolean {
  const status = (err as { response?: { status?: number } })?.response?.status;
  return status === 503;
}

/**
 * Build the axios progress callback for one upload: updates percentage + ETA
 * and keeps the stall watchdog fed.
 */
export function makeProgressHandler(host: UploadHost, uploadId: string, upload: UploadItem) {
  return (watchdog: StallWatchdog) => (progressEvent: AxiosProgressEvent) => {
    watchdog.notifyProgress(progressEvent.loaded, progressEvent.total);
    if (!progressEvent.total) return;
    const percentCompleted = Math.round((progressEvent.loaded * 100) / progressEvent.total);
    const progress = Math.min(percentCompleted, 99);
    host.update(uploadId, { progress });
    host.emit('progress', uploadId, { progress });
    const elapsed = Date.now() - (upload.startTime || Date.now());
    if (elapsed > 0) {
      const rate = progressEvent.loaded / elapsed;
      const remaining = (progressEvent.total - progressEvent.loaded) / rate;
      // A real ETA supersedes the phase text; keep the phase text while
      // `formatTimeRemaining` still returns '' (too early to estimate) so the
      // row never goes blank.
      const eta = formatTimeRemaining(remaining);
      host.update(uploadId, eta ? { estimatedTime: eta, statusText: undefined } : {});
    }
  };
}

/**
 * Send one multipart part body under the shared stall watchdog.
 *
 * Resolves with the part's ETag, or null when the header is unreadable — a
 * bucket that does not expose `ETag` cross-origin. `/files/complete` then
 * reads the authoritative part list back from storage instead.
 */
export function makePutPart(cancelToken: CancelTokenSource): PutPart {
  return (url, body, onProgress) =>
    sendBody(async (watchdog) => {
      const response = await axios.put(url, body, {
        maxContentLength: Infinity,
        maxBodyLength: Infinity,
        cancelToken: cancelToken.token,
        signal: watchdog.signal,
        onUploadProgress: (progressEvent: AxiosProgressEvent) => {
          watchdog.notifyProgress(progressEvent.loaded, progressEvent.total);
          onProgress(progressEvent.loaded);
        },
      });
      const etag = response.headers?.etag ?? response.headers?.ETag;
      return etag ? String(etag).replace(/"/g, '') : null;
    });
}

/** Percentage + ETA from a running byte count (the multipart equivalent of `makeProgressHandler`). */
export function reportBytes(
  host: UploadHost,
  uploadId: string,
  loaded: number,
  total: number,
  startedAt: number
) {
  if (!total) return;
  const progress = Math.min(Math.round((loaded * 100) / total), 99);
  host.update(uploadId, { progress });
  host.emit('progress', uploadId, { progress });
  const elapsed = Date.now() - startedAt;
  if (elapsed > 0 && loaded > 0) {
    const remaining = ((total - loaded) * elapsed) / loaded;
    const eta = formatTimeRemaining(remaining);
    if (eta) host.update(uploadId, { estimatedTime: eta, statusText: undefined });
  }
}
