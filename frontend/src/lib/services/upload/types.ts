import type { CancelTokenSource } from 'axios';
import type { ExtractedAudioMetadata } from '$lib/types/audioExtraction';
import type { MultipartPlan } from '$lib/services/multipartUploader';

// Upload item types
export type UploadType = 'file' | 'url' | 'recording' | 'extracted-audio';
type UploadStatus =
  | 'queued'
  | 'preparing'
  | 'uploading'
  | 'processing'
  | 'completed'
  | 'failed'
  | 'cancelled';

/**
 * The per-file choices the upload wizard collects. Optional everywhere: a caller that has
 * none (a recording, a watch import) sends none, and the server applies its defaults.
 */
export interface UploadTranscriptionParams {
  minSpeakers?: number | null;
  maxSpeakers?: number | null;
  numSpeakers?: number | null;
  /** `null` = the deployment's default model; sent only when the user picked one. */
  whisperModel?: string | null;
  skipSummary?: boolean;
  /** The uploader's statement that the speakers agreed to voice identification (a claim, not verified). */
  voiceprintConsent?: 'granted' | null;
}

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
  /**
   * Formatted time-to-completion ONLY (e.g. `2m 30s`), produced by
   * `formatTimeRemaining`. `UploadProgress.svelte` renders it as
   * `{estimatedTime} remaining`, so anything that is not a duration reads as
   * nonsense there — this field carried localized STATUS strings for four
   * phases, which is how the UI came to say "Calculating file hash... remaining".
   * Put phase text in `statusText`.
   */
  estimatedTime?: string;
  /**
   * Localized description of the current phase ("Calculating file hash…",
   * "Resuming…"). Rendered on its own, never suffixed with "remaining".
   */
  statusText?: string;
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
  // Per-file transcription choices made in the upload wizard
  whisperModel?: string | null;
  skipSummary?: boolean;
  voiceprintConsent?: 'granted' | null;
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
export interface MultipartSession {
  fileId: string;
  taskId: string;
  fingerprint: string | null;
  plan: MultipartPlan;
}

// Upload configuration constants
export const MAX_RETRIES = 3;
export const MAX_CONCURRENT_UPLOADS = 3;
export const QUEUE_PROCESS_DELAY_MS = 100;

// Total-request timeout for the small JSON control-plane calls only (prepare,
// complete, process-url). File bodies must NEVER use a total-request timeout —
// see the note on UploadStalledError below.
export const CONTROL_REQUEST_TIMEOUT_MS = 300000; // 5 minutes

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
export interface UploadResult {
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
export class UploadStalledError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UploadStalledError';
  }
}

/**
 * What the extracted upload helpers need from the queue: read an item, patch it, announce an
 * event. `UploadService` implements it over its private map, so the helpers stay free of the
 * queue's internals.
 */
export interface UploadHost {
  getItem(uploadId: string): UploadItem | undefined;
  update(uploadId: string, updates: Partial<UploadItem>): void;
  emit(type: UploadEventType, uploadId: string, data?: unknown): void;
}
