import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import { t } from '$stores/locale';
import axios from 'axios';
import type { ExtractedAudioMetadata } from '$lib/types/audioExtraction';
import { generateId } from '$lib/utils/ids';
import { normalizeMediaType } from '$lib/utils/mediaType';
import type { MultipartPlan } from '$lib/services/multipartUploader';
import { reconnectDelayMs } from '$lib/utils/backoff';
import { putThenCompleteOrFallback } from './upload/completion';
import { getErrorMessage, getSourceName } from './upload/format';
import {
  abortMultipart,
  fingerprintOrWarn,
  resumeMultipart,
  runMultipart,
} from './upload/multipart';
import {
  discardPersistedUploads,
  loadPersistedUploads,
  persistUploads,
} from './upload/persistence';
import { makeProgressHandler } from './upload/transfer';
import {
  MAX_CONCURRENT_UPLOADS,
  MAX_RETRIES,
  QUEUE_PROCESS_DELAY_MS,
  type UploadEvent,
  type UploadEventType,
  type UploadHost,
  type UploadItem,
  type UploadResult,
  type UploadTranscriptionParams,
  type UploadType,
} from './upload/types';
import { processUrl } from './upload/urlIngest';

export type { UploadEvent, UploadItem, UploadTranscriptionParams };

// The `/files/prepare` body for an extracted-audio upload: the blob's own size and type, the
// SOURCE video's fingerprint, and the extracted-from-video metadata the backend records.
function extractedPrepareBody(
  upload: UploadItem,
  audioBlob: Blob,
  contentType: string,
  sourceFingerprint: string | null,
  extractionMetadata: ExtractedAudioMetadata | undefined
) {
  return {
    filename: upload.name,
    file_size: audioBlob.size,
    content_type: contentType,
    file_hash: sourceFingerprint,
    extracted_from_video: extractionMetadata?.videoMetadata || null,
    collection_ids: upload.collectionIds || undefined,
    tag_names: upload.tagNames || undefined,
    whisper_model: upload.whisperModel || undefined,
    voiceprint_consent: upload.voiceprintConsent || undefined,
    use_presigned: true,
  };
}

class UploadService {
  private uploads: Map<string, UploadItem> = new Map();
  private eventListeners: ((event: UploadEvent) => void)[] = [];
  private processingQueue: string[] = [];
  private activeUploads: Set<string> = new Set();

  private readonly host: UploadHost = {
    getItem: (id) => this.uploads.get(id),
    update: (id, updates) => this.updateUpload(id, updates),
    emit: (type, id, data) => this.emit(type, id, data),
  };

  constructor() {
    const { entries, queueIds } = loadPersistedUploads();
    entries.forEach(([id, upload]) => this.uploads.set(id, upload));
    this.processingQueue.push(...queueIds);
    // Start processing any restored uploads
    if (this.processingQueue.length > 0) {
      setTimeout(() => void this.processQueue(), 1000);
    }
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
    transcriptionParams?: UploadTranscriptionParams,
    collectionIds?: string[],
    tagNames?: string[],
    uploadBatchId?: string
  ): string {
    const id = this.generateId();
    const uploadName = name || getSourceName(source);

    const upload: UploadItem = {
      id,
      type,
      source,
      name: uploadName,
      size: source instanceof File ? source.size : undefined,
      status: 'queued',
      progress: 0,
      retryCount: 0,
      minSpeakers: transcriptionParams?.minSpeakers,
      maxSpeakers: transcriptionParams?.maxSpeakers,
      numSpeakers: transcriptionParams?.numSpeakers,
      whisperModel: transcriptionParams?.whisperModel,
      skipSummary: transcriptionParams?.skipSummary,
      voiceprintConsent: transcriptionParams?.voiceprintConsent,
      collectionIds,
      tagNames,
      uploadBatchId,
    };

    this.uploads.set(id, upload);
    this.processingQueue.push(id);
    persistUploads(this.uploads);

    this.emit('added', id, upload);

    // Start processing if we have capacity
    void this.processQueue();

    return id;
  }

