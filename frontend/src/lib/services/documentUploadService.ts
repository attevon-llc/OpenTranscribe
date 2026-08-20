import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import { t } from '$stores/locale';
import axios, { type AxiosProgressEvent } from 'axios';
import { generateId } from '$lib/utils/ids';
import { createStallWatchdog } from '$lib/services/stallWatchdog';
import type { DocumentResponse } from '$lib/types/document';

/**
 * The document upload queue — a lightweight sibling of `uploadService.ts`, not a reuse
 * of it. `POST /documents` (backend/app/api/endpoints/documents.py) is a single-shot
 * multipart body read straight into the request, capped at 256 MB by default — a
 * completely different contract from media's presigned-prepare/multipart/complete dance,
 * which exists because a recording can be 15 GB. Reusing `uploadService` would mean
 * teaching it a second upload protocol; a dedicated queue with the same shape
 * (id/status/progress, stall-watchdog-guarded body, event bus) is simpler and does not
 * risk the media path.
 */

export type DocumentUploadStatus = 'queued' | 'uploading' | 'completed' | 'failed';

export interface DocumentUploadItem {
  id: string;
  file: File;
  name: string;
  size: number;
  status: DocumentUploadStatus;
  progress: number;
  error?: string;
  documentUuid?: string;
  /** Set only while `runUpload` is in flight; lets `reset()` abort a live body transfer. */
  abortController?: AbortController;
}

type DocumentUploadEventType = 'added' | 'progress' | 'completed' | 'failed';

export interface DocumentUploadEvent {
  type: DocumentUploadEventType;
  uploadId: string;
  data?: unknown;
}

const MAX_CONCURRENT_UPLOADS = 3;

class DocumentUploadService {
  private uploads: Map<string, DocumentUploadItem> = new Map();
  private eventListeners: ((event: DocumentUploadEvent) => void)[] = [];
  private queue: string[] = [];
  private active: Set<string> = new Set();

  addEventListener(listener: (event: DocumentUploadEvent) => void) {
    this.eventListeners.push(listener);
    return () => {
      const index = this.eventListeners.indexOf(listener);
      if (index > -1) this.eventListeners.splice(index, 1);
    };
  }

  private emit(type: DocumentUploadEventType, uploadId: string, data?: unknown) {
    this.eventListeners.forEach((listener) => listener({ type, uploadId, data }));
  }

  addFiles(files: File[]): string[] {
    return files.map((file) => this.addFile(file));
  }

  addFile(file: File): string {
    const id = generateId('doc-upload');
    const upload: DocumentUploadItem = {
      id,
      file,
      name: file.name,
      size: file.size,
      status: 'queued',
      progress: 0,
    };
    this.uploads.set(id, upload);
    this.queue.push(id);
    this.emit('added', id, upload);
    this.processQueue();
    return id;
  }

  getAllUploads(): DocumentUploadItem[] {
    return Array.from(this.uploads.values());
  }

  removeUpload(uploadId: string) {
    this.uploads.delete(uploadId);
    const idx = this.queue.indexOf(uploadId);
    if (idx > -1) this.queue.splice(idx, 1);
  }

  clearCompleted() {
    for (const [id, upload] of this.uploads.entries()) {
      if (upload.status === 'completed') this.uploads.delete(id);
    }
  }

  /**
   * Full reset — aborts in-flight uploads and wipes all queue state. Called on
   * logout (`$lib/session/clearUserState.ts`) so User A's in-flight document
   * uploads cannot complete into or be visible in User B's session, mirroring
   * `uploadService.reset()`.
   */
  reset() {
    for (const upload of this.uploads.values()) {
      upload.abortController?.abort();
    }
    this.uploads.clear();
    this.queue = [];
    this.active.clear();
  }

  private updateUpload(uploadId: string, updates: Partial<DocumentUploadItem>) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;
    Object.assign(upload, updates);
  }

  private processQueue() {
    if (this.active.size >= MAX_CONCURRENT_UPLOADS) return;
    const nextId = this.queue.shift();
    if (!nextId) return;

    this.active.add(nextId);
    this.runUpload(nextId).finally(() => {
      this.active.delete(nextId);
      this.processQueue();
    });
  }

  private async runUpload(uploadId: string) {
    const upload = this.uploads.get(uploadId);
    if (!upload) return;

    // Separate from the watchdog's own signal: `reset()` (session logout) must be
    // able to cancel a live body transfer, and `createStallWatchdog` exposes only a
    // read-only signal with no external abort — it is a per-request factory owned by
    // the caller, not built for outside cancellation. `AbortSignal.any` composes the
    // two without touching that shared module (also used by the media upload path).
    const abortController = new AbortController();
    this.updateUpload(uploadId, { status: 'uploading', progress: 0, abortController });

    const formData = new FormData();
    formData.append('file', upload.file);

    const watchdog = createStallWatchdog();
    try {
      const response = await axiosInstance.post<DocumentResponse>('/documents', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        maxContentLength: Infinity,
        maxBodyLength: Infinity,
        signal: AbortSignal.any([watchdog.signal, abortController.signal]),
        onUploadProgress: (evt: AxiosProgressEvent) => {
          watchdog.notifyProgress(evt.loaded, evt.total);
          if (!evt.total) return;
          const progress = Math.min(Math.round((evt.loaded * 100) / evt.total), 99);
          this.updateUpload(uploadId, { progress });
          this.emit('progress', uploadId, { progress });
        },
      });

      this.updateUpload(uploadId, {
        status: 'completed',
        progress: 100,
        documentUuid: response.data.uuid,
        abortController: undefined,
      });
      this.emit('completed', uploadId, response.data);
      toastStore.success(get(t)('documents.uploadCompleted', { name: upload.name }));
    } catch (err: unknown) {
      if (abortController.signal.aborted) {
        // Session reset — the item is about to be deleted by reset() itself;
        // no toast, no failed-state churn for something the user didn't cause.
        return;
      }
      const message = this.getErrorMessage(err, watchdog.stalled);
      this.updateUpload(uploadId, { status: 'failed', error: message, abortController: undefined });
      this.emit('failed', uploadId, { error: message });
      toastStore.error(get(t)('documents.uploadFailed', { name: upload.name, error: message }));
    } finally {
      watchdog.dispose();
    }
  }

  private getErrorMessage(error: unknown, stalled: boolean): string {
    if (stalled) return get(t)('documents.uploadStalled');
    if (axios.isCancel(error)) return get(t)('common.unknownError');
    const e = error as { response?: { data?: { detail?: string } }; message?: string };
    return e?.response?.data?.detail || e?.message || get(t)('common.unknownError');
  }
}

export const documentUploadService = new DocumentUploadService();
