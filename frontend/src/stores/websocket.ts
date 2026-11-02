import { writable, derived, get, type Writable } from 'svelte/store';
import * as authStore from './auth';
import { downloadStore, type DownloadState } from './downloads';
import { t } from '$stores/locale';
import axiosInstance from '$lib/axios';
import { generateId } from '$lib/utils/ids';
import { reconnectDelayMs } from '$lib/utils/backoff';
import { isCloudEdition } from '$lib/edition';
import { SESSION_REVOKED_CLOSE_CODE, sessionSurvivesRevocation } from '$lib/auth/sessionRecheck';
import { pruneNotifications } from '$lib/notifications/retention';
import {
  advanceRunTiming,
  isTerminalState,
  taskStateFromStatus,
} from '$lib/notifications/runTiming';
import { countUnreadItems } from '$lib/notifications/jobGrouping';
import {
  upsertPostProcessingRecord,
  type PostProcessingFrame,
} from '$lib/notifications/postProcessing';
import { notificationRetention, clearedNotice } from '$stores/notificationRetention';
import { showNotificationsPanel } from '$stores/notificationsPanel';
import type { NotificationType, Notification } from '$lib/notifications/types';
import { composeClientMessage, notificationTitle } from '$lib/notifications/titles';
import { routeFrame } from '$lib/websocket/frameRouting';
import { frameKind, progressFields } from '$lib/notifications/progressFrame';
import {
  recoverTask,
  recoveredNotification,
  type ActiveTask,
} from '$lib/notifications/activeTaskRecovery';

export { NOTIFICATION_TYPES } from '$lib/notifications/types';
export type { NotificationType, NotificationData, Notification } from '$lib/notifications/types';

// WebSocket connection status
export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error';

