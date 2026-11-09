import axios, { type AxiosProgressEvent, type CancelTokenSource } from 'axios';
import axiosInstance from '$lib/axios';
import type { StallWatchdog } from '$lib/services/stallWatchdog';
import {
  apiMediatedUploadAllowed,
  assertLegacyFallbackAllowed,
  duplicateFromConflict,
  isNonRetryableCompleteFailure,
  sendBody,
  transcriptionHeaders,
} from './transfer';
import { UploadStalledError, type UploadItem, type UploadResult } from './types';

export interface PresignedTransfer {
  upload: UploadItem;
  body: File | Blob;
  fileId: string;
  taskId: string | undefined;
  uploadUrl: string | undefined;
  uploadMethod: string | undefined;
  /** Fingerprint sent as `file_hash` (the source video's for extracted audio). */
  fingerprint: string | null;
  contentType: string;
  cancelToken: CancelTokenSource;
  progressHandler: (watchdog: StallWatchdog) => (event: AxiosProgressEvent) => void;
  /** Client-side hashing window, reported to `/files/complete` (main-file path only). */
  hashWindowMs?: { start: number; end: number };
  /** Filename for the legacy form part; omitted for the main-file path. */
  legacyFilename?: string;
}

/** Outcome of the direct PUT: null means "fall through to the legacy POST". */
async function tryPut(p: PresignedTransfer, uploadUrl: string) {
  const clientPutStartMs = Date.now();
  try {
    await sendBody((watchdog) =>
      axios.put(uploadUrl, p.body, {
        headers: { 'Content-Type': p.contentType },
        maxContentLength: Infinity,
        maxBodyLength: Infinity,
        cancelToken: p.cancelToken.token,
        signal: watchdog.signal,
        onUploadProgress: p.progressHandler(watchdog),
      })
    );
    return { clientPutStartMs, clientPutEndMs: Date.now() };
  } catch (err: unknown) {
    // A stalled connection is a network fault, not "the server can't do
    // presigned uploads" — re-sending the whole body through the API
    // container would stall too. Let the queue retry the presigned path.
    if (axios.isCancel(err) || err instanceof UploadStalledError) {
      throw err;
    }
    // No API-mediated fallback on this server: let the queue retry presigned.
    if (!apiMediatedUploadAllowed()) {
      throw err;
    }
    return null;
  }
}

/** The body already landed in storage; returns null to fall through to the legacy POST. */
async function tryComplete(
  p: PresignedTransfer,
  taskId: string,
  put: { clientPutStartMs: number; clientPutEndMs: number }
): Promise<UploadResult | null> {
  const { upload, body, fileId } = p;
  try {
    await axiosInstance.post('/files/complete', {
      file_id: fileId,
      task_id: taskId,
      file_hash: p.fingerprint,
      file_size: body.size,
      ...(p.hashWindowMs && {
        client_hash_start_ms: p.hashWindowMs.start,
        client_hash_end_ms: p.hashWindowMs.end,
      }),
      client_put_start_ms: put.clientPutStartMs,
      client_put_end_ms: put.clientPutEndMs,
      min_speakers: upload.minSpeakers ?? null,
      max_speakers: upload.maxSpeakers ?? null,
      num_speakers: upload.numSpeakers ?? null,
      skip_summary: upload.skipSummary || undefined,
    });
    return { uuid: fileId, isDuplicate: false };
  } catch (err: unknown) {
    if (axios.isCancel(err) || err instanceof UploadStalledError) {
      throw err;
    }
    // A deterministic dispatch refusal (e.g. ASRConfigurationError, 503) will
    // fail identically through the legacy path — don't pay for a full re-upload
    // to learn that twice (issues #905, #911).
    if (isNonRetryableCompleteFailure(err) || !apiMediatedUploadAllowed()) {
      throw err;
    }
    return null;
  }
}

/**
 * Single-PUT presigned upload, then the legacy `POST /files` fallback.
 *
 * Shared by the main-file and extracted-audio flows so the stall, 503 and
 * API-mediated-fallback rules cannot drift between them.
 */
export async function putThenCompleteOrFallback(p: PresignedTransfer): Promise<UploadResult> {
  const { taskId, uploadUrl, uploadMethod } = p;
  if (uploadUrl && uploadMethod === 'PUT' && taskId) {
    const put = await tryPut(p, uploadUrl);
    if (put) {
      const done = await tryComplete(p, taskId, put);
      if (done) return done;
    }
  }
  return legacyPost(p);
}

/** Legacy flow: multipart POST through the API container. */
async function legacyPost(p: PresignedTransfer): Promise<UploadResult> {
  assertLegacyFallbackAllowed();
  const formData = new FormData();
  if (p.legacyFilename) {
    formData.append('file', p.body, p.legacyFilename);
  } else {
    formData.append('file', p.body);
  }

  const headers: Record<string, string> = {
    'Content-Type': 'multipart/form-data',
    'X-File-ID': p.fileId,
    'X-File-Hash': p.fingerprint || '',
    ...transcriptionHeaders(p.upload),
  };

  try {
    await sendBody((watchdog) =>
      axiosInstance.post('/files', formData, {
        headers,
        maxContentLength: Infinity,
        maxBodyLength: Infinity,
        cancelToken: p.cancelToken.token,
        signal: watchdog.signal,
        onUploadProgress: p.progressHandler(watchdog),
      })
    );
  } catch (err: unknown) {
    const duplicate = duplicateFromConflict(err);
    if (duplicate) return duplicate;
    throw err;
  }

  return { uuid: p.fileId, isDuplicate: false };
}
