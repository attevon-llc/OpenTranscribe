import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { authStore } from '$stores/auth';
import { toastStore } from '$stores/toast';
import { t } from '$stores/locale';
import axios, { type AxiosProgressEvent, type CancelTokenSource } from 'axios';
import type { ExtractedAudioMetadata } from '$lib/types/audioExtraction';
import { generateId } from '$lib/utils/ids';
import { fingerprintFile } from '$lib/services/fileFingerprint';
import {
  createStallWatchdog,
  DEFAULT_STALL_TIMEOUT_MS,
  type StallWatchdog,
} from '$lib/services/stallWatchdog';
import { uploadInParts, type MultipartPlan, type PutPart } from '$lib/services/multipartUploader';

// Upload item types
type UploadType = 'file' | 'url' | 'recording' | 'extracted-audio';
type UploadStatus =
  | 'queued'
  | 'preparing'
  | 'uploading'
  | 'processing'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface UploadItem {
  id: string;
  type: UploadType;
  source: File | string | Blob;
  name: string;
  size?: number;
  status: UploadStatus;
  progress: number;
  error?: string;
  fileId?: string; // UUID
  retryCount: number;
  startTime?: number;
  estimatedTime?: string;
  isDuplicate?: boolean;
  /**
   * The content fingerprint could not be computed, so this upload was NOT
   * checked against the library. Rendered by `UploadProgress.svelte` — a
   * degraded duplicate check has to be visible, not swallowed (issue #342).
   */
  dedupSkipped?: boolean;
  cancelToken?: CancelTokenSource;
  // Extraction metadata (for extracted-audio type)
  extractionMetadata?: ExtractedAudioMetadata;
  compressionRatio?: number; // Percentage for display (0-100)
  // Speaker diarization parameters
  minSpeakers?: number | null;
  maxSpeakers?: number | null;
  numSpeakers?: number | null;
  // Organization parameters
  collectionIds?: string[];
  tagNames?: string[];
  // Batch grouping
  uploadBatchId?: string;
  // Live multipart upload, kept so a retry resumes instead of restarting
  multipart?: MultipartSession;
}

/**
 * Everything needed to pick an interrupted multipart upload back up.
 *
 * Survives on the in-memory `UploadItem` only: the File it refers to cannot be
 * persisted (see `loadPersistedUploads`), so resume covers retries within the
 * session — which is where a 10 GB upload actually dies — not a page reload.
 */
interface MultipartSession {
  fileId: string;
  taskId: string;
  fingerprint: string | null;
  plan: MultipartPlan;
}

// Upload configuration constants
const MAX_RETRIES = 3;
const RETRY_BASE_DELAY_MS = 1000;
const MAX_CONCURRENT_UPLOADS = 3;
const QUEUE_PROCESS_DELAY_MS = 100;

// Total-request timeout for the small JSON control-plane calls only (prepare,
// complete, process-url). File bodies must NEVER use a total-request timeout —
// see the note on UploadStalledError below.
const CONTROL_REQUEST_TIMEOUT_MS = 300000; // 5 minutes

// Event types for upload lifecycle
type UploadEventType =
  | 'added'
  | 'started'
  | 'progress'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'retry';

export interface UploadEvent {
  type: UploadEventType;
  uploadId: string;
  data?: unknown;
}

/** Result of a single upload/prepare flow. */
interface UploadResult {
  uuid: string;
  isDuplicate: boolean;
}

/**
 * Raised when the stall watchdog aborts a body transfer.
 *
 * File bodies used to carry a 5-minute *total-request* timeout while the UI
 * advertises a 15 GB limit, so every upload slower than ~50 MB/s failed on the
 * clock alone — then burned all 3 retries doing it again. The timeout is now a
 * stall watchdog (abort only when no bytes have moved), and its abort surfaces
 * as this type rather than an axios `CanceledError`, because `axios.isCancel`
 * means "the user cancelled" everywhere else in this file and suppresses retry.
 */
