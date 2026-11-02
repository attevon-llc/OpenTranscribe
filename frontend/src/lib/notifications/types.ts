/**
 * The notification model the realtime store holds and the notification panel renders.
 * Re-exported by `$stores/websocket`, which is where every consumer imports it from.
 */
import type { StepMark } from '$lib/notifications/runTiming';
import type { PostProcessingRun } from '$lib/notifications/postProcessing';

// Define notification types
// The runtime list is the source of truth so a test can prove the retention
// map ($lib/notifications/retention) classifies every type.
export const NOTIFICATION_TYPES = [
  'transcription_status',
  'transcript_ready',
  'summarization_status',
  'redaction_status',
  'topic_extraction_status',
  'auto_label_status',
  'youtube_processing_status',
  'playlist_processing_status',
  'analytics_status',
  'download_progress',
  'audio_extraction_status',
  'connection_established',
  'echo',
  'file_upload',
  'file_created',
  'file_updated',
  'file_deleted',
  'speaker_updated',
  'speaker_processing_complete',
  'speaker_rename_propagation',
  'gpu_stats_update',
  'reindex_progress',
  'reindex_complete',
  'reindex_stopped',
  'migration_progress',
  'migration_complete',
  'migration_finalized',
  'clustering_progress',
  'clustering_complete',
  'clustering_file_complete',
  'attribute_migration_progress',
  'attribute_migration_complete',
  'data_integrity_progress',
  'data_integrity_complete',
  'embedding_consistency_progress',
  'embedding_consistency_complete',
  'cache_invalidate',
  'collection_shared',
  'collection_share_revoked',
  'collection_share_updated',
  'collection_access_changed',
  'group_member_added',
  'group_member_removed',
  'post_processing_status',
  'file_takedown',
  'file_takedown_released',
] as const;
export type NotificationType = (typeof NOTIFICATION_TYPES)[number];

/**
 * Payload attached to a {@link Notification}. Carries the `data` object from the
 * backend WebSocket message. The known fields below are read by consumers (file
 * detail page, settings panels); the index signature keeps forward-compatibility
 * with task-specific extra fields.
 */
export interface NotificationData {
  file_id?: string;
  file_uuid?: string;
  filename?: string;
  reason?: string;
  abuse_contact_email?: string;
  status?: string;
  message?: string;
  progress?: number;
  summary?: string;
  auto_applied_count?: number;
  suggested_count?: number;
  skipped_detectors?: string[];
  scope?: string;
  eta_seconds?: number | null;
  // How long the just-finished task actually ran, in seconds — issue #753's
  // duration chip. Sent by the backend only on a terminal transcription_status
  // / summarization_status / topic_extraction_status frame; absent otherwise.
  // NEVER the same number as a recording's length (that's `file_updated`'s
  // `duration`/`formatted_duration` — a different, unrelated field).
  duration_seconds?: number;
  // transcription_status processing frames: the sub-stage the backend filed this frame
  // under (backend `TranscriptionStage`; keys in $lib/notifications/processingJob
  // TRANSCRIPTION_STAGES). Absent on terminal frames and older backends.
  stage?: string;
  [key: string]: unknown;
}

// Notification interface
export interface Notification {
  id: string;
  type: NotificationType;
  title: string;
  message: string;
  timestamp: Date;
  read: boolean;
  // When it was marked read; the retention clock for read items (issue #1217).
  readAt?: Date;
  data?: NotificationData;
  // Progressive notification fields
  progressId?: string; // Used to group progressive notifications
  currentStep?: string; // Current processing step
  progress?: {
    current: number;
    total: number;
    percentage: number;
    etaSeconds?: number | null;
    etaDisplay?: string;
  };
  status?: 'processing' | 'completed' | 'error';
  dismissible?: boolean; // false while processing
  silent?: boolean; // if true, don't show in notification panel (for gallery-only updates)
  // Run timing for the consolidated processing card ($lib/notifications/runTiming), epoch ms.
  startedAt?: number;
  finishedAt?: number;
  stepLog?: StepMark[];
  // post_processing_status records only: the file's post-processing run
  // ($lib/notifications/postProcessing).
  postProcessing?: PostProcessingRun;
}
