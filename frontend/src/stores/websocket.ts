import { writable, derived, get } from 'svelte/store';
import * as authStore from './auth';
import { reconnectDelayMs } from '$lib/utils/backoff';
import { isCloudEdition } from '$lib/edition';
import { sessionAnswersAfterRefusal, sessionSurvivesRevocation } from '$lib/auth/sessionRecheck';
import { countUnreadItems } from '$lib/notifications/jobGrouping';
import type { PostProcessingFrame } from '$lib/notifications/postProcessing';
import { showNotificationsPanel } from '$stores/notificationsPanel';
import { dispatchMessage, recoverActiveProgress } from '$lib/websocket/frameDispatch';
import {
  closeAction,
  uiConnectionStatus,
  websocketUrl,
  type UiConnectionStatus,
} from '$lib/websocket/connectionPolicy';
import { createNotificationActions, type WebSocketState } from './websocketNotifications';
import { loadNotificationsFromStorage } from './notificationStorage';

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

// Cloud edition: send the hosted IdP's fresh token as the first frame. A failure is logged,
// not fatal: the backend closes a socket it cannot authenticate.
async function sendExternalAuth(socket: WebSocket): Promise<void> {
  try {
    const { getSessionToken } = await import('$lib/cloud');
    const sessionToken = await getSessionToken();
    if (sessionToken) {
      socket.send(JSON.stringify({ type: 'authenticate', token: sessionToken }));
    }
  } catch (err) {
    console.error('[ws] Failed to send external authenticate frame:', err);
  }
}

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

  const markConnected = () => {
    update((s: WebSocketState) => {
      s.status = 'connected';
      s.reconnectAttempts = 0;
      return s;
    });
    // Recover active task progress from backend (survives page refresh/modal close)
    recoverActiveProgress(notifications);
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
            void sendExternalAuth(socket).then(markConnected);
            return;
          }
          markConnected();
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

        socket.onmessage = (event) => dispatchMessage(event.data, notifications);
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
