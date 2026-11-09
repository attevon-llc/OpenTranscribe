/**
 * Progress frames: which inbound realtime frames become a progressive notification
 * card, and the card fields (progressId, status, step text, progress) a frame carries.
 *
 * Pure: the caller passes the current `Translate` (`get(t)`) and folds the result into
 * the notification list.
 */
import type { Translate } from '$lib/i18n';
import { formatEta } from '$lib/utils/formatting';
import { llmBudgetMessage } from '$lib/notifications/titles';

// Notification ETA text; undefined (not '') when there is no ETA to show.
export function formatEtaSeconds(seconds: number | null | undefined): string | undefined {
  return formatEta(seconds) || undefined;
}

// Admin task type to progressId mapping
export const ADMIN_TASK_PROGRESS_IDS: Record<string, string> = {
  reindex_progress: 'admin_reindex',
  reindex_complete: 'admin_reindex',
  reindex_stopped: 'admin_reindex',
  migration_progress: 'admin_migration',
  migration_complete: 'admin_migration',
  migration_finalized: 'admin_migration',
  clustering_progress: 'admin_clustering',
  clustering_complete: 'admin_clustering',
  attribute_migration_progress: 'admin_attribute_migration',
  attribute_migration_complete: 'admin_attribute_migration',
  data_integrity_progress: 'admin_data_integrity',
  data_integrity_complete: 'admin_data_integrity',
  embedding_consistency_progress: 'admin_embedding_consistency',
  embedding_consistency_complete: 'admin_embedding_consistency',
};

// Admin task completion types
const ADMIN_COMPLETION_TYPES = new Set([
  'reindex_complete',
  'reindex_stopped',
  'migration_complete',
  'migration_finalized',
  'clustering_complete',
  'attribute_migration_complete',
  'data_integrity_complete',
  'embedding_consistency_complete',
]);

// Per-file task frames that update one card per file (or per playlist).
const PROGRESSIVE_TYPES = new Set([
  'transcription_status',
  'summarization_status',
  'redaction_status',
  'topic_extraction_status',
  'auto_label_status',
  'youtube_processing_status',
  'playlist_processing_status',
]);

// Silent notifications (gallery-only updates, internal state events)
const SILENT_TYPES = new Set([
  'file_created',
  'file_updated',
  'clustering_file_complete',
  // Tells the open file view its transcript is readable; the reader sees the
  // transcript appear, which is the point — a toast about it would be noise.
  'transcript_ready',
]);

/** The `data` object of a progress frame; only the fields read here are typed. */
export interface ProgressFrameData {
  file_id?: unknown;
  playlist_id?: unknown;
  status?: string;
  message?: string;
  progress?: number;
  eta_seconds?: number | null;
  indexed_files?: number;
  total_files?: number;
  stats?: { indexed_files?: number; total_files?: number };
  processed_files?: number;
  processed?: number;
  total?: number;
  step?: number;
  total_steps?: number;
  repaired?: number;
  [key: string]: unknown;
}

export type FrameKind = 'progress' | 'post_processing' | 'silent' | 'regular';

/** How the store files a frame that `routeFrame` did not consume. */
export function frameKind(type: string, data: ProgressFrameData): FrameKind {
  // Admin task types that should also appear in NotificationsPanel
  const isAdminProgressType = type in ADMIN_TASK_PROGRESS_IDS;
  if (
    (PROGRESSIVE_TYPES.has(type) && (data.file_id || type === 'playlist_processing_status')) ||
    isAdminProgressType
  ) {
    return 'progress';
  }
  if (type === 'post_processing_status') return 'post_processing';
  if (SILENT_TYPES.has(type)) return 'silent';
  return 'regular';
}

export interface ProgressFields {
  progressId: string;
  status: string;
  currentStep: string;
  progress: {
    current: number;
    total: number;
    percentage: number;
    etaSeconds: number | null;
    etaDisplay: string | undefined;
  };
}

