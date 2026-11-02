/**
 * Realtime connection policy: where the socket connects, what a close code means for
 * reconnecting, and the connection state the UI surfaces. Pure; the store owns the
 * socket, the timer and the backoff counter.
 */
import { SESSION_REVOKED_CLOSE_CODE } from '$lib/auth/sessionRecheck';

/** The store's internal WebSocket connection status. */
export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error';

/**
 * Always construct the WebSocket URL from the current location, so it works in dev,
 * production, and when accessed from different computers. httpOnly cookies are sent
 * with the handshake by the browser.
 */
export function websocketUrl(location: Pick<Location, 'protocol' | 'host'>): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${location.host}/api/ws`;
}

/**
 * - `recheck-session`: the server ended the socket because the user's sessions were
 *   revoked; reconnect only if one HTTP request shows the session survived.
 * - `reconnect`: retry on the backoff timer.
 * - `stay-closed`: a clean close (1000 / 1001), or the page is hidden (the visibility
 *   handler reconnects when it is shown again).
 */
export type CloseAction = 'recheck-session' | 'reconnect' | 'stay-closed';

export function closeAction(code: number, pageHidden: boolean): CloseAction {
  if (code === SESSION_REVOKED_CLOSE_CODE) return 'recheck-session';
  // Don't attempt to reconnect if closed cleanly or if page is hidden
  if (code !== 1000 && code !== 1001 && !pageHidden) return 'reconnect';
  return 'stay-closed';
}

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

export function uiConnectionStatus(state: {
  status: ConnectionStatus;
  reconnectAttempts: number;
  socket: WebSocket | null;
}): UiConnectionStatus {
  if (state.status === 'connected') return 'connected';
  if (state.reconnectAttempts > 0) return 'reconnecting';
  // A browser always follows `error` with `close`, which schedules the retry; until it
  // arrives the socket is still ours, so don't flash "Connection lost" in between.
  if (state.status === 'connecting' || (state.status === 'error' && state.socket)) {
    return 'connecting';
  }
  return 'disconnected';
}