// Store state interface
interface WebSocketState {
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
const loadNotificationsFromStorage = (): Notification[] => {
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

// Initial state
const initialState: WebSocketState = {
  socket: null,
  status: 'disconnected',
  notifications: loadNotificationsFromStorage(),
  reconnectAttempts: 0,
  error: null,
};

// Create the store
function createWebSocketStore() {
  const { subscribe, set, update } = writable<WebSocketState>(initialState);

  // Keep track of reconnect timeout
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

  // Get current state without subscribing
  const getState = (): WebSocketState => {
    let currentState: WebSocketState = initialState;
    const unsubscribe = subscribe((state) => {
      currentState = state;
    });
    unsubscribe();
    return currentState;
  };

  // Connect to WebSocket server (cookies are sent automatically)
  const connect = (_token?: string) => {
    update((state: WebSocketState) => {
      // Clean up previous connection if exists
      if (state.socket) {
        state.socket.onclose = null;
        state.socket.onerror = null;
        state.socket.onmessage = null;
        state.socket.onopen = null;
        state.socket.close();
      }

      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }

      try {
        // Always construct WebSocket URL dynamically based on current location
        // This ensures it works in dev, production, and when accessed from different computers
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;
        const wsUrl = `${protocol}//${host}/api/ws`;

        // Create new WebSocket — httpOnly cookies are sent automatically by the browser
        const socket = new WebSocket(wsUrl);

        state.status = 'connecting';
        state.socket = socket;
        state.error = null;

        // WebSocket event handlers
        socket.onopen = () => {
          // Community edition: authentication is handled via httpOnly cookies sent
          // during the WebSocket handshake. The backend WS handler reads the
          // access_token cookie automatically.
          //
          // Cloud edition: browsers can't set Authorization headers on a WS
          // handshake, so we authenticate with a first-message frame carrying a
          // fresh hosted-IdP token. The backend's WS external-verifier reads it.
          // We only mark connected / recover progress after auth is sent.
          if (isCloudEdition) {
            (async () => {
              try {
                const { getSessionToken } = await import('$lib/cloud');
                const sessionToken = await getSessionToken();
                if (sessionToken) {
                  socket.send(JSON.stringify({ type: 'authenticate', token: sessionToken }));
                }
              } catch (err) {
                console.error('[ws] Failed to send external authenticate frame:', err);
              }
              update((s: WebSocketState) => {
                s.status = 'connected';
                s.reconnectAttempts = 0;
                return s;
              });
              recoverActiveProgress();
            })();
            return;
          }

          update((s: WebSocketState) => {
            s.status = 'connected';
            s.reconnectAttempts = 0;
            return s;
          });

          // Recover active task progress from backend (survives page refresh/modal close)
          recoverActiveProgress();
        };

        socket.onclose = (event) => {
          update((s: WebSocketState) => {
            s.status = 'disconnected';
            s.socket = null;

            // Don't attempt to reconnect if closed cleanly or if page is hidden
            const shouldReconnect =
              event.code !== 1000 &&
              event.code !== 1001 &&
              (typeof document === 'undefined' || !document.hidden);

            if (event.code === SESSION_REVOKED_CLOSE_CODE) {
              void sessionSurvivesRevocation().then((live) => {
                if (live) tryReconnect();
              });
            } else if (shouldReconnect) {
              tryReconnect();
            }

            return s;
          });
        };

        socket.onerror = () => {
          update((s: WebSocketState) => {
            s.status = 'error';
            s.error = 'WebSocket connection error';
            return s;
          });
        };

        socket.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);

            // Handle different message types
            if (data.type === 'download_progress') {
              // Handle download progress messages specially
              handleDownloadProgress(data);
              return;
            }

            // Window events and cache invalidation ($lib/websocket/frameRouting).
            if (routeFrame(data)) return;

            // Progress cards, post-processing, silent and regular notifications
            // ($lib/notifications/progressFrame).
            const kind = frameKind(data.type, data.data);

            if (kind === 'progress') {
              const { progressId, status, currentStep, progress } = progressFields(
                data.type,
                data.data,
                get(t)
              );

              update((s: WebSocketState) => {
                // Find existing progressive notification
                const existingIndex = s.notifications.findIndex((n) => n.progressId === progressId);

                const existing = existingIndex !== -1 ? s.notifications[existingIndex] : undefined;
                // Any finished state, including 'done' (redaction) and 'disabled' /
                // 'not_configured' (LLM tasks), may be dismissed; the two creation and
                // update paths used to disagree, leaving a first-frame skip undismissable.
                const dismissible =
                  isTerminalState(taskStateFromStatus(status)) ||
                  (data.type === 'youtube_processing_status' && status === 'pending');
                const { timing } = advanceRunTiming(
                  existing,
                  existing ? existing.data?.status ?? existing.status : undefined,
                  status,
                  progress.percentage,
                  Date.now(),
                  typeof data.data.stage === 'string' ? data.data.stage : undefined
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
                      ...data.data,
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
                    type: data.type as NotificationType,
                    title: getNotificationTitle(data.type),
                    message: currentStep,
                    timestamp: new Date(),
                    read: false,
                    data: data.data,
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
            } else if (kind === 'post_processing') {
              applyPostProcessing(data.data);
            } else if (kind === 'silent') {
              // Create a silent notification for gallery updates only
              const notification: Notification = {
                id: generateId('ws'),
                type: data.type as NotificationType,
                title: getNotificationTitle(data.type),
                message: data.data.message || get(t)('notifications.galleryUpdate'),
                timestamp: new Date(),
                read: false,
                data: data.data,
                dismissible: true,
                silent: true, // This prevents it from showing in notification panel
              };

              // Add notification (MediaLibrary will see it, but NotificationsPanel will filter it out)
              update((s: WebSocketState) => {
                s.notifications = retain([notification, ...s.notifications]);
                // Don't save silent notifications to localStorage to keep it clean
                return s;
              });
            } else {
              // Create a regular notification for other non-progressive types
              const notification: Notification = {
                id: generateId('ws'),
                type: data.type as NotificationType,
                title: getNotificationTitle(data.type),
                message:
                  composeClientMessage(data.type, data.data, get(t)) ||
                  data.data.message ||
                  get(t)('notifications.noMessage'),
                timestamp: new Date(),
                read: false,
                data: data.data,
                dismissible: true,
              };

              // Add notification
              update((s: WebSocketState) => {
                s.notifications = retain([notification, ...s.notifications]);
                saveNotificationsToStorage(s.notifications);
                return s;
              });
            }
          } catch (error) {
            console.error('Error processing WebSocket message:', error);
          }
        };
      } catch (error) {
        state.status = 'error';
        state.error = 'Failed to connect to WebSocket server';
        console.error('WebSocket connection error:', error);
      }

      return state;
    });
  };

  // Try to reconnect with jittered exponential backoff.
  //
  // The jitter matters: a backend restart drops every connected client at the
  // same instant, and an un-jittered 2/4/8/16/30 s grid makes all of them
  // retry in lockstep, so the server takes a synchronised burst on every tick
  // while it is still coming up. `reconnectDelayMs` spreads each client over
  // the second half of its backoff window instead.
  const tryReconnect = () => {
    update((state: WebSocketState) => {
      state.reconnectAttempts += 1;
      return state;
    });

    reconnectTimeout = setTimeout(() => {
      connect();
    }, reconnectDelayMs(getState().reconnectAttempts));
  };

  const disconnect = () => {
    update((state: WebSocketState) => {
      if (state.socket) {
        state.socket.close(1000, 'User logged out');
        state.socket = null;
      }

      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }

      // The backoff belongs to the session that just ended; the next connect is a first one.
      state.reconnectAttempts = 0;
      state.status = 'disconnected';
      return state;
    });
  };