function progressIdFor(type: string, data: ProgressFrameData): string {
  if (type in ADMIN_TASK_PROGRESS_IDS) return ADMIN_TASK_PROGRESS_IDS[type];
  if (type === 'playlist_processing_status') {
    return `playlist_processing_${data.playlist_id || 'unknown'}`;
  }
  return `${type}_${data.file_id}`;
}

// Admin completion types map to 'completed'
function statusFor(type: string, data: ProgressFrameData): string {
  if (ADMIN_COMPLETION_TYPES.has(type)) return 'completed';
  if (type in ADMIN_TASK_PROGRESS_IDS) return 'processing';
  return data.status || 'processing';
}

// "processed N / M files" wording shared by the migration task families.
function processedStep(data: ProgressFrameData, completed: boolean, translate: Translate): string {
  const count = data.processed_files ?? 0;
  const total = data.total_files ?? 0;
  return completed
    ? translate('notifications.progress.processedComplete', { count })
    : translate('notifications.progress.processedProgress', { count, total });
}

type StepBuilder = (data: ProgressFrameData, completed: boolean, translate: Translate) => string;

// Step text for admin tasks whose frames carry no message, keyed by task-type prefix.
const ADMIN_STEP_BUILDERS: ReadonlyArray<readonly [string, StepBuilder]> = [
  [
    'reindex',
    (data, completed, translate) => {
      const count = data.indexed_files ?? data.stats?.indexed_files ?? 0;
      const total = data.total_files ?? data.stats?.total_files ?? 0;
      return completed
        ? translate('notifications.progress.indexedComplete', { count })
        : translate('notifications.progress.indexedProgress', { count, total });
    },
  ],
  ['migration', processedStep],
  [
    'clustering',
    (data, completed, translate) => {
      const step = data.step ?? 0;
      const total = data.total_steps ?? 0;
      return completed
        ? translate('notifications.progress.clusteringComplete')
        : translate('notifications.progress.clusteringStep', { step, total });
    },
  ],
  ['attribute_migration', processedStep],
  [
    'data_integrity',
    (data, completed, translate) => {
      const count = data.processed_files ?? data.processed ?? 0;
      const total = data.total_files ?? data.total ?? 0;
      return completed
        ? translate('notifications.progress.checkedComplete', { count })
        : translate('notifications.progress.checkedProgress', { count, total });
    },
  ],
  [
    'embedding_consistency',
    (data, completed, translate) => {
      const count = data.processed_files ?? 0;
      const total = data.total_files ?? 0;
      return completed
        ? translate('notifications.progress.repairedComplete', { count: data.repaired ?? 0 })
        : translate('notifications.progress.repairingProgress', { count, total });
    },
  ],
];

function currentStepFor(
  type: string,
  data: ProgressFrameData,
  status: string,
  translate: Translate
): string {
  const given = llmBudgetMessage(data, translate) || data.message || '';
  if (given) return given;
  if (type in ADMIN_TASK_PROGRESS_IDS) {
    const builder = ADMIN_STEP_BUILDERS.find(([prefix]) => type.startsWith(prefix));
    const synthesized = builder?.[1](data, status === 'completed', translate);
    if (synthesized) return synthesized;
  }
  return translate('notifications.progress.processing');
}

/** The card fields of a frame `frameKind` filed as `progress`. */
export function progressFields(
  type: string,
  data: ProgressFrameData,
  translate: Translate
): ProgressFields {
  const status = statusFor(type, data);

  // Normalize progress: admin tasks send 0.0-1.0, others send 0-100
  let rawProgress = data.progress || 0;
  if (type in ADMIN_TASK_PROGRESS_IDS && rawProgress <= 1 && rawProgress > 0) {
    rawProgress = rawProgress * 100;
  }

  const etaRaw = data.eta_seconds ?? null;
  return {
    progressId: progressIdFor(type, data),
    status,
    currentStep: currentStepFor(type, data, status, translate),
    progress: {
      current: Math.floor(rawProgress),
      total: 100,
      percentage: Math.round(rawProgress),
      etaSeconds: etaRaw,
      etaDisplay: formatEtaSeconds(etaRaw),
    },
  };
}
