/**
 * Applying live updates (WebSocket notifications, upload completion) to the gallery's loaded files.
 *
 * `applyNotificationBatch` folds a debounced batch into the id-keyed file map WITHOUT touching
 * Svelte state per notification (a bulk run used to freeze the page); the page rebuilds its array
 * once from the result. `createNotificationBatcher` owns the dedupe, the 250 ms debounce and the
 * upload-store watcher; `createThrottledRefresher` the keyed, trailing refetch.
 */
import { websocketStore } from '$stores/websocket';
import { uploadsStore } from '$stores/uploads';
import { apiCache } from '$lib/apiCache';
import { markHeld } from '$lib/legalHold/summary';
import type { MediaFile } from '$lib/types/media';

type Translate = (key: string, options?: Record<string, unknown>) => string;

interface NotificationPayload {
  file_id?: string | number;
  status?: string;
  display_status?: string;
  thumbnail_url?: string;
  scope?: string;
  file?: Partial<MediaFile>;
}

export interface GalleryNotification {
  id: string;
  type: string;
  data?: NotificationPayload;
}

export interface BatchHandlers {
  removeFile: (fileId: string) => void;
  addNewFile: (file: MediaFile) => void;
  refresh: (key: string, delay: number) => void;
}

const STATUS_LABEL_KEYS: Record<string, string> = {
  processing: 'common.processing',
  completed: 'common.completed',
  pending: 'common.pending',
  error: 'common.error',
};

/** Fold one `transcription_status` frame into the map; returns what changed. */
function applyStatus(
  data: NotificationPayload,
  fileMap: Map<string, MediaFile>,
  t: Translate
): Effects {
  const fileId = String(data.file_id);
  const existing = fileMap.get(fileId);
  let changed = false;
  if (existing) {
    const labelKey = data.status ? STATUS_LABEL_KEYS[data.status] : undefined;
    fileMap.set(fileId, {
      ...existing,
      status: data.status as MediaFile['status'],
      display_status: labelKey ? t(labelKey) : data.status,
    });
    changed = true;
  }
  return { changed, refresh: data.status === 'error' };
}

/** Fold one `file_updated` frame into the map. A file outside the loaded pages is ignored silently. */
function applyUpdated(data: NotificationPayload, fileMap: Map<string, MediaFile>): boolean {
  const fileId = String(data.file_id);
  const existing = fileMap.get(fileId);
  if (!existing) return false;
  fileMap.set(
    fileId,
    data.file
      ? { ...existing, ...data.file }
      : {
          ...existing,
          status: (data.status as MediaFile['status']) || existing.status,
          display_status: data.display_status || existing.display_status,
          thumbnail_url: data.thumbnail_url || existing.thumbnail_url,
        }
  );
  return true;
}

interface Effects {
  changed?: boolean;
  refresh?: boolean;
}
type FrameHandler = (
  data: NotificationPayload,
  fileMap: Map<string, MediaFile>,
  t: Translate,
  handlers: BatchHandlers
) => Effects | void;

/** One handler per notification type; a type with no entry is ignored. */
const FRAME_HANDLERS: Record<string, FrameHandler> = {
  file_deleted: (data, _map, _t, handlers) => {
    if (data.file_id) handlers.removeFile(String(data.file_id));
  },
  transcription_status: (data, map, t) => (data.file_id ? applyStatus(data, map, t) : undefined),
  file_updated: (data, map) => (data.file_id ? { changed: applyUpdated(data, map) } : undefined),
  file_created: (data, _map, _t, handlers) => addOrRefresh(data, handlers),
  file_upload: (data, _map, _t, handlers) => addOrRefresh(data, handlers),
  // Push-based cache invalidation from the backend.
  cache_invalidate: (data, _map, _t, handlers) => {
    if (data.scope === 'files' || data.scope === 'all') {
      apiCache.invalidate('files:');
      handlers.refresh('cache-invalidate', 500);
    }
  },
};

function addOrRefresh(data: NotificationPayload, handlers: BatchHandlers): void {
  if (data.file) handlers.addNewFile(data.file as MediaFile);
  else handlers.refresh('new-file', 500);
}

/** Fold a batch into `fileMap`; returns whether any loaded file changed. */
export function applyNotificationBatch(
  notifications: GalleryNotification[],
  fileMap: Map<string, MediaFile>,
  t: Translate,
  handlers: BatchHandlers
): boolean {
  let filesUpdated = false;
  let needsRefresh = false;
  for (const { type, data } of notifications) {
    const effects = FRAME_HANDLERS[type]?.(data ?? {}, fileMap, t, handlers);
    filesUpdated ||= !!effects?.changed;
    needsRefresh ||= !!effects?.refresh;
  }
  if (needsRefresh) handlers.refresh('error-batch', 1000);
  return filesUpdated;
}

