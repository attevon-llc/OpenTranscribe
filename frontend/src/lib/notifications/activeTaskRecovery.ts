/**
 * Rebuild admin/background task progress after a (re)connect.
 *
 * `GET /tasks/progress/active` lists the tasks still running, so a page refresh or a
 * dropped socket does not lose their progress card or leave a settings panel showing
 * "idle" while the task runs. Pure: the store fetches, dispatches and folds.
 */
import type { Notification, NotificationType } from '$lib/notifications/types';
import { ADMIN_TASK_PROGRESS_IDS, formatEtaSeconds } from '$lib/notifications/progressFrame';

/** One row of `GET /tasks/progress/active`. */
export interface ActiveTask {
  task_type: string;
  user_id: number;
  total: number;
  processed: number;
  status: string;
  message: string;
  eta_seconds: number | null;
}

// Task type to notification type mapping
const TASK_NOTIFICATION_TYPES: Record<string, string> = {
  reindex: 'reindex_progress',
  migration: 'migration_progress',
  attribute_migration: 'attribute_migration_progress',
  clustering: 'clustering_progress',
  auto_label: 'auto_label_status',
  data_integrity: 'data_integrity_progress',
  embedding_consistency: 'embedding_consistency_progress',
};

// Task type to the window event its settings component listens for
const TASK_EVENTS: Record<string, string> = {
  reindex: 'reindex-progress',
  migration: 'migration-progress',
  attribute_migration: 'attribute-migration-progress',
  clustering: 'clustering-progress',
  auto_label: 'auto-label-status',
  data_integrity: 'data-integrity-progress',
  embedding_consistency: 'embedding-consistency-progress',
};

export interface RecoveredTask {
  notificationType: string;
  progressId: string;
  percentage: number;
  etaDisplay: string | undefined;
  /** Window event for the task's settings component, or null when it has none. */
  event: { name: string; detail: Record<string, unknown> } | null;
}

/** The card and event a running task maps to, or null for a task type nobody shows. */
export function recoverTask(task: ActiveTask): RecoveredTask | null {
  const notificationType = TASK_NOTIFICATION_TYPES[task.task_type];
  if (!notificationType) return null;

  const progressId =
    ADMIN_TASK_PROGRESS_IDS[notificationType] || `${notificationType}_retroactive_apply`;

  const percentage = task.total > 0 ? Math.round((task.processed / task.total) * 100) : 0;
  const etaDisplay = formatEtaSeconds(task.eta_seconds);

  const eventName = TASK_EVENTS[task.task_type];
  const event = eventName
    ? {
        name: eventName,
        detail: {
          progress: task.total > 0 ? task.processed / task.total : 0,
          processed: task.processed,
          total: task.total,
          processed_files: task.processed,
          total_files: task.total,
          indexed_files: task.processed,
          message: task.message,
          eta_seconds: task.eta_seconds,
          running: true,
          status: 'processing',
        },
      }
    : null;

  return { notificationType, progressId, percentage, etaDisplay, event };
}

/** The progress card for a recovered task; `id` keeps an existing card's identity. */
export function recoveredNotification(
  task: ActiveTask,
  recovered: RecoveredTask,
  id: string,
  title: string
): Notification {
  const { notificationType, progressId, percentage, etaDisplay } = recovered;
  return {
    id,
    progressId,
    type: notificationType as NotificationType,
    title,
    message: task.message,
    timestamp: new Date(),
    read: false,
    data: {
      progress: percentage,
      processed: task.processed,
      total: task.total,
      eta_seconds: task.eta_seconds,
    },
    currentStep: task.message,
    progress: {
      current: percentage,
      total: 100,
      percentage,
      etaSeconds: task.eta_seconds,
      etaDisplay,
    },
    status: 'processing',
    dismissible: false,
  };
}
