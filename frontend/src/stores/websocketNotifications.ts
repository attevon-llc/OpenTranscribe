/**
 * The notification list held by `$stores/websocket`: persistence, retention, the
 * actions the panel and client-side sources call, and the folds of inbound frames into
 * cards. The socket itself (connect, reconnect, close codes) stays in `websocket.ts`;
 * this module only ever touches `state.notifications`.
 */
import { get, type Writable } from 'svelte/store';
import { t } from '$stores/locale';
import { generateId } from '$lib/utils/ids';
import { pruneNotifications } from '$lib/notifications/retention';
import {
  advanceRunTiming,
  isTerminalState,
  taskStateFromStatus,
} from '$lib/notifications/runTiming';
import {
  upsertPostProcessingRecord,
  type PostProcessingFrame,
} from '$lib/notifications/postProcessing';
import { notificationRetention, clearedNotice } from '$stores/notificationRetention';
import { showNotificationsPanel } from '$stores/notificationsPanel';
import type { Notification, NotificationData, NotificationType } from '$lib/notifications/types';
import { composeClientMessage, notificationTitle } from '$lib/notifications/titles';
import type { ProgressFields } from '$lib/notifications/progressFrame';
import {
  recoveredNotification,
  type ActiveTask,
  type RecoveredTask,
} from '$lib/notifications/activeTaskRecovery';
import type { ConnectionStatus } from '$lib/websocket/connectionPolicy';

// Store state interface
export interface WebSocketState {
  socket: WebSocket | null;
  status: ConnectionStatus;
  notifications: Notification[];
  reconnectAttempts: number;
  error: string | null;
}

/**
 * Apply the retention policy (issue #1217) to a list. Pure policy lives in
 * `$lib/notifications/retention`; this only supplies the clock, the user's
 * setting and the panel state, and tells the user later what was removed.
 * While the panel is open it removes nothing, so rows never vanish mid-read.
 */
function retain(list: Notification[]): Notification[] {
  const { kept, removedVisible } = pruneNotifications(list, {
    now: Date.now(),
    retention: get(notificationRetention),
    panelOpen: get(showNotificationsPanel),
  });
  clearedNotice.add(removedVisible);
  return kept;
}

// Load notifications from localStorage if available
export const loadNotificationsFromStorage = (): Notification[] => {
  if (typeof window !== 'undefined') {
    try {
      const stored = localStorage.getItem('notifications');
      if (stored) {
        const parsed: Array<Notification & { timestamp: string; readAt?: string }> =
          JSON.parse(stored);
        // Convert timestamp strings back to Date objects. A read item saved before
        // `readAt` existed starts its retention clock now rather than being
        // judged against a read time nobody recorded.
        const loadedAt = new Date();
        return retain(
          parsed.map((n) => ({
            ...n,
            timestamp: new Date(n.timestamp),
            readAt: n.readAt ? new Date(n.readAt) : n.read ? loadedAt : undefined,
          }))
        );
      }
    } catch (error) {
      console.warn('Failed to load notifications from localStorage:', error);
    }
  }
  return [];
};

// Save notifications to localStorage
const saveNotificationsToStorage = (notifications: Notification[]) => {
  if (typeof window !== 'undefined') {
    try {
      localStorage.setItem('notifications', JSON.stringify(notifications));
    } catch (error) {
      console.warn('Failed to save notifications to localStorage:', error);
    }
  }
};