  const send = (message: unknown) => {
    update((state: WebSocketState) => {
      if (state.socket && state.status === 'connected') {
        state.socket.send(JSON.stringify(message));
      } else {
        console.warn('Cannot send message, WebSocket not connected');
      }
      return state;
    });
  };

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

  // Handle download progress messages (legacy/global). Media downloads initiated
  // from the file page are delivered over a dedicated SSE stream — see
  // TranscriptDisplay's EventSource — so this only mirrors status into the store.
  const handleDownloadProgress = (data: {
    data: {
      file_id?: string;
      status: DownloadState['status'];
      progress?: number;
      error?: string;
    };
  }) => {
    const { file_id, status, progress, error } = data.data;

    if (file_id) {
      downloadStore.updateStatus(file_id, status, progress, error);
    }
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

  // Recover active task progress from backend on connect/reconnect
  // ($lib/notifications/activeTaskRecovery).
  const recoverActiveProgress = async () => {
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
        update((s: WebSocketState) => {
          const existingIndex = s.notifications.findIndex(
            (n) => n.progressId === recovered.progressId
          );

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
      }
    } catch {
      // Recovery is best-effort; don't break WebSocket on failure
    }
  };

  return {
    subscribe,
    connect,
    disconnect,
    send,
    markAsRead,
    markAllAsRead,
    clearAll,
    prune,
    removeNotification,
    addNotification,
    updateNotification,
    applyPostProcessingSnapshot: (snapshot: PostProcessingFrame) =>
      applyPostProcessing(snapshot, true),
  };
}

// Create the WebSocket store
export const websocketStore = createWebSocketStore();

// Retention is deferred while the panel is open; the close is the deferred moment.
let panelWasOpen = false;
showNotificationsPanel.subscribe((open) => {
  if (panelWasOpen && !open) websocketStore.prune();
  panelWasOpen = open;
});

// Unread panel ITEMS (silent records excluded): a file's processing card counts once,
// however many of its event records are unread.
export const unreadCount = derived(websocketStore, ($websocketStore: WebSocketState) =>
  countUnreadItems($websocketStore.notifications)
);

/**
 * Simplified, UI-facing connection status.
 *
 * Collapses the store's internal {@link ConnectionStatus} into the states the UI
 * actually cares about for surfacing connection loss:
 *  - `connected`     — socket open, live updates flowing.
 *  - `connecting`    — the first attempt of a connect (sign-in, page load, tab shown) is
 *                      in flight and nothing has failed yet. Not a loss: show nothing.
 *  - `reconnecting`  — recovering after a non-clean close (backoff retries).
 *  - `disconnected`  — closed/errored with no reconnect in progress.
 *
 * Purely observational — it does not drive the reconnection logic, it only reflects it.
 */
export type UiConnectionStatus = 'connected' | 'connecting' | 'reconnecting' | 'disconnected';

export const connectionStatus = derived<typeof websocketStore, UiConnectionStatus>(
  websocketStore,
  ($ws) => {
    if ($ws.status === 'connected') return 'connected';
    if ($ws.reconnectAttempts > 0) return 'reconnecting';
    // A browser always follows `error` with `close`, which schedules the retry; until it
    // arrives the socket is still ours, so don't flash "Connection lost" in between.
    if ($ws.status === 'connecting' || ($ws.status === 'error' && $ws.socket)) {
      return 'connecting';
    }
    return 'disconnected';
  }
);

// Initialize WebSocket when auth changes (cookies sent automatically)
authStore.token.subscribe((token: string | null) => {
  if (token) {
    websocketStore.connect();
  } else {
    websocketStore.disconnect();
  }
});

// Handle page visibility changes to reconnect when page becomes visible
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      // Page became visible, check if we need to reconnect
      let shouldReconnect = false;

      const unsubscribe = websocketStore.subscribe((state: WebSocketState) => {
        if (state.status === 'disconnected' || state.status === 'error') {
          // Check if we're still authenticated (token is in httpOnly cookie)
          if (get(authStore.isAuthenticated)) {
            shouldReconnect = true;
          }
        }
      });
      unsubscribe();

      if (shouldReconnect) {
        websocketStore.connect();
      }
    }
  });
}