  addMultipleFiles(
    files: File[],
    collectionIds?: string[],
    tagNames?: string[],
    transcriptionParams?: UploadTranscriptionParams
  ): string[] {
    const uploadIds: string[] = [];

    // Generate a shared batch UUID when uploading 2+ files together
    // so they are linked as a batch for downstream topic grouping
    const batchId = files.length >= 2 ? generateId() : undefined;

    files.forEach((file) => {
      const id = this.addUpload(
        'file',
        file,
        undefined,
        transcriptionParams,
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
    compressionRatio: number,
    transcriptionParams?: UploadTranscriptionParams,
    collectionIds?: string[],
    tagNames?: string[]
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
      minSpeakers: transcriptionParams?.minSpeakers,
      maxSpeakers: transcriptionParams?.maxSpeakers,
      numSpeakers: transcriptionParams?.numSpeakers,
      whisperModel: transcriptionParams?.whisperModel,
      skipSummary: transcriptionParams?.skipSummary,
      voiceprintConsent: transcriptionParams?.voiceprintConsent,
      collectionIds,
      tagNames,
    };

    this.uploads.set(id, upload);
    this.processingQueue.push(id);
    persistUploads(this.uploads);

    this.emit('added', id, upload);

    // Start processing if we have capacity
    void this.processQueue();

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
    } catch {
      // Error is handled in processUpload method
    } finally {
      this.activeUploads.delete(nextUploadId);
      // Continue processing queue
      setTimeout(() => void this.processQueue(), QUEUE_PROCESS_DELAY_MS);
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
        statusText: undefined,
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
          result = await processUrl(this.host, uploadId, upload.source as string);
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
        statusText: undefined,
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

      const errorMessage = getErrorMessage(error);
      // The phase text belongs to the attempt that just died ("Calculating file hash…"
      // stayed on failed rows); the error is what the row says now.
      this.updateUpload(uploadId, {
        status: 'failed',
        error: errorMessage,
        statusText: undefined,
        estimatedTime: undefined,
      });

      this.emit('failed', uploadId, { error: errorMessage });

      // Handle retry logic. Shared backoff policy (H4a): capped at 30 s, jittered
      // so a backend restart doesn't bring every client's retries back in lockstep.
      if (upload.retryCount < MAX_RETRIES && !axios.isCancel(error)) {
        setTimeout(
          () => {
            this.retryUpload(uploadId);
          },
          reconnectDelayMs(upload.retryCount + 1)
        );
      } else {
        // Out of retries: release the parts of an unfinished multipart upload
        // rather than leaving the user paying for gigabytes nothing will claim.
        if (upload.multipart) {
          abortMultipart(upload.multipart.fileId);
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

    persistUploads(this.uploads);
  }

  private async uploadFile(uploadId: string, file: File | Blob): Promise<UploadResult> {
    const upload = this.uploads.get(uploadId)!;

    // Create cancel token
    const cancelToken = axios.CancelToken.source();
    this.updateUpload(uploadId, { cancelToken });

    // A parked session means a previous attempt died mid-transfer. Pick up the
    // parts already in the bucket instead of re-hashing and re-sending them.
    if (upload.multipart) {
      const resumed = await resumeMultipart(
        this.host,
        uploadId,
        file,
        upload.multipart,
        cancelToken
      );
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
        statusText: get(t)('upload.calculatingHash'),
      });
      fingerprint = await fingerprintOrWarn(this.host, uploadId, file, upload.name);
    }
    const clientHashEndMs = Date.now();

    // Canonical spelling (issue #1044): a single presigned PUT stores this as the
    // object's Content-Type, and aliases such as audio/vnd.wave don't play.
    const mediaType = normalizeMediaType(file instanceof File ? file.type : 'audio/webm') ?? '';

    // Step 1: Prepare the upload — try presigned direct-to-MinIO first,
    // fall back to the legacy multipart POST if the server doesn't support
    // it or anything goes wrong during the direct PUT.
    const prepareResponse = await axiosInstance.post('/files/prepare', {
      filename: upload.name,
      file_size: file.size,
      content_type: mediaType,
      file_hash: fingerprint,
      collection_ids: upload.collectionIds || undefined,
      tag_names: upload.tagNames || undefined,
      upload_batch_id: upload.uploadBatchId || undefined,
      whisper_model: upload.whisperModel || undefined,
      voiceprint_consent: upload.voiceprintConsent || undefined,
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
      statusText: get(t)('upload.statusUploading'),
    });

    // --- Presigned multipart flow ----------------------------------------
    // Chosen by the backend for objects large enough to need resume, or too
    // large for one PUT. No fallback to the legacy POST: the whole point is to
    // keep multi-GB bodies out of the API container, and a failure here is
    // resumable on retry, which re-sending the object from zero is not.
    if (uploadMethod === 'MULTIPART' && multipartPlan && taskId) {
      return await runMultipart(
        this.host,
        uploadId,
        file,
        { fileId, taskId, fingerprint, plan: multipartPlan as MultipartPlan },
        cancelToken,
        false
      );
    }

    return putThenCompleteOrFallback({
      upload,
      body: file,
      fileId,
      taskId,
      uploadUrl,
      uploadMethod,
      fingerprint,
      contentType: mediaType,
      cancelToken,
      progressHandler: makeProgressHandler(this.host, uploadId, upload),
      hashWindowMs: { start: clientHashStartMs, end: clientHashEndMs },
    });
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

    // A parked session means a previous attempt died mid-transfer — same as
    // uploadFile(), pick up the parts already in the bucket instead of
    // re-sending them.
    if (upload.multipart) {
      const resumed = await resumeMultipart(
        this.host,
        uploadId,
        audioBlob,
        upload.multipart,
        cancelToken
      );
      if (resumed) return resumed;
    }

    // Dedupe on the SOURCE VIDEO's fingerprint, carried over from extraction:
    // ffmpeg does not produce the same audio bytes twice, so fingerprinting the
    // blob we are about to upload would never match a previous extraction.
    const sourceFingerprint = extractionMetadata?.originalFingerprint || null;
    const contentType = audioBlob.type || 'audio/webm';

    // Step 1: Prepare — presigned direct-to-MinIO first (same ingress path as
    // regular uploads), carrying the extracted-from-video metadata so the
    // backend records the source video details.
    const prepareResponse = await axiosInstance.post(
      '/files/prepare',
      extractedPrepareBody(upload, audioBlob, contentType, sourceFingerprint, extractionMetadata)
    );

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
      statusText: get(t)('upload.statusUploadingExtracted'),
    });

    // --- Presigned multipart flow -----------------------------------------
    // Same rationale as uploadFile(): the backend picks this for objects too
    // large/needing resume. No fallback to the legacy POST for the same
    // reason — extracted audio from a long source video can exceed the
    // multipart threshold even with stream-copy (-c:a copy, no re-encode).
    if (uploadMethod === 'MULTIPART' && multipartPlan && taskId) {
      return await runMultipart(
        this.host,
        uploadId,
        audioBlob,
        { fileId, taskId, fingerprint: sourceFingerprint, plan: multipartPlan as MultipartPlan },
        cancelToken,
        false
      );
    }

    return putThenCompleteOrFallback({
      upload,
      body: audioBlob,
      fileId,
      taskId,
      uploadUrl,
      uploadMethod,
      fingerprint: sourceFingerprint,
      contentType,
      cancelToken,
      progressHandler: makeProgressHandler(this.host, uploadId, upload),
      legacyFilename: upload.name,
    });
  }
  // Upload management
  retryUpload(uploadId: string) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;
    // A retry is scheduled via setTimeout from processUpload's catch block with
    // no way to cancel that timer. If the upload was cancelled (or otherwise
    // moved on) while the timer was still pending, this guard stops it from
    // silently resurrecting a 'cancelled' upload back to 'queued' — after its
    // multipart session, if any, was already released server-side.
    if (upload.status !== 'failed') return;