/** Notification-list actions bound to the websocket store's `update`. */
export function createNotificationActions(update: Writable<WebSocketState>['update']) {
  // Mark notification as read (with auto-regeneration for processing notifications)
  const markAsRead = (id: string) => {
    update((state: WebSocketState) => {
      const index = state.notifications.findIndex((n: Notification) => n.id === id);
      if (index !== -1) {
        const notification = state.notifications[index];

        // If it's a processing notification, auto-regenerate it
        if (notification.status === 'processing' && !notification.dismissible) {
          // Mark as read but keep the notification
          state.notifications[index].read = true;
          state.notifications[index].readAt = new Date();
          // Re-add as unread after a short delay (simulated by creating a duplicate)
          setTimeout(() => {
            update((s: WebSocketState) => {
              const stillExists = s.notifications.find(
                (n) => n.id === id && n.status === 'processing'
              );
              if (stillExists) {
                stillExists.read = false;
                stillExists.readAt = undefined;
                saveNotificationsToStorage(s.notifications);
              }
              return s;
            });
          }, 100);
        } else {
          // Regular dismissal for completed/error notifications
          state.notifications[index].read = true;
          state.notifications[index].readAt = new Date();
        }

        saveNotificationsToStorage(state.notifications);
      }
      return state;
    });
  };

  // Apply retention now (on panel close, the moment deferred pruning is allowed)
  const prune = () => {
    update((state: WebSocketState) => {
      const kept = retain(state.notifications);
      if (kept.length !== state.notifications.length) {
        state.notifications = kept;
        saveNotificationsToStorage(state.notifications);
      }
      return state;
    });
  };

  // Mark all notifications as read
  const markAllAsRead = () => {
    update((state: WebSocketState) => {
      const readAt = new Date();
      state.notifications = state.notifications.map((n: Notification) => ({
        ...n,
        read: true,
        // Keep the original read time of items that were already read.
        readAt: n.read && n.readAt ? n.readAt : readAt,
      }));
      saveNotificationsToStorage(state.notifications);
      return state;
    });
  };

  const clearAll = () => {
    update((state: WebSocketState) => {
      state.notifications = [];
      clearedNotice.reset();
      saveNotificationsToStorage(state.notifications);
      return state;
    });
  };

  // Titles and the takedown notice text live in $lib/notifications/titles.
  const getNotificationTitle = (type: string): string => notificationTitle(type, get(t));

  // Add a notification manually (for client-side events like audio extraction)
  const addNotification = (notification: Omit<Notification, 'id' | 'timestamp' | 'read'>) => {
    update((state: WebSocketState) => {
      const newNotification: Notification = {
        ...notification,
        id: generateId('ws'),
        timestamp: new Date(),
        read: false,
      };

      // If this is a progressive notification, check if we should update existing one
      if (notification.progressId) {
        const existingIndex = state.notifications.findIndex(
          (n) => n.progressId === notification.progressId
        );
        if (existingIndex !== -1) {
          // Update existing notification
          state.notifications[existingIndex] = {
            ...state.notifications[existingIndex],
            ...newNotification,
            readAt: undefined,
            id: state.notifications[existingIndex].id, // Keep original ID
            timestamp: state.notifications[existingIndex].timestamp, // Keep original timestamp
          };
        } else {
          // Add new notification at the beginning. Capped like every WS-delivered
          // path (`:782`/`:836`/`:858`/`:1288`) — this one was the odd one out
          // (issue #569's currency review), so a client-originated source (audio
          // extraction, download bridge) with a fresh `progressId` per item could
          // grow the list without bound.
          state.notifications = retain([newNotification, ...state.notifications]);
        }
      } else {
        // Add new notification at the beginning
        state.notifications = retain([newNotification, ...state.notifications]);
      }

      saveNotificationsToStorage(state.notifications);
      return state;
    });
  };

  // Update an existing notification (for progressive updates)
  const updateNotification = (progressId: string, updates: Partial<Notification>) => {
    update((state: WebSocketState) => {
      const existingIndex = state.notifications.findIndex((n) => n.progressId === progressId);
      if (existingIndex !== -1) {
        state.notifications[existingIndex] = {
          ...state.notifications[existingIndex],
          ...updates,
        };
        saveNotificationsToStorage(state.notifications);
      }
      return state;
    });
  };

  // Remove notification (with auto-regeneration for processing notifications)
  const removeNotification = (id: string) => {
    update((state: WebSocketState) => {
      const notification = state.notifications.find((n) => n.id === id);

      if (notification && notification.status === 'processing' && !notification.dismissible) {
        // Auto-regenerate processing notifications
        setTimeout(() => {
          update((s: WebSocketState) => {
            // Only re-add if it doesn't already exist
            const exists = s.notifications.find(
              (n) => n.progressId === notification.progressId && n.status === 'processing'
            );
            if (!exists) {
              const regenerated: Notification = {
                ...notification,
                id: generateId('ws'),
                timestamp: new Date(),
                read: false,
                readAt: undefined,
              };
              s.notifications = [regenerated, ...s.notifications];
              saveNotificationsToStorage(s.notifications);
            }
            return s;
          });
        }, 100);
      }

      // Remove the notification
      state.notifications = state.notifications.filter((n) => n.id !== id);
      saveNotificationsToStorage(state.notifications);
      return state;
    });
  };

  // Fold a post_processing_status frame, or a REST snapshot the card asked for (`force`),
  // into the file's post-processing record ($lib/notifications/postProcessing).
  const applyPostProcessing = (frame: PostProcessingFrame, force = false) => {
    update((s: WebSocketState) => {
      const next = upsertPostProcessingRecord(
        s.notifications,
        frame,
        Date.now(),
        () => generateId('ws'),
        getNotificationTitle('post_processing_status'),
        force
      );
      if (next) {
        s.notifications = retain(next);
        saveNotificationsToStorage(s.notifications);
      }
      return s;
    });
  };

  // Fold a progress frame into its card: update in place and move to the front, or
  // create it ($lib/notifications/progressFrame computed the fields).
  const foldProgressFrame = (type: string, data: NotificationData, fields: ProgressFields) => {
    const { progressId, status, currentStep, progress } = fields;
    update((s: WebSocketState) => {
      // Find existing progressive notification
      const existingIndex = s.notifications.findIndex((n) => n.progressId === progressId);

      const existing = existingIndex !== -1 ? s.notifications[existingIndex] : undefined;
      // Any finished state, including 'done' (redaction) and 'disabled' /
      // 'not_configured' (LLM tasks), may be dismissed; the two creation and
      // update paths used to disagree, leaving a first-frame skip undismissable.
      const dismissible =
        isTerminalState(taskStateFromStatus(status)) ||
        (type === 'youtube_processing_status' && status === 'pending');
      const { timing } = advanceRunTiming(
        existing,
        existing ? existing.data?.status ?? existing.status : undefined,
        status,
        progress.percentage,
        Date.now(),
        typeof data.stage === 'string' ? data.stage : undefined
      );

      if (existing) {
        // Update existing notification with new data (important for summary completion)
        const updatedNotification: Notification = {
          ...existing,
          ...timing,
          message: currentStep,
          timestamp: new Date(),
          currentStep,
          progress,
          status: status as 'processing' | 'completed' | 'error',
          dismissible,
          read: false, // Mark as unread for updates
          readAt: undefined,
          data: {
            ...existing.data,
            ...data,
          }, // Merge old and new data
        };

        // Remove from current position and add to front to maintain most-recent-first ordering
        s.notifications.splice(existingIndex, 1);
        s.notifications.unshift(updatedNotification);
      } else {
        // Create new progressive notification
        const notification: Notification = {
          id: generateId('ws'),
          progressId,
          type: type as NotificationType,
          title: getNotificationTitle(type),
          message: currentStep,
          timestamp: new Date(),
          read: false,
          data,
          ...timing,
          currentStep,
          progress,
          status: status as 'processing' | 'completed' | 'error',
          dismissible,
        };

        s.notifications = retain([notification, ...s.notifications]);
      }

      saveNotificationsToStorage(s.notifications);
      return s;
    });
  };

  const addSilentFrame = (type: string, data: NotificationData) => {
    // Create a silent notification for gallery updates only
    const notification: Notification = {
      id: generateId('ws'),
      type: type as NotificationType,
      title: getNotificationTitle(type),
      message: data.message || get(t)('notifications.galleryUpdate'),
      timestamp: new Date(),
      read: false,
      data,
      dismissible: true,
      silent: true, // This prevents it from showing in notification panel
    };

    // Add notification (MediaLibrary will see it, but NotificationsPanel will filter it out)
    update((s: WebSocketState) => {
      s.notifications = retain([notification, ...s.notifications]);
      // Don't save silent notifications to localStorage to keep it clean
      return s;
    });
  };

  const addRegularFrame = (type: string, data: NotificationData) => {
    // Create a regular notification for other non-progressive types
    const notification: Notification = {
      id: generateId('ws'),
      type: type as NotificationType,
      title: getNotificationTitle(type),
      message:
        composeClientMessage(type, data, get(t)) ||
        data.message ||
        get(t)('notifications.noMessage'),
      timestamp: new Date(),
      read: false,
      data,
      dismissible: true,
    };

    // Add notification
    update((s: WebSocketState) => {
      s.notifications = retain([notification, ...s.notifications]);
      saveNotificationsToStorage(s.notifications);
      return s;
    });
  };

  // Create/update the progress card of a task recovered after a (re)connect
  const upsertRecoveredTask = (task: ActiveTask, recovered: RecoveredTask) => {
    update((s: WebSocketState) => {
      const existingIndex = s.notifications.findIndex((n) => n.progressId === recovered.progressId);

      const notificationData = recoveredNotification(
        task,
        recovered,
        existingIndex !== -1 ? s.notifications[existingIndex].id : generateId('ws'),
        getNotificationTitle(recovered.notificationType)
      );

      if (existingIndex !== -1) {
        s.notifications[existingIndex] = notificationData;
      } else {
        s.notifications = retain([notificationData, ...s.notifications]);
      }

      saveNotificationsToStorage(s.notifications);
      return s;
    });
  };

  return {
    markAsRead,
    prune,
    markAllAsRead,
    clearAll,
    addNotification,
    updateNotification,
    removeNotification,
    applyPostProcessing,
    foldProgressFrame,
    addSilentFrame,
    addRegularFrame,
    upsertRecoveredTask,
  };
}
