import { writable, derived, get } from 'svelte/store';
import * as authStore from './auth';
import { downloadStore, type DownloadState } from './downloads';
import { t } from '$stores/locale';
import axiosInstance from '$lib/axios';
import { reconnectDelayMs } from '$lib/utils/backoff';
import { isCloudEdition } from '$lib/edition';
import { sessionAnswersAfterRefusal, sessionSurvivesRevocation } from '$lib/auth/sessionRecheck';
import { countUnreadItems } from '$lib/notifications/jobGrouping';
import type { PostProcessingFrame } from '$lib/notifications/postProcessing';
import { showNotificationsPanel } from '$stores/notificationsPanel';
import { routeFrame } from '$lib/websocket/frameRouting';
import {
  closeAction,
  uiConnectionStatus,
  websocketUrl,
  type UiConnectionStatus,
} from '$lib/websocket/connectionPolicy';
import { frameKind, progressFields } from '$lib/notifications/progressFrame';
import { recoverTask, type ActiveTask } from '$lib/notifications/activeTaskRecovery';
import {
  createNotificationActions,
  loadNotificationsFromStorage,
  type WebSocketState,
} from './websocketNotifications';

export { NOTIFICATION_TYPES } from '$lib/notifications/types';
export type { NotificationType, NotificationData, Notification } from '$lib/notifications/types';

export type { ConnectionStatus, UiConnectionStatus } from '$lib/websocket/connectionPolicy';

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
  const { subscribe, update } = writable<WebSocketState>(initialState);
  const notifications = createNotificationActions(update);

  // Keep track of reconnect timeout
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

  // At most one session probe per refusal streak; cleared by a successful open or by a
  // disconnect.
  let terminalProbeDone = false;

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
        // Create new WebSocket — httpOnly cookies are sent automatically by the browser
        const socket = new WebSocket(websocketUrl(window.location));

        state.status = 'connecting';
        state.socket = socket;
        state.error = null;

        // WebSocket event handlers
        socket.onopen = () => {
          terminalProbeDone = false;
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

            const action = closeAction(
              event.code,
              typeof document !== 'undefined' && document.hidden,
              terminalProbeDone
            );
            if (action === 'probe-session' || action === 'refused') {
              s.status = 'error';
              if (action === 'probe-session') {
                terminalProbeDone = true;
                void sessionAnswersAfterRefusal().then((answered) => {
                  if (answered) tryReconnect();
                });
              }
              return s;
            }
            if (action === 'recheck-session') {
              void sessionSurvivesRevocation().then((live) => {
                if (live) tryReconnect();
              });
            } else if (action === 'reconnect') {
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
      terminalProbeDone = false;
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
        notifications.upsertRecoveredTask(task, recovered);
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
    markAsRead: notifications.markAsRead,
    markAllAsRead: notifications.markAllAsRead,
    clearAll: notifications.clearAll,
    prune: notifications.prune,
    removeNotification: notifications.removeNotification,
    addNotification: notifications.addNotification,
    updateNotification: notifications.updateNotification,
    applyPostProcessingSnapshot: (snapshot: PostProcessingFrame) =>
      notifications.applyPostProcessing(snapshot, true),
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

// Collapsed for the UI ($lib/websocket/connectionPolicy); purely observational.
export const connectionStatus = derived<typeof websocketStore, UiConnectionStatus>(
  websocketStore,
  uiConnectionStatus
);

// Initialize WebSocket when auth changes (cookies sent automatically)
authStore.token.subscribe((token: string | null) => {
  if (token) {
    websocketStore.connect();
  } else {
    websocketStore.disconnect();
  }
});

// A handshake made while the account owed a banner acknowledgment or a password change
// was refused, and a refusal is terminal; connect again once the hold clears.
authStore.lifecycleHoldCleared.subscribe((count: number) => {
  if (count === 0) return;
  if (get(websocketStore).status !== 'connected' && get(authStore.isAuthenticated)) {
    websocketStore.connect();
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
