/**
 * Progress frames: which inbound realtime frames become a progressive notification
 * card, and the card fields (progressId, status, step text, progress) a frame carries.
 *
 * Pure: the caller passes the current `Translate` (`get(t)`) and folds the result into
 * the notification list.
 */
import type { Translate } from '$lib/i18n';
import { formatEta } from '$lib/utils/formatting';

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

/** The card fields of a frame `frameKind` filed as `progress`. */
export function progressFields(
  type: string,
  data: ProgressFrameData,
  translate: Translate
): ProgressFields {
  const isAdminProgressType = type in ADMIN_TASK_PROGRESS_IDS;

  // Determine progressId
  let progressId: string;
  if (isAdminProgressType) {
    progressId = ADMIN_TASK_PROGRESS_IDS[type];
  } else if (type === 'playlist_processing_status') {
    progressId = `playlist_processing_${data.playlist_id || 'unknown'}`;
  } else {
    progressId = `${type}_${data.file_id}`;
  }

  // Determine status — admin completion types map to 'completed'
  let status: string;
  if (ADMIN_COMPLETION_TYPES.has(type)) {
    status = 'completed';
  } else if (isAdminProgressType) {
    status = 'processing';
  } else {
    status = data.status || 'processing';
  }

  // Synthesize message for admin tasks that don't have one
  let currentStep = data.message || '';
  if (!currentStep && isAdminProgressType) {
    if (type.startsWith('reindex')) {
      const idx = data.indexed_files ?? data.stats?.indexed_files ?? 0;
      const tot = data.total_files ?? data.stats?.total_files ?? 0;
      currentStep =
        status === 'completed'
          ? translate('notifications.progress.indexedComplete', { count: idx })
          : translate('notifications.progress.indexedProgress', {
              count: idx,
              total: tot,
            });
    } else if (type.startsWith('migration')) {
      const proc = data.processed_files ?? 0;
      const tot = data.total_files ?? 0;
      currentStep =
        status === 'completed'
          ? translate('notifications.progress.processedComplete', { count: proc })
          : translate('notifications.progress.processedProgress', {
              count: proc,
              total: tot,
            });
    } else if (type.startsWith('clustering')) {
      const step = data.step ?? 0;
      const tot = data.total_steps ?? 0;
      currentStep =
        status === 'completed'
          ? translate('notifications.progress.clusteringComplete')
          : translate('notifications.progress.clusteringStep', { step, total: tot });
    } else if (type.startsWith('attribute_migration')) {
      const proc = data.processed_files ?? 0;
      const tot = data.total_files ?? 0;
      currentStep =
        status === 'completed'
          ? translate('notifications.progress.processedComplete', { count: proc })
          : translate('notifications.progress.processedProgress', {
              count: proc,
              total: tot,
            });
    } else if (type.startsWith('data_integrity')) {
      const proc = data.processed_files ?? data.processed ?? 0;
      const tot = data.total_files ?? data.total ?? 0;
      currentStep =
        status === 'completed'
          ? translate('notifications.progress.checkedComplete', { count: proc })
          : translate('notifications.progress.checkedProgress', {
              count: proc,
              total: tot,
            });
    } else if (type.startsWith('embedding_consistency')) {
      const proc = data.processed_files ?? 0;
      const tot = data.total_files ?? 0;
      currentStep =
        status === 'completed'
          ? translate('notifications.progress.repairedComplete', {
              count: data.repaired ?? 0,
            })
          : translate('notifications.progress.repairingProgress', {
              count: proc,
              total: tot,
            });
    }
  }
  if (!currentStep) currentStep = translate('notifications.progress.processing');

  // Normalize progress: admin tasks send 0.0-1.0, others send 0-100
  let rawProgress = data.progress || 0;
  if (isAdminProgressType && rawProgress <= 1 && rawProgress > 0) {
    rawProgress = rawProgress * 100;
  }

  // Read ETA from data
  const etaRaw = data.eta_seconds ?? null;
  const etaDisplay = formatEtaSeconds(etaRaw);

  const progress = {
    current: Math.floor(rawProgress),
    total: 100,
    percentage: Math.round(rawProgress),
    etaSeconds: etaRaw,
    etaDisplay,
  };

  return { progressId, status, currentStep, progress };
}
