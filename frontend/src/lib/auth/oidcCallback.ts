import { get } from 'svelte/store';
import { handleOIDCCallback, isAuthenticated } from '$stores/auth';

export type OidcCallbackOutcome =
  /** Already handled (or already signed in): send the browser home with a full navigation. */
  | { kind: 'redirect-home' }
  | { kind: 'success' }
  /** The IdP round trip failed; `message` is the server's, if any. Show the form. */
  | { kind: 'error'; message?: string }
  /** Something threw (e.g. blocked site storage). Show the form. */
  | { kind: 'exception' };

/**
 * Finish an OIDC authorization-code callback.
 *
 * NOTHING in here may throw: the auth-methods fetch that follows on the login page (and the gate
 * it opens) is unreachable if an exception escapes, which would leave a user returning from the
 * IdP with no way in at all, not even the local credential form. `sessionStorage` is the concrete
 * hazard: in a browser blocking site storage, merely touching it throws `SecurityError`.
 */
export async function runOidcCallback(
  code: string,
  state: string,
  setLoading: (loading: boolean) => void
): Promise<OidcCallbackOutcome> {
  try {
    // Clear URL parameters immediately to prevent double-processing on refresh
    window.history.replaceState({}, document.title, window.location.pathname);

    // Check if we already processed this callback (prevents double toast)
    const processedKey = `oidc_callback_${state}`;
    if (sessionStorage.getItem(processedKey)) {
      return { kind: 'redirect-home' };
    }
    sessionStorage.setItem(processedKey, 'true');

    setLoading(true);
    const result = await handleOIDCCallback(code, state);
    setLoading(false);

    if (result.success) return { kind: 'success' };

    // A state-related failure is likely a double request: if the user is in fact already
    // signed in, just go home instead of showing an error.
    if (result.message?.includes('state') && get(isAuthenticated)) {
      return { kind: 'redirect-home' };
    }
    return { kind: 'error', message: result.message };
  } catch (err) {
    console.error('OIDC callback handling failed; falling back to the sign-in form', err);
    setLoading(false);
    return { kind: 'exception' };
  }
}