/**
 * Subscribe to WebSocket notifications (deduped by id, flushed as one batch after 250 ms of quiet)
 * and to the upload store (a drop to zero active uploads schedules a refresh). Returns the stop.
 */
export function createNotificationBatcher(
  onBatch: (batch: GalleryNotification[]) => void,
  onUploadsFinished: () => void
): () => void {
  const processedIds = new Set<string>();
  let pending: GalleryNotification[] = [];
  let flushTimer: ReturnType<typeof setTimeout> | null = null;
  let previousActiveUploads = 0;

  // Keep the id set from growing without bound over a long session.
  const cleanupInterval = setInterval(() => {
    if (processedIds.size > 1000) processedIds.clear();
  }, 300000);

  const flush = () => {
    flushTimer = null;
    if (pending.length === 0) return;
    const batch = pending;
    pending = [];
    onBatch(batch);
  };

  const unsubscribeWs = websocketStore.subscribe(($ws) => {
    const fresh = $ws.notifications.filter((n) => !processedIds.has(n.id));
    if (fresh.length === 0) return;
    fresh.forEach((n) => processedIds.add(n.id));
    pending.push(...fresh);
    if (flushTimer) clearTimeout(flushTimer);
    flushTimer = setTimeout(flush, 250);
  });

  const unsubscribeUploads = uploadsStore.subscribe(($uploads) => {
    const active = $uploads.uploads.filter((u) =>
      ['uploading', 'processing', 'preparing'].includes(u.status)
    ).length;
    if (previousActiveUploads > 0 && active === 0) onUploadsFinished();
    previousActiveUploads = active;
  });

  return () => {
    unsubscribeWs();
    unsubscribeUploads();
    processedIds.clear();
    clearInterval(cleanupInterval);
    if (flushTimer) clearTimeout(flushTimer);
    flushTimer = null;
  };
}

/** Keyed trailing refetch: calls for the same key within `delay` collapse into one. */
export function createThrottledRefresher(run: () => void) {
  const timeouts = new Map<string, ReturnType<typeof setTimeout>>();
  return {
    refresh(key: string, delay = 500) {
      const existing = timeouts.get(key);
      if (existing) clearTimeout(existing);
      timeouts.set(
        key,
        setTimeout(() => {
          run();
          timeouts.delete(key);
        }, delay)
      );
    },
    cancelAll() {
      timeouts.forEach((id) => clearTimeout(id));
      timeouts.clear();
    },
  };
}

/** Pagination state the totals helpers read (a slice of the gallery store). */
export interface PageTotals {
  currentPage: number;
  pageSize: number;
  totalFiles: number;
  totalPages: number;
  hasMoreFiles: boolean;
}

/** Totals after one file arrived: on page 1 it was prepended, elsewhere it only raises the count. */
export function totalsAfterAdd(s: PageTotals) {
  const total = s.totalFiles + 1;
  return s.currentPage === 1
    ? {
        page: s.currentPage,
        pageSize: s.pageSize,
        total,
        totalPages: s.totalPages,
        hasMore: s.hasMoreFiles,
      }
    : // A new file means there might be more.
      {
        page: s.currentPage,
        pageSize: s.pageSize,
        total,
        totalPages: Math.ceil(total / s.pageSize),
        hasMore: true,
      };
}

/** Totals after `removed` files left the list. */
export function totalsAfterRemove(s: PageTotals, removed: number) {
  const total = Math.max(0, s.totalFiles - removed);
  const totalPages = Math.ceil(total / s.pageSize);
  return {
    page: s.currentPage,
    pageSize: s.pageSize,
    total,
    totalPages,
    hasMore: s.currentPage < totalPages,
  };
}

/** Rows for `uuids` flagged quarantined, in place (a quarantined row legitimately stays in an admin's gallery, §B.6 E5). */
export function markQuarantinedRows(files: MediaFile[], uuids: string[]): MediaFile[] {
  const changed = new Set(uuids);
  return files.map((f) => (changed.has(f.uuid) ? { ...f, is_quarantined: true } : f));
}

/** Rows for `uuids` badged with a hold placed from the selection (legal-hold plan, Lane D). */
export function markHeldRows(
  files: MediaFile[],
  uuids: string[],
  authority: 'organization' | 'platform'
): MediaFile[] {
  const held = new Set(uuids);
  return files.map((f) => (held.has(f.uuid) ? { ...f, hold: markHeld(f.hold, [authority]) } : f));
}