class UploadStalledError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UploadStalledError';
  }
}

class UploadService {
  private uploads: Map<string, UploadItem> = new Map();
  private eventListeners: ((event: UploadEvent) => void)[] = [];
  private processingQueue: string[] = [];
  private activeUploads: Set<string> = new Set();

  constructor() {
    this.loadPersistedUploads();
  }

  // Event system
  addEventListener(listener: (event: UploadEvent) => void) {
    this.eventListeners.push(listener);
    return () => {
      const index = this.eventListeners.indexOf(listener);
      if (index > -1) {
        this.eventListeners.splice(index, 1);
      }
    };
  }

  private emit(type: UploadEventType, uploadId: string, data?: unknown) {
    const event: UploadEvent = { type, uploadId, data };
    this.eventListeners.forEach((listener) => listener(event));
  }

  // Queue management
  addUpload(
    type: UploadType,
    source: File | string | Blob,
    name?: string,
    speakerParams?: {
      minSpeakers?: number | null;
      maxSpeakers?: number | null;
      numSpeakers?: number | null;
    },
    collectionIds?: string[],
    tagNames?: string[],
    uploadBatchId?: string
  ): string {
    const id = this.generateId();
    const uploadName = name || this.getSourceName(source);

    const upload: UploadItem = {
      id,
      type,
      source,
      name: uploadName,
      size: source instanceof File ? source.size : undefined,
      status: 'queued',
      progress: 0,
      retryCount: 0,
      minSpeakers: speakerParams?.minSpeakers,
      maxSpeakers: speakerParams?.maxSpeakers,
      numSpeakers: speakerParams?.numSpeakers,
      collectionIds,
      tagNames,
      uploadBatchId,
    };

    this.uploads.set(id, upload);
    this.processingQueue.push(id);
    this.persistUploads();

    this.emit('added', id, upload);

    // Start processing if we have capacity
    this.processQueue();

    return id;
  }

  addMultipleFiles(files: File[], collectionIds?: string[], tagNames?: string[]): string[] {
    const uploadIds: string[] = [];

    // Generate a shared batch UUID when uploading 2+ files together
    // so they are linked as a batch for downstream topic grouping
    const batchId = files.length >= 2 ? generateId() : undefined;

    files.forEach((file) => {
      const id = this.addUpload(
        'file',
        file,
        undefined,
        undefined,
        collectionIds,
        tagNames,
        batchId
      );
      uploadIds.push(id);
    });

    return uploadIds;
  }

  addExtractedAudio(
    audioBlob: Blob,
    filename: string,
    extractionMetadata: ExtractedAudioMetadata,
    compressionRatio: number
  ): string {
    const id = this.generateId();

    const upload: UploadItem = {
      id,
      type: 'extracted-audio',
      source: audioBlob,
      name: filename,
      size: audioBlob.size,
      status: 'queued',
      progress: 0,
      retryCount: 0,
      extractionMetadata,
      compressionRatio,
    };

    this.uploads.set(id, upload);
    this.processingQueue.push(id);
    this.persistUploads();

    this.emit('added', id, upload);

    // Start processing if we have capacity
    this.processQueue();

    return id;
  }

  // Process upload queue
  private async processQueue() {
    if (this.activeUploads.size >= MAX_CONCURRENT_UPLOADS) {
      return;
    }

    const nextUploadId = this.processingQueue.find(
      (id) => !this.activeUploads.has(id) && this.uploads.get(id)?.status === 'queued'
    );

    if (!nextUploadId) {
      return;
    }

    this.activeUploads.add(nextUploadId);
    const queueIndex = this.processingQueue.indexOf(nextUploadId);
    if (queueIndex > -1) {
      this.processingQueue.splice(queueIndex, 1);
    }

    try {
      await this.processUpload(nextUploadId);
    } catch (error) {
      // Error is handled in processUpload method
    } finally {
      this.activeUploads.delete(nextUploadId);
      // Continue processing queue
      setTimeout(() => this.processQueue(), QUEUE_PROCESS_DELAY_MS);
    }
  }

