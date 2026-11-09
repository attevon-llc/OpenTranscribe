import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import { t } from '$stores/locale';
import type { CancelTokenSource } from 'axios';
import { fingerprintFile } from '$lib/services/fileFingerprint';
import { uploadInParts } from '$lib/services/multipartUploader';
import { makePutPart, reportBytes } from './transfer';
import {
  CONTROL_REQUEST_TIMEOUT_MS,
  type MultipartSession,
  type UploadHost,
  type UploadResult,
} from './types';

/**
 * Run a presigned multipart upload to completion.
 *
 * The session is parked on the `UploadItem` for the whole transfer so a queue
 * retry resumes it; it is cleared only once `/files/complete` has assembled
 * the object.
 */
export async function runMultipart(
  host: UploadHost,
  uploadId: string,
  file: File | Blob,
  session: MultipartSession,
  cancelToken: CancelTokenSource,
  resume: boolean
): Promise<UploadResult> {
  const upload = host.getItem(uploadId)!;
  host.update(uploadId, {
    status: 'uploading',
    fileId: session.fileId,
    multipart: session,
    statusText: get(t)(resume ? 'upload.resuming' : 'upload.statusUploading'),
  });

  const clientPutStartMs = Date.now();
  const { parts } = await uploadInParts({
    fileId: session.fileId,
    body: file,
    plan: session.plan,
    resume,
    putPart: makePutPart(cancelToken),
    onProgress: (loadedBytes) =>
      reportBytes(host, uploadId, loadedBytes, file.size, clientPutStartMs),
  });
  const clientPutEndMs = Date.now();

  await axiosInstance.post(
    '/files/complete',
    {
      file_id: session.fileId,
      task_id: session.taskId,
      file_hash: session.fingerprint,
      file_size: file.size,
      upload_id: session.plan.upload_id,
      parts,
      client_put_start_ms: clientPutStartMs,
      client_put_end_ms: clientPutEndMs,
      min_speakers: upload.minSpeakers ?? null,
      max_speakers: upload.maxSpeakers ?? null,
      num_speakers: upload.numSpeakers ?? null,
      skip_summary: upload.skipSummary || undefined,
    },
    // Assembling hundreds of parts is more than the 60 s default allows for.
    { timeout: CONTROL_REQUEST_TIMEOUT_MS }
  );

  host.update(uploadId, { multipart: undefined });
  return { uuid: session.fileId, isDuplicate: false };
}

/**
 * Resume a parked multipart session, or report that it is gone.
 *
 * Returns null when the backend no longer knows the upload (404/409), which
 * means starting over from `/prepare` is the only option. Any other failure
 * is rethrown so the queue retries the resume rather than re-sending
 * gigabytes that are already in the bucket.
 */
export async function resumeMultipart(
  host: UploadHost,
  uploadId: string,
  file: File | Blob,
  session: MultipartSession,
  cancelToken: CancelTokenSource
): Promise<UploadResult | null> {
  try {
    return await runMultipart(host, uploadId, file, session, cancelToken, true);
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status;
    if (status === 404 || status === 409) {
      host.update(uploadId, { multipart: undefined });
      return null;
    }
    throw err;
  }
}

/**
 * Discard a multipart upload the client has given up on.
 *
 * Object storage bills for the parts of an incomplete upload until it is
 * aborted, and they do not show up in a normal object listing. Fire-and-forget:
 * the bucket's abort-incomplete lifecycle rule is the backstop.
 */
export function abortMultipart(fileId: string) {
  axiosInstance.delete(`/files/${fileId}`).catch(() => {
    /* best-effort */
  });
}

/**
 * Fingerprint a source, reporting rather than swallowing a failure.
 *
 * The fingerprint is optional — an upload without one still succeeds — but a
 * missing one means this file was never checked against the library. That used
 * to happen silently on every file above ~4 GB (`file.arrayBuffer()` threw
 * `NotReadableError` and the `catch` was empty), so the largest uploads, where
 * a duplicate costs the most GPU time, were the only ones never deduplicated.
 * Reading 48 KiB should not fail, but if it does the user is told and the item
 * carries `dedupSkipped` for the queue UI.
 *
 * @returns The fingerprint, or null if it could not be computed.
 */
export async function fingerprintOrWarn(
  host: UploadHost,
  uploadId: string,
  source: File | Blob,
  name: string
): Promise<string | null> {
  try {
    return await fingerprintFile(source);
  } catch (err: unknown) {
    host.update(uploadId, { dedupSkipped: true });
    host.emit('progress', uploadId, { dedupSkipped: true });
    console.warn(`[upload] duplicate check skipped for "${name}":`, err);
    toastStore.warning(get(t)('upload.dedupSkipped', { name }));
    return null;
  }
}
