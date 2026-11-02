/**
 * What to do when the server closes the realtime socket because the user's sessions
 * were revoked (logout-all, a password reset, an admin termination, a role change,
 * deletion). `api/websockets.py` sends close code 4004 for exactly that case.
 *
 * Reconnecting blindly would retry on the backoff timer with a cookie the server now
 * refuses. One request through the HTTP client settles it instead: its 401 path either
 * refreshes the session (this tab's session was reissued, so reconnecting is right) or
 * ends it and returns to /login, the same as any other revoked session.
 */
import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { isAuthenticated } from '$stores/auth';

/** Close code for a socket ended by a session revocation (`SESSION_REVOKED_CLOSE`). */
export const SESSION_REVOKED_CLOSE_CODE = 4004;

/** Whether the session outlived the revocation, so the socket may reconnect. */
export async function sessionSurvivesRevocation(): Promise<boolean> {
  try {
    await axiosInstance.get('/users/me');
  } catch {
    // A dead session has already been ended by the client's refresh-failure path;
    // any other failure leaves the auth state as it was, and that decides below.
  }
  return get(isAuthenticated);
}
