/**
 * The notification list held by `$stores/websocket`: its state shape and the actions the panel
 * and client-side sources call. Persistence and retention are `notificationStorage`; the folds
 * of inbound frames into cards are `notificationFrameFolds`. The socket itself (connect,
 * reconnect, close codes) stays in `websocket.ts`; this module only ever touches
 * `state.notifications`.
 */
import type { Writable } from 'svelte/store';
import { generateId } from '$lib/utils/ids';
import type { Notification } from '$lib/notifications/types';
import type { ConnectionStatus } from '$lib/websocket/connectionPolicy';
import { clearedNotice } from '$stores/notificationRetention';
import { retain, saveNotificationsToStorage } from './notificationStorage';
import { createFrameFolds } from './notificationFrameFolds';

// Store state interface
export interface WebSocketState {
  socket: WebSocket | null;
  status: ConnectionStatus;
  notifications: Notification[];
  reconnectAttempts: number;
  error: string | null;
}

// The actions the panel and client-side sources call on the list.
function createPanelActions(update: Writable<WebSocketState>['update']) {
  // Mark notification as read (with auto-regeneration for processing notifications)
  const markAsRead = (id: string) => {
    update((state: WebSocketState) => {
      const index = state.notifications.findIndex((n: Notification) => n.id === id);
      const notification = index !== -1 ? state.notifications[index] : undefined;
      if (notification) {
        // If it's a processing notification, auto-regenerate it
        if (notification.status === 'processing' && !notification.dismissible) {
          // Mark as read but keep the notification
          notification.read = true;
          notification.readAt = new Date();
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
          notification.read = true;
          notification.readAt = new Date();
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
        const existing = existingIndex !== -1 ? state.notifications[existingIndex] : undefined;
        if (existing) {
          // Update existing notification
          state.notifications[existingIndex] = {
            ...existing,
            ...newNotification,
            readAt: undefined,
            id: existing.id, // Keep original ID
            timestamp: existing.timestamp, // Keep original timestamp
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
      const existing = existingIndex !== -1 ? state.notifications[existingIndex] : undefined;
      if (existing) {
        state.notifications[existingIndex] = {
          ...existing,
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

  return {
    markAsRead,
    prune,
    markAllAsRead,
    clearAll,
    addNotification,
    updateNotification,
    removeNotification,
  };
}

/** Notification-list actions bound to the websocket store's `update`. */
export function createNotificationActions(update: Writable<WebSocketState>['update']) {
  return { ...createPanelActions(update), ...createFrameFolds(update) };
}
