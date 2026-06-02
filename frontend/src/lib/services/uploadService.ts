import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { authStore } from '$stores/auth';
import { toastStore } from '$stores/toast';
import { t } from '$stores/locale';
import axios, { type AxiosProgressEvent, type CancelTokenSource } from 'axios';
import type { ExtractedAudioMetadata } from '$lib/types/audioExtraction';
import { generateId } from '$lib/utils/ids';
import { hashFileSHA256 } from '$lib/services/sha256Hasher';

// Upload item types
export type UploadType = 'file' | 'url' | 'recording' | 'extracted-audio';
export type UploadStatus =
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
}

// Upload configuration constants
const MAX_RETRIES = 3;
const RETRY_BASE_DELAY_MS = 1000;
const MAX_CONCURRENT_UPLOADS = 3;
const UPLOAD_TIMEOUT_MS = 300000; // 5 minutes
const QUEUE_PROCESS_DELAY_MS = 100;

// Event types for upload lifecycle
export type UploadEventType =
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

// Hash calculation for duplicate detection. Uses a Web Worker so the UI
// stays responsive on multi-GB files — see $lib/services/sha256Hasher.
async function calculateFileHash(file: File): Promise<string> {
  return hashFileSHA256(file);
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
    const batchId = files.length >= 2 ? crypto.randomUUID() : undefined;

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

  private async uploadFile(uploadId: string, file: File | Blob): Promise<UploadResult> {
    const upload = this.uploads.get(uploadId)!;

    // Create cancel token
    const cancelToken = axios.CancelToken.source();
    this.updateUpload(uploadId, { cancelToken });

    // Calculate file hash for duplicate detection (SHA-256 via Web Worker).
    let fileHash: string | null = null;
    const clientHashStartMs = Date.now();
    if (file instanceof File) {
      try {
        this.updateUpload(uploadId, {
          status: 'preparing',
          progress: 0,
          estimatedTime: get(t)('upload.calculatingHash'),
        });
        fileHash = await calculateFileHash(file);
      } catch {
        // File hash calculation is optional, continue without it
      }
    }
    const clientHashEndMs = Date.now();

    // Step 1: Prepare the upload — try presigned direct-to-MinIO first,
    // fall back to the legacy multipart POST if the server doesn't support
    // it or anything goes wrong during the direct PUT.
    const prepareResponse = await axiosInstance.post('/files/prepare', {
      filename: upload.name,
      file_size: file.size,
      content_type: file instanceof File ? file.type : 'audio/webm',
      file_hash: fileHash,
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
    } = prepareResponse.data;

    if (is_duplicate) {
      return { uuid: fileId, isDuplicate: true };
    }

    this.updateUpload(uploadId, {
      status: 'uploading',
      fileId,
      progress: 0,
      estimatedTime: 'Uploading...',
    });

    const progressHandler = (progressEvent: AxiosProgressEvent) => {
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

    // --- Presigned flow ---------------------------------------------------
    if (uploadUrl && uploadMethod === 'PUT' && taskId) {
      try {
        const clientPutStartMs = Date.now();
        await axios.put(uploadUrl, file, {
          headers: {
            'Content-Type': file instanceof File ? file.type : 'audio/webm',
          },
          timeout: UPLOAD_TIMEOUT_MS,
          maxContentLength: Infinity,
          maxBodyLength: Infinity,
          cancelToken: cancelToken.token,
          onUploadProgress: progressHandler,
        });
        const clientPutEndMs = Date.now();

        await axiosInstance.post('/files/complete', {
          file_id: fileId,
          task_id: taskId,
          file_hash: fileHash,
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
        if (axios.isCancel(err)) {
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
      'X-File-Hash': fileHash || '',
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

    await axiosInstance.post('/files', formData, {
      headers,
      timeout: UPLOAD_TIMEOUT_MS,
      maxContentLength: Infinity,
      maxBodyLength: Infinity,
      cancelToken: cancelToken.token,
      onUploadProgress: progressHandler,
    });

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

    // Use original video file hash from extraction metadata for duplicate detection
    const originalFileHash = extractionMetadata?.originalFileHash || null;
    const contentType = audioBlob.type || 'audio/webm';

    // Step 1: Prepare — presigned direct-to-MinIO first (same ingress path as
    // regular uploads), carrying the extracted-from-video metadata so the
    // backend records the source video details.
    const prepareResponse = await axiosInstance.post('/files/prepare', {
      filename: upload.name,
      file_size: audioBlob.size,
      content_type: contentType,
      file_hash: originalFileHash,
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
      estimatedTime: 'Uploading extracted audio...',
    });

    const progressHandler = (progressEvent: AxiosProgressEvent) => {
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

    // --- Presigned flow: PUT the audio blob straight to MinIO, then finalize.
    if (uploadUrl && uploadMethod === 'PUT' && taskId) {
      try {
        const clientPutStartMs = Date.now();
        await axios.put(uploadUrl, audioBlob, {
          headers: { 'Content-Type': contentType },
          timeout: UPLOAD_TIMEOUT_MS,
          maxContentLength: Infinity,
          maxBodyLength: Infinity,
          cancelToken: cancelToken.token,
          onUploadProgress: progressHandler,
        });
        const clientPutEndMs = Date.now();

        await axiosInstance.post('/files/complete', {
          file_id: fileId,
          task_id: taskId,
          file_hash: originalFileHash,
          file_size: audioBlob.size,
          client_put_start_ms: clientPutStartMs,
          client_put_end_ms: clientPutEndMs,
          min_speakers: upload.minSpeakers ?? null,
          max_speakers: upload.maxSpeakers ?? null,
          num_speakers: upload.numSpeakers ?? null,
        });

        return { uuid: fileId, isDuplicate: false };
      } catch (err: unknown) {
        if (axios.isCancel(err)) {
          throw err;
        }
        // Fall through to the legacy flow below.
      }
    }

    // --- Legacy fallback (multipart POST through the API container) -------
    const formData = new FormData();
    formData.append('file', audioBlob, upload.name);

    await axiosInstance.post('/files', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
        'X-File-ID': fileId,
        'X-File-Hash': originalFileHash || '',
      },
      timeout: UPLOAD_TIMEOUT_MS,
      maxContentLength: Infinity,
      maxBodyLength: Infinity,
      cancelToken: cancelToken.token,
      onUploadProgress: progressHandler,
    });

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
        timeout: UPLOAD_TIMEOUT_MS,
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

    this.updateUpload(uploadId, {
      status: 'cancelled',
      error: 'Cancelled by user',
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
    for (const [id, upload] of this.uploads.entries()) {
      if (upload.cancelToken) {
        try {
          upload.cancelToken.cancel('User logged out');
        } catch {
          /* ignore */
        }
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
          return 'YouTube Video';
        }
        return url.pathname.split('/').pop() || 'Video URL';
      } catch {
        return 'Video URL';
      }
    } else {
      // Blob (recording)
      return `recording_${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;
    }
  }

  private getErrorMessage(error: unknown): string {
    if (axios.isCancel(error)) {
      return 'Upload cancelled';
    }

    const e = error as { response?: { data?: { detail?: string } }; message?: string };
    if (e?.response?.data?.detail) {
      return e.response.data.detail;
    }

    if (e?.message) {
      return e.message;
    }

    return 'Unknown error occurred';
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
          // Don't persist source data or cancel tokens
          source: upload.type === 'url' ? upload.source : null,
          cancelToken: undefined,
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
