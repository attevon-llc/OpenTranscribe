/**
 * Notification retention (issue #1217).
 *
 * The notification list is an INBOX, not a toast stream: retention is pruned by a
 * pure function at defined moments (app load, on add, panel close), never by a
 * timer and never while the panel is open, so an item cannot vanish under the
 * user's eyes (WCAG 2.2.1).
 *
 * Never auto-removed: errors and warnings, in-flight items, actionable or
 * system-notice types, and unread items younger than {@link UNREAD_RETENTION_MS}.
 */
import type { Notification, NotificationType } from '$stores/websocket';

export type RetentionClass = 'informational' | 'persistent';

/**
 * Every notification type, classified explicitly. `Record<NotificationType, ...>`
 * makes a new union member a compile error here, and the unit test asserts the
 * runtime key set matches `NOTIFICATION_TYPES`; anything that still slips through
 * (an unknown wire string) is treated as `persistent`, never auto-clearable.
 */
export const RETENTION_BY_TYPE: Record<NotificationType, RetentionClass> = {
  // Outcomes of work the user started; once seen they are history.
  transcription_status: 'informational',
  transcript_ready: 'informational',
  summarization_status: 'informational',
  redaction_status: 'informational',
  topic_extraction_status: 'informational',
  auto_label_status: 'informational',
  youtube_processing_status: 'informational',
  playlist_processing_status: 'informational',
  analytics_status: 'informational',
  audio_extraction_status: 'informational',
  speaker_processing_complete: 'informational',
  search_indexing_complete: 'informational',
  clustering_file_complete: 'informational',
  enrichment_started: 'informational',
  enrichment_task_complete: 'informational',
  // Gallery/connection chatter, mostly never shown as a panel row (silent).
  connection_established: 'informational',
  echo: 'informational',
  file_upload: 'informational',
  file_created: 'informational',
  file_updated: 'informational',
  file_deleted: 'informational',
  speaker_updated: 'informational',
  speaker_rename_propagation: 'informational',
  gpu_stats_update: 'informational',
  cache_invalidate: 'informational',
  // In-flight transfer state.
  download_progress: 'persistent',
  // Admin/system notices: the operator may need to find them later.
  reindex_progress: 'persistent',
  reindex_complete: 'persistent',
  reindex_stopped: 'persistent',
  migration_progress: 'persistent',
  migration_complete: 'persistent',
  migration_finalized: 'persistent',
  clustering_progress: 'persistent',
  clustering_complete: 'persistent',
  attribute_migration_progress: 'persistent',
  attribute_migration_complete: 'persistent',
  data_integrity_progress: 'persistent',
  data_integrity_complete: 'persistent',
  embedding_consistency_progress: 'persistent',
  embedding_consistency_complete: 'persistent',
  // Awaiting a user decision or carrying legal/sharing meaning.
  collection_shared: 'persistent',
  collection_share_revoked: 'persistent',
  collection_share_updated: 'persistent',
  group_member_added: 'persistent',
  group_member_removed: 'persistent',
  file_takedown: 'persistent',
  file_takedown_released: 'persistent',
};

export type RetentionSetting = 'read24h' | 'read7d' | 'never';
export const RETENTION_SETTINGS: readonly RetentionSetting[] = ['read24h', 'read7d', 'never'];
export const DEFAULT_RETENTION: RetentionSetting = 'read24h';

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
/** Read + successful items live this long after being marked read, per setting. */
export const READ_RETENTION_MS: Record<Exclude<RetentionSetting, 'never'>, number> = {
  read24h: 24 * HOUR_MS,
  read7d: 7 * DAY_MS,
};
/** Unread successful items were never seen; keep them long enough to be found. */
export const UNREAD_RETENTION_MS = 14 * DAY_MS;
export const MAX_NOTIFICATIONS = 100;

/**
 * True only for a finished, successful, informational item. Anything in flight,
 * failed, warned, of a persistent class, or of an unknown type is NOT clearable.
 */
export function isAutoClearable(n: Notification): boolean {
  const cls = (RETENTION_BY_TYPE as Record<string, RetentionClass | undefined>)[n.type];
  if (cls !== 'informational') return false;
  if (n.status !== undefined && n.status !== 'completed') return false;
  // The backend status can be richer than the typed union ('failed',
  // 'not_configured', 'pending', ...). Only an explicit success counts.
  const wire = n.data?.status;
  if (typeof wire === 'string' && wire !== 'completed' && wire !== 'success') return false;
  return true;
}

export interface PruneOptions {
  /** Injected clock (ms since epoch) so the function stays pure and testable. */
  now: number;
  retention: RetentionSetting;
  /** When true nothing is removed: deferred until the panel closes. */
  panelOpen: boolean;
}

export interface PruneResult {
  kept: Notification[];
  /** Visible (non-silent) rows removed, for the "N cleared" note. */
  removedVisible: number;
}

function toMs(d: Date | string | number | undefined): number {
  return d instanceof Date ? d.getTime() : new Date(d ?? Number.NaN).getTime();
}

/** Returns the notifications that survive retention. Never mutates the input. */
export function pruneNotifications(list: Notification[], opts: PruneOptions): PruneResult {
  if (opts.panelOpen) return { kept: list, removedVisible: 0 };

  const drop = new Set<Notification>();

  if (opts.retention !== 'never') {
    const readTtl = READ_RETENTION_MS[opts.retention];
    for (const n of list) {
      if (!isAutoClearable(n)) continue;
      if (n.read) {
        const readAt = toMs(n.readAt);
        // No/invalid readAt: the age cannot be proven, so keep it (fail safe).
        if (!Number.isNaN(readAt) && opts.now - readAt >= readTtl) drop.add(n);
      } else {
        const created = toMs(n.timestamp);
        if (!Number.isNaN(created) && opts.now - created >= UNREAD_RETENTION_MS) drop.add(n);
      }
    }
  }

  // Hard cap, applied in every mode (it bounds localStorage, it is not time-based).
  // Candidate order: invisible (silent) rows, then the oldest read informational,
  // then the oldest unread informational. Errors, warnings, in-flight and
  // actionable rows are never candidates, so they may exceed the cap.
  const survivors = list.filter((n) => !drop.has(n));
  let excess = survivors.length - MAX_NOTIFICATIONS;
  if (excess > 0) {
    const clearable = survivors.filter(isAutoClearable);
    const tiers = [
      clearable.filter((n) => n.silent),
      clearable.filter((n) => !n.silent && n.read),
      clearable.filter((n) => !n.silent && !n.read),
    ];
    for (const tier of tiers) {
      // The list is newest-first, so reversing gives oldest-first, and the stable
      // sort keeps that order for equal (or unparseable) timestamps.
      const oldestFirst = [...tier].reverse().sort((a, b) => {
        const d = toMs(a.timestamp) - toMs(b.timestamp);
        return Number.isNaN(d) ? 0 : d;
      });
      for (const n of oldestFirst) {
        if (excess <= 0) break;
        drop.add(n);
        excess -= 1;
      }
    }
  }

  if (drop.size === 0) return { kept: list, removedVisible: 0 };
  let removedVisible = 0;
  for (const n of drop) if (!n.silent) removedVisible += 1;
  return { kept: list.filter((n) => !drop.has(n)), removedVisible };
}