  private async processUpload(uploadId: string) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;

    try {
      // Set status to preparing with 0% progress
      this.updateUpload(uploadId, {
        status: 'preparing',
        startTime: Date.now(),
        progress: 0,
        estimatedTime: undefined,
      });
      this.emit('started', uploadId);

      let result;
      switch (upload.type) {
        case 'file':
        case 'recording':
          result = await this.uploadFile(uploadId, upload.source as File);
          break;
        case 'extracted-audio':
          result = await this.uploadExtractedAudio(
            uploadId,
            upload.source as Blob,
            upload.extractionMetadata as ExtractedAudioMetadata
          );
          break;
        case 'url':
          result = await this.processUrl(uploadId, upload.source as string);
          break;
        default:
          throw new Error(get(t)('upload.unknownType', { type: upload.type }));
      }

      // Upload completed successfully - show 100% with green checkmark
      this.updateUpload(uploadId, {
        status: 'completed',
        progress: 100,
        fileId: result.uuid,
        isDuplicate: result.isDuplicate,
        estimatedTime: undefined,
      });

      this.emit('completed', uploadId, result);

      // Show appropriate toast based on duplicate status
      if (result.isDuplicate) {
        toastStore.warning(get(t)('upload.fileAlreadyExists', { name: upload.name }));
      } else {
        toastStore.success(get(t)('upload.uploadCompleted', { name: upload.name }));
      }
    } catch (error: unknown) {
      // Log error through proper error handling below

      const errorMessage = this.getErrorMessage(error);
      this.updateUpload(uploadId, {
        status: 'failed',
        error: errorMessage,
      });

      this.emit('failed', uploadId, { error: errorMessage });

      // Handle retry logic
      if (upload.retryCount < MAX_RETRIES && !axios.isCancel(error)) {
        setTimeout(
          () => {
            this.retryUpload(uploadId);
          },
          RETRY_BASE_DELAY_MS * Math.pow(2, upload.retryCount)
        );
      } else {
        // Out of retries: release the parts of an unfinished multipart upload
        // rather than leaving the user paying for gigabytes nothing will claim.
        if (upload.multipart) {
          this.abortMultipart(upload.multipart.fileId);
          this.updateUpload(uploadId, { multipart: undefined });
        }
        toastStore.error(
          get(t)('upload.uploadFailed', {
            name: upload.name,
            error: errorMessage,
          })
        );
      }
    }