    this.updateUpload(uploadId, {
      status: 'queued',
      progress: 0,
      error: undefined,
      retryCount: upload.retryCount + 1,
      cancelToken: undefined,
    });

    this.processingQueue.push(uploadId);
    this.emit('retry', uploadId);
    void this.processQueue();
    persistUploads(this.uploads);
  }

  cancelUpload(uploadId: string) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;

    // Cancel the request if it has a cancel token
    if (upload.cancelToken) {
      upload.cancelToken.cancel('Upload cancelled by user');
    }

    if (upload.multipart) {
      abortMultipart(upload.multipart.fileId);
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
    persistUploads(this.uploads);

    // Continue processing queue
    void this.processQueue();
  }

  removeUpload(uploadId: string) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;

    // Cancel if still active
    if (
      upload.status === 'uploading' ||
      upload.status === 'processing' ||
      upload.status === 'preparing'
    ) {
      this.cancelUpload(uploadId);
    }

    this.uploads.delete(uploadId);
    persistUploads(this.uploads);
  }

  clearCompleted() {
    const completedIds = Array.from(this.uploads.entries())
      .filter(([_, upload]) => upload.status === 'completed')
      .map(([id, _]) => id);

    completedIds.forEach((id) => this.uploads.delete(id));
    persistUploads(this.uploads);
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
        abortMultipart(upload.multipart.fileId);
      }
    }
    this.uploads.clear();
    this.processingQueue = [];
    this.activeUploads.clear();
    discardPersistedUploads();
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
}

// Export singleton instance
export const uploadService = new UploadService();
