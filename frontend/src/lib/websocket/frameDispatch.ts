/**
 * What the socket does with the frames it receives and with the active tasks the backend
 * reports on (re)connect: route each frame to the download store, the window-event/cache
 * router, or the notification list, and rebuild the progress cards of tasks already running.
 */
import { get } from 'svelte/store';
import { downloadStore, type DownloadState } from '$stores/downloads';
import { t } from '$stores/locale';
import axiosInstance from '$lib/axios';
import { routeFrame } from '$lib/websocket/frameRouting';
import { frameKind, progressFields } from '$lib/notifications/progressFrame';
import { recoverTask, type ActiveTask } from '$lib/notifications/activeTaskRecovery';
import type { createNotificationActions } from '$stores/websocketNotifications';

type NotificationActions = ReturnType<typeof createNotificationActions>;

interface DownloadProgressFrame {
  data: {
    file_id?: string;
    status: DownloadState['status'];
    progress?: number;
    error?: string;
  };
}

// Handle download progress messages (legacy/global). Media downloads initiated from the file
// page are delivered over a dedicated SSE stream — see TranscriptDisplay's EventSource — so
// this only mirrors status into the store.
function handleDownloadProgress(data: DownloadProgressFrame): void {
  const { file_id, status, progress, error } = data.data;
  if (file_id) {
    downloadStore.updateStatus(file_id, status, progress, error);
  }
}

/**
 * Parse and dispatch one raw socket message. A malformed or unhandled frame is logged, never
 * thrown, so one bad frame cannot tear down the socket.
 */
export function dispatchMessage(raw: string, notifications: NotificationActions): void {
  try {
    const data = JSON.parse(raw);

    if (data.type === 'download_progress') {
      handleDownloadProgress(data);
      return;
    }

    // Window events and cache invalidation ($lib/websocket/frameRouting).
    if (routeFrame(data)) return;

    // Progress cards, post-processing, silent and regular notifications
    // ($lib/notifications/progressFrame).
    const kind = frameKind(data.type, data.data);

    if (kind === 'progress') {
      const fields = progressFields(data.type, data.data, get(t));
      notifications.foldProgressFrame(data.type, data.data, fields);
    } else if (kind === 'post_processing') {
      notifications.applyPostProcessing(data.data);
    } else if (kind === 'silent') {
      notifications.addSilentFrame(data.type, data.data);
    } else {
      notifications.addRegularFrame(data.type, data.data);
    }
  } catch (error) {
    console.error('Error processing WebSocket message:', error);
  }
}

/**
 * Recover active task progress from the backend on connect/reconnect, so a page refresh or a
 * closed modal does not lose a running task's card ($lib/notifications/activeTaskRecovery).
 * Best-effort: a failure never breaks the socket.
 */
export async function recoverActiveProgress(notifications: NotificationActions): Promise<void> {
  try {
    const { data: activeTasks } = await axiosInstance.get<ActiveTask[]>('/tasks/progress/active');

    if (!activeTasks || activeTasks.length === 0) return;

    for (const task of activeTasks) {
      const recovered = recoverTask(task);
      if (!recovered) continue;

      // Dispatch CustomEvent for settings components
      if (typeof window !== 'undefined' && recovered.event) {
        window.dispatchEvent(
          new CustomEvent(recovered.event.name, { detail: recovered.event.detail })
        );
      }

      // Create/update progressive notification
      notifications.upsertRecoveredTask(task, recovered);
    }
  } catch {
    // Recovery is best-effort; don't break WebSocket on failure
  }
}