    this.persistUploads();
  }

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
  private async sendBody<T>(run: (watchdog: StallWatchdog) => Promise<T>): Promise<T> {
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
   * Build the axios progress callback for one upload: updates percentage + ETA
   * and keeps the stall watchdog fed.
   */
  private makeProgressHandler(uploadId: string, upload: UploadItem) {
    return (watchdog: StallWatchdog) => (progressEvent: AxiosProgressEvent) => {
      watchdog.notifyProgress(progressEvent.loaded, progressEvent.total);
      if (!progressEvent.total) return;
      const percentCompleted = Math.round((progressEvent.loaded * 100) / progressEvent.total);
      const progress = Math.min(percentCompleted, 99);
      this.updateUpload(uploadId, { progress });
      this.emit('progress', uploadId, { progress });
      const elapsed = Date.now() - (upload.startTime || Date.now());
      if (elapsed > 0) {
        const rate = progressEvent.loaded / elapsed;
        const remaining = (progressEvent.total - progressEvent.loaded) / rate;
        this.updateUpload(uploadId, { estimatedTime: this.formatTimeRemaining(remaining) });
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
  private makePutPart(cancelToken: CancelTokenSource): PutPart {
    return (url, body, onProgress) =>
      this.sendBody(async (watchdog) => {
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
  private reportBytes(uploadId: string, loaded: number, total: number, startedAt: number) {
    if (!total) return;
    const progress = Math.min(Math.round((loaded * 100) / total), 99);
    this.updateUpload(uploadId, { progress });
    this.emit('progress', uploadId, { progress });
    const elapsed = Date.now() - startedAt;
    if (elapsed > 0 && loaded > 0) {
      const remaining = ((total - loaded) * elapsed) / loaded;
      this.updateUpload(uploadId, { estimatedTime: this.formatTimeRemaining(remaining) });
    }
  }

  /**
   * Run a presigned multipart upload to completion.
   *
   * The session is parked on the `UploadItem` for the whole transfer so a queue
   * retry resumes it; it is cleared only once `/files/complete` has assembled
   * the object.
   */
  private async runMultipart(
    uploadId: string,
    file: File | Blob,
    session: MultipartSession,
    cancelToken: CancelTokenSource,
    resume: boolean
  ): Promise<UploadResult> {
    const upload = this.uploads.get(uploadId)!;
    this.updateUpload(uploadId, {
      status: 'uploading',
      fileId: session.fileId,
      multipart: session,
      estimatedTime: get(t)(resume ? 'upload.resuming' : 'upload.statusUploading'),
    });

    const clientPutStartMs = Date.now();
    const { parts } = await uploadInParts({
      fileId: session.fileId,
      body: file,
      plan: session.plan,
      resume,
      putPart: this.makePutPart(cancelToken),
      onProgress: (loadedBytes) =>
        this.reportBytes(uploadId, loadedBytes, file.size, clientPutStartMs),
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
      },
      // Assembling hundreds of parts is more than the 60 s default allows for.
      { timeout: CONTROL_REQUEST_TIMEOUT_MS }
    );

    this.updateUpload(uploadId, { multipart: undefined });
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
  private async resumeMultipart(
    uploadId: string,
    file: File | Blob,
    session: MultipartSession,
    cancelToken: CancelTokenSource
  ): Promise<UploadResult | null> {
    try {
      return await this.runMultipart(uploadId, file, session, cancelToken, true);
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status;
      if (status === 404 || status === 409) {
        this.updateUpload(uploadId, { multipart: undefined });
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
  private abortMultipart(fileId: string) {
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
  private async fingerprintOrWarn(
    uploadId: string,
    source: File | Blob,
    name: string
  ): Promise<string | null> {
    try {
      return await fingerprintFile(source);
    } catch (err: unknown) {
      this.updateUpload(uploadId, { dedupSkipped: true });
      this.emit('progress', uploadId, { dedupSkipped: true });
      console.warn(`[upload] duplicate check skipped for "${name}":`, err);
      toastStore.warning(get(t)('upload.dedupSkipped', { name }));
      return null;
    }
  }

  private async uploadFile(uploadId: string, file: File | Blob): Promise<UploadResult> {
    const upload = this.uploads.get(uploadId)!;

    // Create cancel token
    const cancelToken = axios.CancelToken.source();
    this.updateUpload(uploadId, { cancelToken });

    // A parked session means a previous attempt died mid-transfer. Pick up the
    // parts already in the bucket instead of re-hashing and re-sending them.
    if (upload.multipart) {
      const resumed = await this.resumeMultipart(uploadId, file, upload.multipart, cancelToken);
      if (resumed) return resumed;
    }

    // Content fingerprint for duplicate detection — the same constant-time
    // imohash the backend computes, so a match here means the same thing it
    // means server-side. Reads 48 KiB whatever the file size. Recordings are
    // generated in this browser and cannot duplicate anything, so they skip it.
    let fingerprint: string | null = null;
    const clientHashStartMs = Date.now();
    if (file instanceof File) {
      this.updateUpload(uploadId, {
        status: 'preparing',
        progress: 0,
        estimatedTime: get(t)('upload.calculatingHash'),
      });
      fingerprint = await this.fingerprintOrWarn(uploadId, file, upload.name);
    }
    const clientHashEndMs = Date.now();

    // Step 1: Prepare the upload — try presigned direct-to-MinIO first,
    // fall back to the legacy multipart POST if the server doesn't support
    // it or anything goes wrong during the direct PUT.
    const prepareResponse = await axiosInstance.post('/files/prepare', {
      filename: upload.name,
      file_size: file.size,
      content_type: file instanceof File ? file.type : 'audio/webm',
      file_hash: fingerprint,
      collection_ids: upload.collectionIds || undefined,
      tag_names: upload.tagNames || undefined,
      upload_batch_id: upload.uploadBatchId || undefined,
      use_presigned: true,
    });

    const {
      file_id: fileId,
      is_duplicate,
      task_id: taskId,
      upload_url: uploadUrl,
      upload_method: uploadMethod,
      multipart: multipartPlan,
    } = prepareResponse.data;

    if (is_duplicate) {
      return { uuid: fileId, isDuplicate: true };
    }

    this.updateUpload(uploadId, {
      status: 'uploading',
      fileId,
      progress: 0,
      estimatedTime: get(t)('upload.statusUploading'),
    });

    // --- Presigned multipart flow ----------------------------------------
    // Chosen by the backend for objects large enough to need resume, or too
    // large for one PUT. No fallback to the legacy POST: the whole point is to
    // keep multi-GB bodies out of the API container, and a failure here is
    // resumable on retry, which re-sending the object from zero is not.
    if (uploadMethod === 'MULTIPART' && multipartPlan && taskId) {
      return await this.runMultipart(
        uploadId,
        file,
        { fileId, taskId, fingerprint, plan: multipartPlan as MultipartPlan },
        cancelToken,
        false
      );
    }

    const progressHandler = this.makeProgressHandler(uploadId, upload);

    // --- Presigned flow ---------------------------------------------------
    if (uploadUrl && uploadMethod === 'PUT' && taskId) {
      try {
        const clientPutStartMs = Date.now();
        await this.sendBody((watchdog) =>
          axios.put(uploadUrl, file, {
            headers: {
              'Content-Type': file instanceof File ? file.type : 'audio/webm',
            },
            maxContentLength: Infinity,
            maxBodyLength: Infinity,
            cancelToken: cancelToken.token,
            signal: watchdog.signal,
            onUploadProgress: progressHandler(watchdog),
          })
        );
        const clientPutEndMs = Date.now();

        await axiosInstance.post('/files/complete', {
          file_id: fileId,
          task_id: taskId,
          file_hash: fingerprint,
          file_size: file.size,
          client_hash_start_ms: clientHashStartMs,
          client_hash_end_ms: clientHashEndMs,
          client_put_start_ms: clientPutStartMs,
          client_put_end_ms: clientPutEndMs,
          min_speakers: upload.minSpeakers ?? null,
          max_speakers: upload.maxSpeakers ?? null,
          num_speakers: upload.numSpeakers ?? null,
        });

        return { uuid: fileId, isDuplicate: false };
      } catch (err: unknown) {
        // A stalled connection is a network fault, not "the server can't do
        // presigned uploads" — re-sending the whole body through the API
        // container would stall too. Let the queue retry the presigned path.
        if (axios.isCancel(err) || err instanceof UploadStalledError) {
          throw err;
        }
        // Fall through to the legacy flow below.
      }
    }

    // --- Legacy flow (multipart POST through the API container) ----------
    const formData = new FormData();
    formData.append('file', file);

    const headers: Record<string, string> = {
      'Content-Type': 'multipart/form-data',
      'X-File-ID': fileId,
      'X-File-Hash': fingerprint || '',
    };

    if (upload.minSpeakers !== null && upload.minSpeakers !== undefined) {
      headers['X-Min-Speakers'] = upload.minSpeakers.toString();
    }
    if (upload.maxSpeakers !== null && upload.maxSpeakers !== undefined) {
      headers['X-Max-Speakers'] = upload.maxSpeakers.toString();
    }
    if (upload.numSpeakers !== null && upload.numSpeakers !== undefined) {
      headers['X-Num-Speakers'] = upload.numSpeakers.toString();
    }

    await this.sendBody((watchdog) =>
      axiosInstance.post('/files', formData, {
        headers,
        maxContentLength: Infinity,
        maxBodyLength: Infinity,
        cancelToken: cancelToken.token,
        signal: watchdog.signal,
        onUploadProgress: progressHandler(watchdog),
      })
    );

    return { uuid: fileId, isDuplicate: false };
  }

  private async uploadExtractedAudio(
    uploadId: string,
    audioBlob: Blob,
    extractionMetadata: ExtractedAudioMetadata
  ): Promise<UploadResult> {
    const upload = this.uploads.get(uploadId)!;

    // Create cancel token
    const cancelToken = axios.CancelToken.source();
    this.updateUpload(uploadId, { cancelToken });

    // Dedupe on the SOURCE VIDEO's fingerprint, carried over from extraction:
    // ffmpeg does not produce the same audio bytes twice, so fingerprinting the
    // blob we are about to upload would never match a previous extraction.
    const sourceFingerprint = extractionMetadata?.originalFingerprint || null;
    const contentType = audioBlob.type || 'audio/webm';

    // Step 1: Prepare — presigned direct-to-MinIO first (same ingress path as
    // regular uploads), carrying the extracted-from-video metadata so the
    // backend records the source video details.
    const prepareResponse = await axiosInstance.post('/files/prepare', {
      filename: upload.name,
      file_size: audioBlob.size,
      content_type: contentType,
      file_hash: sourceFingerprint,
      extracted_from_video: extractionMetadata?.videoMetadata || null,
      collection_ids: upload.collectionIds || undefined,
      tag_names: upload.tagNames || undefined,
      use_presigned: true,
    });

    const {
      file_id: fileId,
      is_duplicate,
      task_id: taskId,
      upload_url: uploadUrl,
      upload_method: uploadMethod,
    } = prepareResponse.data;

    if (is_duplicate) {
      return { uuid: fileId, isDuplicate: true };
    }

    this.updateUpload(uploadId, {
      status: 'uploading',
      fileId,
      progress: 0,
      estimatedTime: get(t)('upload.statusUploadingExtracted'),
    });

    const progressHandler = this.makeProgressHandler(uploadId, upload);

    // --- Presigned flow: PUT the audio blob straight to MinIO, then finalize.
    if (uploadUrl && uploadMethod === 'PUT' && taskId) {
      try {
        const clientPutStartMs = Date.now();
        await this.sendBody((watchdog) =>
          axios.put(uploadUrl, audioBlob, {
            headers: { 'Content-Type': contentType },
            maxContentLength: Infinity,
            maxBodyLength: Infinity,
            cancelToken: cancelToken.token,
            signal: watchdog.signal,
            onUploadProgress: progressHandler(watchdog),
          })
        );
        const clientPutEndMs = Date.now();

        await axiosInstance.post('/files/complete', {
          file_id: fileId,
          task_id: taskId,
          file_hash: sourceFingerprint,
          file_size: audioBlob.size,
          client_put_start_ms: clientPutStartMs,
          client_put_end_ms: clientPutEndMs,
          min_speakers: upload.minSpeakers ?? null,
          max_speakers: upload.maxSpeakers ?? null,
          num_speakers: upload.numSpeakers ?? null,
        });

        return { uuid: fileId, isDuplicate: false };
      } catch (err: unknown) {
        // See uploadFile(): a stall must not silently re-send the body through
        // the API container.
        if (axios.isCancel(err) || err instanceof UploadStalledError) {
          throw err;
        }
        // Fall through to the legacy flow below.
      }
    }

    // --- Legacy fallback (multipart POST through the API container) -------
    const formData = new FormData();
    formData.append('file', audioBlob, upload.name);

    await this.sendBody((watchdog) =>
      axiosInstance.post('/files', formData, {
        headers: {
          'Content-Type': 'multipart/form-data',
          'X-File-ID': fileId,
          'X-File-Hash': sourceFingerprint || '',
        },
        maxContentLength: Infinity,
        maxBodyLength: Infinity,
        cancelToken: cancelToken.token,
        signal: watchdog.signal,
        onUploadProgress: progressHandler(watchdog),
      })
    );

    return { uuid: fileId, isDuplicate: false };
  }

  private async processUrl(uploadId: string, url: string): Promise<UploadResult> {
    const upload = this.uploads.get(uploadId)!;

    // Create cancel token
    const cancelToken = axios.CancelToken.source();
    this.updateUpload(uploadId, {
      cancelToken,
      status: 'processing',
    });

    const response = await axiosInstance.post(
      '/files/process-url',
      {
        url: url.trim(),
        collection_ids: upload.collectionIds || undefined,
        tag_names: upload.tagNames || undefined,
      },
      {
        timeout: CONTROL_REQUEST_TIMEOUT_MS,
        cancelToken: cancelToken.token,
        onUploadProgress: (progressEvent: AxiosProgressEvent) => {
          if (progressEvent.total) {
            const progress = Math.round((progressEvent.loaded * 100) / progressEvent.total);
            this.updateUpload(uploadId, { progress });
            this.emit('progress', uploadId, { progress });
          }
        },
      }
    );

    return { uuid: response.data.uuid, isDuplicate: false };
  }

  // Upload management
  retryUpload(uploadId: string) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;

    this.updateUpload(uploadId, {
      status: 'queued',
      progress: 0,
      error: undefined,
      retryCount: upload.retryCount + 1,
      cancelToken: undefined,
    });

    this.processingQueue.push(uploadId);
    this.emit('retry', uploadId);
    this.processQueue();
    this.persistUploads();
  }

  cancelUpload(uploadId: string) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;

    // Cancel the request if it has a cancel token
    if (upload.cancelToken) {
      upload.cancelToken.cancel('Upload cancelled by user');
    }

    if (upload.multipart) {
      this.abortMultipart(upload.multipart.fileId);
    }

    this.updateUpload(uploadId, {
      status: 'cancelled',
      error: get(t)('upload.cancelledByUser'),
      multipart: undefined,
    });

    // Remove from active uploads and queue
    this.activeUploads.delete(uploadId);
    const queueIndex = this.processingQueue.indexOf(uploadId);
    if (queueIndex > -1) {
      this.processingQueue.splice(queueIndex, 1);
    }

    this.emit('cancelled', uploadId);
    this.persistUploads();

    // Continue processing queue
    this.processQueue();
  }

  removeUpload(uploadId: string) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;

    // Cancel if still active
    if (upload.status === 'uploading' || upload.status === 'processing') {
      this.cancelUpload(uploadId);
    }

    this.uploads.delete(uploadId);
    this.persistUploads();
  }

  clearCompleted() {
    const completedIds = Array.from(this.uploads.entries())
      .filter(([_, upload]) => upload.status === 'completed')
      .map(([id, _]) => id);

    completedIds.forEach((id) => this.uploads.delete(id));
    this.persistUploads();
  }

  /**
   * Full reset — cancels all in-flight uploads, clears the queue, and
   * wipes persisted state. Called on logout to prevent User A's uploads
   * from leaking into User B's session.
   */
  reset() {
    // Cancel any in-flight uploads
    for (const [, upload] of this.uploads.entries()) {
      if (upload.cancelToken) {
        try {
          upload.cancelToken.cancel('User logged out');
        } catch {
          /* ignore */
        }
      }
      // Abort before the session cookie goes away — afterwards nothing can.
      if (upload.multipart) {
        this.abortMultipart(upload.multipart.fileId);
      }
    }
    this.uploads.clear();
    this.processingQueue = [];
    this.activeUploads.clear();
    try {
      localStorage.removeItem('upload_queue');
    } catch {
      /* ignore */
    }
  }

  // Getters
  getUpload(uploadId: string): UploadItem | undefined {
    return this.uploads.get(uploadId);
  }

  getAllUploads(): UploadItem[] {
    return Array.from(this.uploads.values());
  }

  getActiveUploads(): UploadItem[] {
    return this.getAllUploads().filter(
      (upload) =>
        upload.status === 'uploading' ||
        upload.status === 'processing' ||
        upload.status === 'preparing'
    );
  }

  getQueuedUploads(): UploadItem[] {
    return this.getAllUploads().filter((upload) => upload.status === 'queued');
  }

  // Helper methods
  private updateUpload(uploadId: string, updates: Partial<UploadItem>) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;

    Object.assign(upload, updates);
    this.uploads.set(uploadId, upload);
  }

  private generateId(): string {
    return generateId('upload');
  }

  private getSourceName(source: File | string | Blob): string {
    if (source instanceof File) {
      return source.name;
    } else if (typeof source === 'string') {
      try {
        const url = new URL(source);
        // Try to extract YouTube video title or use a more descriptive name
        if (url.hostname.includes('youtube.com') || url.hostname.includes('youtu.be')) {
          return get(t)('upload.youtubeVideo');
        }
        return url.pathname.split('/').pop() || get(t)('upload.videoUrl');
      } catch {
        return get(t)('upload.videoUrl');
      }
    } else {
      // Blob (recording)
      return `recording_${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;
    }
  }

  private getErrorMessage(error: unknown): string {
    if (axios.isCancel(error)) {
      return get(t)('upload.cancelled');
    }

    const e = error as { response?: { data?: { detail?: string } }; message?: string };
    if (e?.response?.data?.detail) {
      return e.response.data.detail;
    }

    if (e?.message) {
      return e.message;
    }

    return get(t)('common.unknownError');
  }

  private formatTimeRemaining(ms: number): string {
    if (!ms || ms <= 0) return '';

    const seconds = Math.ceil(ms / 1000);
    if (seconds < 60) return `${seconds}s`;

    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;

    if (minutes < 60) {
      return remainingSeconds > 0 ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
    }

    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    return `${hours}h ${remainingMinutes}m`;
  }

  // Persistence
  private persistUploads() {
    try {
      const uploadsData = Array.from(this.uploads.entries()).map(([id, upload]) => [
        id,
        {
          ...upload,
          // Don't persist source data, cancel tokens, or a multipart session:
          // resuming one needs the File, which cannot survive a reload.
          source: upload.type === 'url' ? upload.source : null,
          cancelToken: undefined,
          multipart: undefined,
        },
      ]);
      localStorage.setItem('upload_queue', JSON.stringify(uploadsData));
    } catch (error) {
      // localStorage persistence is optional
    }
  }

  private loadPersistedUploads() {
    try {
      const stored = localStorage.getItem('upload_queue');
      if (stored) {
        const uploadsData = JSON.parse(stored);
        uploadsData.forEach(([id, upload]: [string, UploadItem]) => {
          // Only restore queued or failed uploads that can be retried
          if (upload.status === 'queued' || upload.status === 'failed') {
            // Reset to queued state for retry
            upload.status = 'queued';
            upload.progress = 0;
            upload.error = undefined;
            this.uploads.set(id, upload);

            // Only add URL uploads back to queue (files would need re-selection)
            if (upload.type === 'url' && upload.source) {
              this.processingQueue.push(id);
            }
          }
        });

        // Start processing any restored uploads
        if (this.processingQueue.length > 0) {
          setTimeout(() => this.processQueue(), 1000);
        }
      }
    } catch (error) {
      // localStorage loading is optional
    }
  }
}

// Export singleton instance
export const uploadService = new UploadService();
