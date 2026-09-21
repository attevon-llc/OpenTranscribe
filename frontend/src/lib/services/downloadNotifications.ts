/**
 * Bridges `$stores/downloads` (in-memory, download-tray state) into the
 * persistent bell (`$stores/websocket`) as a `download_progress` notification —
 * one row per file, deduped on `progressId`. Issue #569.
 *
 * Design rationale lives in the v0.6.0 app-chrome plan, §3.2. The two points
 * worth restating here because a future reader of just this file would not
 * otherwise know them:
 *
 * - **`dismissible` is always `true`**, diverging from the repo's
 *   `false`-while-processing convention (`websocket.ts`'s other progressive
 *   notifications). This is a client-side download with no server to correct
 *   a dead SSE stream — an undismissable row would be stuck forever once the
 *   stream failed silently, because `markAsRead`/`removeNotification` only
 *   auto-regenerate rows that are BOTH `processing` AND non-dismissible.
 * - **A `downloadStore` key disappearing is ignored, unconditionally.** On
 *   logout, `clearUserState.ts` runs `websocketStore.clearAll()` (via
 *   `notificationsPanel`) and `downloadStore.reset()` concurrently — a flat
 *   array of `Promise.allSettled` entries, no ordering guarantee — so a
 *   vanished key could mean "cleaned up 30s after success" or "session torn
 *   down". Treating both the same and doing nothing is correct either way;
 *   there is no notification-list state to react to.
 */
import { get } from 'svelte/store';
import { downloadStore, type DownloadState, type DownloadType } from '$stores/downloads';
import { websocketStore } from '$stores/websocket';
import { t } from '$stores/locale';

const PROGRESS_ID_PREFIX = 'download-';

function progressIdFor(fileId: string): string {
  return `${PROGRESS_ID_PREFIX}${fileId}`;
}

/** Collapses the store's 5 fine-grained states onto the notification's 3. */
function toNotificationStatus(
  status: DownloadState['status']
): 'processing' | 'completed' | 'error' {
  if (status === 'completed') return 'completed';
  if (status === 'error') return 'error';
  return 'processing'; // preparing, processing, downloading
}

function toPercentage(state: DownloadState): number {
  if (state.status === 'completed') return 100;
  return state.progress ?? 0;
}

/**
 * Per-type message key for the "in progress" phases. Reuses the 16
 * `downloads.*` strings that already exist, fully translated, in all 12
 * locales — they were dead (nothing called them outside `downloads.ts`'s own
 * toasts) until this bridge. Zero new translations needed for #569 (J15).
 */
function activityMessageKey(downloadType: DownloadType, phase: 'preparing' | 'active'): string {
  if (phase === 'preparing') {
    switch (downloadType) {
      case 'audio':
        return 'downloads.preparingAudio';
      case 'video_with_subtitles':
        return 'downloads.preparingWithSubtitles';
      default:
        return 'downloads.preparingDownload';
    }
  }
  switch (downloadType) {
    case 'audio':
      return 'downloads.extractingAudio';
    case 'video_with_subtitles':
      return 'downloads.addingSubtitles';
    default:
      // No dedicated "processing" copy exists for a plain video download with
      // no subtitle burn-in; the preparing message stays accurate long enough
      // for the progress bar (rendered from `progress.percentage`) to carry
      // the rest of the signal.
      return 'downloads.preparingDownload';
  }
}

function buildMessage(state: DownloadState): string {
  const translate = get(t);
  switch (state.status) {
    case 'preparing':
      return translate(activityMessageKey(state.downloadType, 'preparing'), {
        filename: state.filename,
      });
    case 'processing':
    case 'downloading':
      return translate(activityMessageKey(state.downloadType, 'active'), {
        filename: state.filename,
      });
    case 'completed':
      return translate('downloads.downloadedSuccessfully', { filename: state.filename });
    case 'error':
      return translate('downloads.downloadFailedError', {
        error: state.error || translate('downloads.unknownError'),
      });
  }
}

function pushNotificationFor(fileId: string, state: DownloadState): void {
  const translate = get(t);
  const status = toNotificationStatus(state.status);
  websocketStore.addNotification({
    type: 'download_progress',
    title: translate('notifications.downloadProgress'),
    message: buildMessage(state),
    progressId: progressIdFor(fileId),
    progress: {
      current: toPercentage(state),
      total: 100,
      percentage: toPercentage(state),
    },
    status,
    dismissible: true,
    silent: false,
    // `status` here (not just top-level) is what `getNotificationStatus`
    // (`NotificationsPanel.svelte:192-213`) actually reads — without it every
    // row renders uncoloured, the same defect audio-extraction notifications
    // already ship with (N2). `file_id` also lights up the "view file" link.
    data: { status, file_id: fileId, filename: state.filename },
  });
}

let unsubscribe: (() => void) | null = null;

/**
 * Boot-time reconcile (J16). `downloadStore` is in-memory only and is never
 * persisted, but the bell's notification list IS persisted to `localStorage`.
 * After a reload, `downloadStore` always restarts empty, so any
 * `download_progress` row still `processing` from before the reload can never
 * receive another real event. Left alone it is a permanent spinner; this
 * converts it into a dismissible error row instead. `dismissible` was already
 * `true` for these rows (J14), so only the "still working" visual claim needs
 * correcting, not the ability to close it.
 */
function reconcileStaleDownloadNotifications(): void {
  const translate = get(t);
  const { notifications } = get(websocketStore);
  for (const notification of notifications) {
    if (
      notification.type === 'download_progress' &&
      notification.progressId?.startsWith(PROGRESS_ID_PREFIX) &&
      notification.status === 'processing'
    ) {
      websocketStore.updateNotification(notification.progressId, {
        status: 'error',
        message: translate('downloads.downloadFailedError', {
          error: translate('downloads.unknownError'),
        }),
        data: { ...notification.data, status: 'error' },
      });
    }
  }
}

/**
 * Start bridging `downloadStore` transitions into the bell. Safe to call more
 * than once (e.g. hot-reload) — subsequent calls are no-ops while already
 * subscribed. Call once at app boot (`+layout.svelte`); no logout teardown is
 * needed because `downloadStore.reset()` on logout empties the very state
 * this bridge mirrors, so the next tick simply has nothing to iterate.
 */
export function initDownloadNotifications(): void {
  if (unsubscribe) return;

  reconcileStaleDownloadNotifications();

  unsubscribe = downloadStore.subscribe((downloads) => {
    for (const [fileId, state] of Object.entries(downloads)) {
      pushNotificationFor(fileId, state);
    }
  });
}
