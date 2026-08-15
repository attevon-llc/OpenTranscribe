import { writable, derived, get } from 'svelte/store';
import axiosInstance, { abortAllRequests } from '../lib/axios';
import { t } from '$stores/locale';
import { clearUserState } from '$lib/session/clearUserState';
import { isCloudEdition } from '$lib/edition';
import { loadCapabilities } from '$stores/capabilities';

/**
 * Minimal shape of an axios error used for status- and detail-based message
 * extraction in this module. Mirrors the relevant subset of AxiosError.
 */
interface AuthRequestError {
  response?: {
    status?: number;
    data?: { detail?: unknown; message?: unknown };
  };
  request?: unknown;
  message?: string;
}

function asAuthError(error: unknown): AuthRequestError {
  return (error ?? {}) as AuthRequestError;
}

// Certificate metadata lives with the endpoint that serves it
// (`$lib/api/certificate`). It was declared here, and hung off the user type,
// as though `/auth/me` returned it — it never has.

// Define user interface
interface User {
  uuid: string;
  email: string;
  full_name: string;
  role: 'user' | 'admin' | 'super_admin';
  // Authentication type. External/SSO provider strings beyond the core four
  // are registered by the managed edition's auth layer; the password-change UI
  // keys off `auth_type === 'local'`.
  auth_type: 'local' | 'ldap' | 'oidc' | 'pki' | string;
  allow_local_fallback?: boolean;
  // Cloud-edition tenancy/billing context (populated by the backend from the
  // external IdP's org claim). Absent in the community edition.
  org_id?: string;
  org_role?: string;
  subscription_tier?: string;
  // Account-lifecycle flags (backend `UserInDB`). `must_change_password` is the
  // same condition the 403 gate enforces — reading it here lets the login page
  // show the forced-change screen without first provoking a refused request.
  must_change_password?: boolean;
  account_expires_at?: string | null;
  // Email verification (v375). Surfaced in the admin user list.
  email_verified?: boolean;
  email_verified_at?: string | null;
  /**
   * Administrator admission state (v379): `pending` | `approved` | `rejected`.
   * Served on the ordinary user schema (`UserInDB`), so the admin Users table can
   * show a held account without a second request. Optional here because an older
   * backend omits it; treat a missing value as `approved`, which is the backend's
   * own default.
   */
  approval_status?: 'pending' | 'approved' | 'rejected' | string;
  approved_at?: string | null;
  created_at: string;
  updated_at: string;
}

// Available authentication methods
export interface AuthMethods {
  methods: string[];
  oidc_enabled: boolean;
  /**
   * @deprecated Duplicate of `oidc_enabled`, emitted by the backend for one
   * minor release so a cached bundle of THIS file keeps working against an
   * upgraded backend. Never read it in new code.
   */
  keycloak_enabled?: boolean;
  pki_enabled: boolean;
  ldap_enabled: boolean;
  // Whether accounts holding a local password may sign in at all. Note this is
  // NOT the same as "show the username/password form" — LDAP authenticates
  // through that same form, so the form is gated on `local_enabled ||
  // ldap_enabled`.
  local_enabled: boolean;
  // Whether anyone may create their own account via POST /auth/register.
  allow_registration: boolean;
  mfa_enabled: boolean;
  mfa_required: boolean;
  login_banner_enabled: boolean;
  login_banner_text: string;
  login_banner_classification: string;
}

// Define auth store interface
interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  ready: boolean;
  token: string | null;
}

// Define the extended auth store type with helper methods
type AuthStore = {
  subscribe: (
    run: (value: AuthState) => void,
    invalidate?: (value?: AuthState) => void
  ) => () => void;
  set: (value: AuthState) => void;
  update: (updater: (value: AuthState) => AuthState) => void;
  // Helper methods
  setUser: (userData: User | null) => void;
  setToken: (tokenValue: string | null) => void;
  setReady: (isReady: boolean) => void;
  reset: () => void;
};

// Create a single auth store to avoid circular dependencies
const createAuthStore = (): AuthStore => {
  const store = writable<AuthState>({
    user: null,
    isAuthenticated: false,
    ready: false,
    token: null,
  });

  return {
    ...store,
    // Helper methods to update specific parts of the state
    setUser: (userData: User | null) => {
      store.update((state) => ({ ...state, user: userData }));
    },
    setToken: (tokenValue: string | null) => {
      store.update((state) => ({
        ...state,
        token: tokenValue,
        isAuthenticated: tokenValue !== null,
      }));
    },
    setReady: (isReady: boolean) => {
      store.update((state) => ({ ...state, ready: isReady }));
    },
    reset: () => {
      store.set({
        user: null,
        isAuthenticated: false,
        ready: true,
        token: null,
      });
    },
  };
};

// Create the auth store with helper methods
export const authStore = createAuthStore();

// Create convenience derived stores
export const user = derived(authStore, ($store) => $store.user);
export const isAuthenticated = derived(authStore, ($store) => $store.isAuthenticated);
export const authReady = derived(authStore, ($store) => $store.ready);
export const token = derived(authStore, ($store) => $store.token);

// ---------------------------------------------------------------------------
// Account lifecycle (FedRAMP AC-2 / AC-8 / IA-5)
//
// `get_current_active_user` refuses five account states with a 403 whose
// `detail` is an OBJECT, not the usual string:
//
//   {"detail": {"code": "password_change_required",       "message": "…"}}
//   {"detail": {"code": "account_expired",                "message": "…"}}
//   {"detail": {"code": "banner_acknowledgment_required", "message": "…",
//               "reason": "never_acknowledged" | "banner_text_changed"}}
//   {"detail": {"code": "account_pending_approval",       "message": "…"}}
//   {"detail": {"code": "account_rejected",               "message": "…"}}
//
// The `code` is the contract; the prose is not. Anything that string-matches the
// message breaks the moment it is reworded or translated, and every other 403
// ("Not enough permissions") still carries a plain string and must keep its
// ordinary error handling — hence the narrow classifier below rather than a
// blanket "403 means locked out".
//
// The gates fire in the order expiry → banner → password change, so a caller
// owing more than one clears them one 403 at a time; each remedy re-provokes the
// next hold rather than trying to predict it.
//
// The approval pair (`v379`) has no in-app remedy at all — the decision is
// somebody else's — and no route is exempt from it, so EVERY request answers 403.
// That is exactly why these render as a blocking screen and never as a toast: a
// toast would fire once per refused request, forever.
// ---------------------------------------------------------------------------

type AccountLifecycleCode =
  | 'password_change_required'
  | 'account_expired'
  | 'banner_acknowledgment_required'
  | 'account_pending_approval'
  | 'account_rejected';

/**
 * Why the banner gate fired. `banner_text_changed` means the user DID accept a
 * banner — different wording — so the UI must say the notice was updated rather
 * than present the refusal as a failure.
 */
type BannerAcknowledgmentReason = 'never_acknowledged' | 'banner_text_changed';

export interface AccountLifecycleState {
  code: AccountLifecycleCode;
  /** Server-supplied prose, rendered verbatim. May be empty. */
  message: string;
  /** Only carried by `banner_acknowledgment_required`; absent otherwise. */
  reason?: BannerAcknowledgmentReason;
}

const ACCOUNT_LIFECYCLE_CODES: readonly AccountLifecycleCode[] = [
  'password_change_required',
  'account_expired',
  'banner_acknowledgment_required',
  'account_pending_approval',
  'account_rejected',
];

const BANNER_ACKNOWLEDGMENT_REASONS: readonly BannerAcknowledgmentReason[] = [
  'never_acknowledged',
  'banner_text_changed',
];

/**
 * The active lifecycle hold, or null. Non-null means the session is confined to
 * the corresponding screen: no other API call will succeed until it clears.
 */
export const accountLifecycle = writable<AccountLifecycleState | null>(null);

/**
 * Classify an axios error as an account-lifecycle refusal.
 *
 * Returns null for every other error — including a 403 with a string `detail`,
 * an array `detail` (Pydantic validation), and an object `detail` carrying an
 * unrecognised code. Callers must fall back to their normal error path then.
 */
export function readAccountLifecycle(error: unknown): AccountLifecycleState | null {
  const response = (
    error as { response?: { status?: number; data?: { detail?: unknown } } } | undefined
  )?.response;
  if (response?.status !== 403) return null;

  const detail = response?.data?.detail;
  if (detail === null || typeof detail !== 'object' || Array.isArray(detail)) return null;

  const { code, message, reason } = detail as {
    code?: unknown;
    message?: unknown;
    reason?: unknown;
  };
  if (typeof code !== 'string') return null;
  if (!ACCOUNT_LIFECYCLE_CODES.includes(code as AccountLifecycleCode)) return null;

  const state: AccountLifecycleState = {
    code: code as AccountLifecycleCode,
    message: typeof message === 'string' ? message : '',
  };

  // An unrecognised `reason` is dropped rather than passed through: the UI
  // switches copy on it, and inventing a branch for an unknown value would show
  // the user nothing at all.
  if (
    typeof reason === 'string' &&
    BANNER_ACKNOWLEDGMENT_REASONS.includes(reason as BannerAcknowledgmentReason)
  ) {
    state.reason = reason as BannerAcknowledgmentReason;
  }

  return state;
}

/**
 * Apply an account-lifecycle refusal, if this error is one.
 *
 * `account_expired` and `account_rejected` have no self-service remedy, so the
 * session is torn down immediately; the reason is re-published afterwards because
 * `logout()` clears user state. `password_change_required` keeps the session alive
 * on purpose — `PUT /users/me` is what clears the flag, and it needs that session.
 * Same for `banner_acknowledgment_required`: `POST /auth/banner/acknowledge` is
 * the remedy and it is authenticated.
 *
 * `account_pending_approval` also keeps the session. Not because anything can be
 * done with it — no route is exempt from that gate — but because the hold may
 * clear the moment an administrator works the queue, and tearing the session down
 * would turn "wait a minute and reload" into "sign in again". The blocking screen
 * offers an explicit logout instead, so the user is never trapped.
 */
export async function handleAccountLifecycleError(
  error: unknown
): Promise<AccountLifecycleState | null> {
  const state = readAccountLifecycle(error);
  if (!state) return null;

  accountLifecycle.set(state);

  if (state.code === 'account_expired' || state.code === 'account_rejected') {
    await logout();
    accountLifecycle.set(state);
  }

  return state;
}

/** Drop the lifecycle hold (the flag cleared server-side, or the user signed out). */
export function clearAccountLifecycle(): void {
  accountLifecycle.set(null);
}

/**
 * Record consent to the login banner (`POST /auth/banner/acknowledge`).
 *
 * This is the ONLY thing that satisfies the AC-8 gate. The SPA used to fake it
 * with a `sessionStorage` flag, which the server never saw — so on a deployment
 * with the banner enabled every route answered 403 forever. The endpoint is one
 * of the few exempt from the gate, so it is reachable while the hold is active.
 *
 * The call needs a session: the pre-login banner is a notice, and consent is
 * recorded once the user is actually signed in.
 *
 * Only a banner hold is cleared on success. A caller who also owes a password
 * change is still refused by the next gate, and dropping that hold here would
 * hide the remedy screen for it.
 */
export async function acknowledgeBanner(): Promise<{ success: boolean; message?: string }> {
  try {
    await axiosInstance.post('/auth/banner/acknowledge');

    if (get(accountLifecycle)?.code === 'banner_acknowledgment_required') {
      clearAccountLifecycle();
    }
    return { success: true };
  } catch (rawError: unknown) {
    console.error('auth.ts: Banner acknowledgment failed:', rawError);
    const detail = asAuthError(rawError).response?.data?.detail;
    return {
      success: false,
      message: typeof detail === 'string' ? detail : get(t)('loginBanner.acknowledgeFailed'),
    };
  }
}

let lifecycleInterceptorId: number | null = null;

/**
 * Watch every axios response for the two lifecycle refusals.
 *
 * Installed once from the app shell. It only observes — the error still rejects,
 * so existing catch blocks are unaffected; the UI reacts to `accountLifecycle`.
 */
export function installAccountLifecycleInterceptor(): void {
  if (lifecycleInterceptorId !== null) return;
  lifecycleInterceptorId = axiosInstance.interceptors.response.use(
    (response) => response,
    async (error) => {
      await handleAccountLifecycleError(error);
      return Promise.reject(error);
    }
  );
}

/**
 * Cloud-edition session init: read the hosted external-auth session instead of
 * the cookie `/auth/session` probe. If a session is active, the per-request
 * axios interceptor mints a bearer token and `/auth/me` is authenticated by the
 * backend's external verifier. Community edition never reaches this path.
 */
async function initAuthExternal(): Promise<void> {
  try {
    const { loadExternalAuth, hasExternalSession } = await import('$lib/cloud');
    await loadExternalAuth();

    if (await hasExternalSession()) {
      // Bearer is attached per-request by the axios interceptor (cloud build).
      const userData = await fetchUserInfo();
      if (userData) {
        authStore.setToken('external');
        authStore.setReady(true);
        return;
      }
    }

    authStore.reset();
  } catch (error) {
    console.error('auth.ts: external-auth initAuth failed:', error);
    authStore.reset();
  }
}

// Initialize auth state by verifying the cookie session with the backend
export async function initAuth() {
  authStore.setReady(false);

  // Clear any legacy localStorage tokens (migration from pre-cookie auth)
  localStorage.removeItem('token');
  localStorage.removeItem('user');

  // Cloud edition delegates session detection to the hosted IdP (no cookie session).
  if (isCloudEdition) {
    await initAuthExternal();
    return;
  }

  try {
    // Probe /auth/session — returns 200 whether or not a session exists, so
    // anonymous page loads don't produce 401 console errors or side effects.
    const { data } = await axiosInstance.get('/auth/session');

    if (data.authenticated) {
      authStore.setUser(data.user);
      authStore.setToken('cookie');
      authStore.setReady(true);
      return;
    }

    if (data.refreshable) {
      // Access token expired but a refresh cookie is present — restore the
      // session silently instead of bouncing the user to the login page.
      await axiosInstance.post('/auth/token/refresh', {});
      const userData = await fetchUserInfo();
      if (userData) {
        authStore.setToken('cookie');
        authStore.setReady(true);
        return;
      }
    }

    authStore.reset();
  } catch (error) {
    authStore.reset();
  }
}

// Fetch current user info from API
//
// Deliberately does NOT fetch certificate metadata. Both this branch and PR #404
// fixed the same dead-surface bug (the CertificateInfo panel was mounted but
// nothing ever populated it); #404's approach won on merge and is the one kept:
// CertificateInfo.svelte fetches its own data through `$lib/api/certificate`,
// and the misleading `certificate?:` field is gone from the user type entirely.
// Hydrating it here would have put those fields back on every session probe for
// the local/LDAP/OIDC majority, for whom they are meaningless.
export async function fetchUserInfo() {
  try {
    const response = await axiosInstance.get('/auth/me');
    const userData = response.data;

    authStore.setUser(userData);
    return userData;
  } catch (error: unknown) {
    // No logout() here: callers decide what a failed probe means. The old
    // logout() side effect aborted unrelated in-flight requests (e.g. the
    // login page's getAuthMethods) on every anonymous page load.
    const status = asAuthError(error).response?.status;
    if (status !== 401) {
      console.error('auth.ts: Failed to fetch user info:', error);
    }
    return null;
  }
}

// Login function - returns mfa_required and mfa_token if MFA is needed
export async function login(
  email: string,
  password: string
): Promise<{
  success: boolean;
  message?: string;
  // HTTP status of the failed request. Callers must key their UI off THIS, not
  // off substrings of `message` — `message` is localised, so `.includes('email')`
  // silently matches nothing in the other seven locales.
  status?: number;
  mfa_required?: boolean;
  // Set when the deployment requires MFA and this account has not enrolled yet.
  // The server sends no access token and sets no cookies in that case — the only
  // way forward is the enrolment flow. May be ABSENT on a plain MFA challenge,
  // hence the strict `=== true` test below.
  mfa_enrollment_required?: boolean;
  mfa_token?: string;
  // The credentials were correct but the address is unverified and this
  // deployment requires verification (v375). `/auth/login` raises exactly one
  // 403 — `assert_email_verified_for_local_login` — so the STATUS identifies it
  // and no substring match on the localised message is needed.
  email_not_verified?: boolean;
  // The account carries `must_change_password`. The session is real, but every
  // route except `PUT /users/me` and logout will answer 403 until it clears.
  must_change_password?: boolean;
}> {
  try {
    const params = new URLSearchParams();
    params.append('username', email);
    params.append('password', password);

    // Use the axiosInstance which handles URL formats consistently
    // But we need to customize headers for this specific request
    const response = await axiosInstance.post('/auth/login', params, {
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
    });

    // Check if MFA is required. Two distinct 200 bodies land here: a plain TOTP
    // challenge, and a forced-enrolment challenge for an account that has never
    // set up a second factor on an MFA-required deployment.
    if (response.status === 200 && response.data.mfa_required) {
      return {
        success: false,
        mfa_required: true,
        mfa_enrollment_required: response.data.mfa_enrollment_required === true,
        mfa_token: response.data.mfa_token,
      };
    }

    if (response.status !== 200 || !response.data.access_token) {
      console.error('auth.ts: Invalid login response');
      return { success: false, message: get(t)('auth.error.invalidLoginResponse') };
    }

    // Clear ALL stale user state from any previous session before the new
    // user sees the app. See lib/session/clearUserState.ts for the full list.
    await clearUserState();

    // Token is now in httpOnly cookie — mark as authenticated in memory
    authStore.setToken('cookie');

    const userData = await fetchUserInfo();

    authStore.setReady(true);

    // Re-fetch the (tier-scoped) capability map for the NEW user. `clearUserState()`
    // above reset it, and `routes/+layout.svelte`'s onMount — the only other call
    // site — does not re-run on an SPA login. Every login path below repeats this.
    void loadCapabilities();

    // `/auth/me` resolves through `get_current_user`, which does NOT run the
    // lifecycle gate, so this flag is the earliest honest signal. Surfacing it
    // here means the forced-change screen appears instead of the app shell
    // failing its first request with a 403.
    return { success: true, must_change_password: userData?.must_change_password === true };
  } catch (rawErr: unknown) {
    const err = asAuthError(rawErr);
    console.error('auth.ts: Login error:', rawErr);

    authStore.reset();

    // Extract meaningful error message from backend response
    let errorMessage = get(t)('auth.error.loginFailedCheckCredentials');

    if (err.response) {
      // Server responded with an error status
      switch (err.response.status) {
        case 401:
          errorMessage =
            (err.response.data?.detail as string) || get(t)('auth.error.invalidCredentials');
          break;
        case 400:
          errorMessage =
            (err.response.data?.detail as string) || get(t)('auth.error.invalidRequest');
          break;
        case 429:
          errorMessage = get(t)('auth.error.tooManyLoginAttempts');
          break;
        case 500:
        case 502:
        case 503:
          errorMessage = get(t)('auth.error.serverError');
          break;
        default:
          errorMessage =
            (err.response.data?.detail as string) ||
            (err.response.data?.message as string) ||
            get(t)('auth.error.loginFailed');
      }
    } else if (err.request) {
      // Network error - no response received
      errorMessage = get(t)('auth.error.networkUnreachable');
    } else if (err.message) {
      // Something else happened
      errorMessage = get(t)('auth.error.unexpected');
    }

    return {
      success: false,
      message: errorMessage,
      status: err.response?.status,
      email_not_verified: err.response?.status === 403,
    };
  }
}

/**
 * Self-service password change (`PUT /users/me`).
 *
 * This is the ONLY route (besides logout) that answers while
 * `must_change_password` is set, and succeeding here is what clears the flag
 * server-side — so the lifecycle hold is dropped on success and the next
 * request goes through.
 */
export async function changeOwnPassword(
  currentPassword: string,
  newPassword: string
): Promise<{ success: boolean; message?: string }> {
  try {
    await axiosInstance.put('/users/me', {
      current_password: currentPassword,
      password: newPassword,
    });

    clearAccountLifecycle();
    await fetchUserInfo();
    return { success: true };
  } catch (rawError: unknown) {
    const detail = asAuthError(rawError).response?.data?.detail;

    if (typeof detail === 'string') return { success: false, message: detail };
    if (Array.isArray(detail)) {
      return {
        success: false,
        message: detail
          .map((item: { msg?: string }) => item?.msg ?? String(item))
          .filter(Boolean)
          .join('. '),
      };
    }
    return { success: false, message: get(t)('auth.forcedChange.failed') };
  }
}

// Register function
export async function register(email: string, fullName: string, password: string) {
  try {
    const response = await axiosInstance.post('/auth/register', {
      email,
      full_name: fullName,
      password,
    });

    return { success: true, user: response.data };
  } catch (error: unknown) {
    console.error('auth.ts: Registration error:', error);

    // Handle validation errors (array) vs simple error messages (string)
    let errorMessage = get(t)('auth.error.registrationFailed');
    const detail = asAuthError(error).response?.data?.detail;

    if (detail) {
      if (typeof detail === 'string') {
        errorMessage = detail;
      } else if (Array.isArray(detail)) {
        // Pydantic validation errors - extract messages
        errorMessage = detail
          .map(
            (item: { msg?: string; message?: string }) => item.msg || item.message || String(item)
          )
          .join('. ');
      } else if (typeof detail === 'object') {
        const d = detail as { msg?: string; message?: string };
        errorMessage = d.msg || d.message || JSON.stringify(detail);
      }
    }

    return {
      success: false,
      message: errorMessage,
    };
  }
}

/**
 * Cloud-edition login: called after the hosted sign-in component reports an
 * active session. There is no local credential round-trip — the external IdP
 * owns auth and MFA. We just hydrate the local user store from `/auth/me`
 * (bearer-authenticated by the axios interceptor) so the app shell renders.
 */
export async function loginWithExternalAuth(): Promise<{ success: boolean; message?: string }> {
  try {
    await clearUserState();
    const userData = await fetchUserInfo();
    if (!userData) {
      authStore.reset();
      return { success: false, message: get(t)('auth.error.externalAccountLoadFailed') };
    }
    authStore.setToken('external');
    authStore.setReady(true);
    void loadCapabilities();
    return { success: true };
  } catch (error) {
    console.error('auth.ts: external-auth login hydration failed:', error);
    authStore.reset();
    return { success: false, message: get(t)('auth.error.externalSignInIncomplete') };
  }
}

/**
 * Close the realtime socket so no further frames can be handled.
 *
 * Best-effort and never throws: logout must complete even if the websocket
 * module fails to load.
 */
async function disconnectRealtime(): Promise<void> {
  try {
    const { websocketStore } = await import('$stores/websocket');
    websocketStore.disconnect();
  } catch {
    /* nothing to disconnect */
  }
}

// Logout function
export async function logout() {
  // A voluntary sign-out ends any lifecycle hold. `handleAccountLifecycleError`
  // re-publishes it afterwards for `account_expired`, which must survive the
  // teardown so the login page can explain why the session ended.
  clearAccountLifecycle();

  // Close the WebSocket FIRST — before clearUserState(), not as a side effect of
  // authStore.reset() afterwards.
  //
  // DEFECT: the socket only closed because `websocket.ts` subscribes to
  // `authStore.token` and disconnects on null, and `authStore.reset()` ran AFTER
  // `clearUserState()`. In that window the socket was still open and still
  // handling frames, so one inbound notification re-populated
  // `state.notifications` AND re-wrote `localStorage['notifications']` (every
  // notification handler calls `saveNotificationsToStorage`) after
  // clearUserState had just deleted the key — leaking the previous user's
  // notifications into the next session, on disk. `disconnect()` closes with
  // code 1000, which `onclose` treats as clean and does not reconnect.
  //
  // Dynamic import: `websocket.ts` statically imports this module, so a static
  // import here would be a cycle.
  await disconnectRealtime();

  // Cloud edition: the hosted IdP owns the session. Sign out there (revokes its
  // session + clears its cookies) instead of hitting the local revoke endpoint.
  if (isCloudEdition) {
    try {
      const { externalSignOut } = await import('$lib/cloud');
      await externalSignOut();
    } catch {
      // Ignore — we're tearing the session down regardless.
    }
    abortAllRequests('User logged out');
    await clearUserState();
    authStore.reset();
    return;
  }

  // Notify backend to revoke tokens and clear cookies.
  // This request uses its own session-abort-immune path so the logout call
  // itself is never cancelled by abortAllRequests() below.
  try {
    await axiosInstance.post('/auth/logout');
  } catch {
    // Ignore errors — we're logging out anyway
  }

  // Cancel ALL in-flight requests. This closes the race window where a
  // response could arrive after clearUserState() and repopulate a store with
  // stale data from the previous session.
  abortAllRequests('User logged out');

  // Clear ALL user-specific state (stores, caches, localStorage keys, websocket
  // notifications, in-flight uploads, etc.). This is the single source of truth
  // for session cleanup — see lib/session/clearUserState.ts.
  await clearUserState();

  authStore.reset();
}

// Get available authentication methods
export async function getAuthMethods(): Promise<AuthMethods> {
  try {
    const response = await axiosInstance.get('/auth/methods');
    return response.data;
  } catch (error) {
    console.error('Failed to fetch auth methods:', error);
    // Fail-closed defaults when the probe fails. The two new flags are
    // deliberately ASYMMETRIC:
    //   local_enabled: true  — "closed" here means "still reachable". A
    //     self-hosted install whose /auth/methods probe fails (backend
    //     restarting, proxy hiccup) must still render the sign-in form, or a
    //     transient error locks every operator out of their own deployment.
    //     The backend is the authority and rejects the login anyway if local
    //     auth really is off.
    //   allow_registration: false — "closed" here means "offer nothing". A
    //     signup link we cannot confirm is enabled either 403s after the user
    //     fills the whole form, or advertises an open-registration surface the
    //     operator deliberately turned off. Both are worse than hiding it.
    return {
      methods: ['local'],
      oidc_enabled: false,
      pki_enabled: false,
      ldap_enabled: false,
      local_enabled: true,
      allow_registration: false,
      mfa_enabled: false,
      mfa_required: false,
      login_banner_enabled: false,
      login_banner_text: '',
      login_banner_classification: 'UNCLASSIFIED',
    };
  }
}

// Initiate OIDC login
export async function loginWithOIDC(): Promise<{
  success: boolean;
  message?: string;
}> {
  try {
    const response = await axiosInstance.get('/auth/oidc/login');
    const { authorization_url } = response.data;

    if (!authorization_url) {
      return {
        success: false,
        message: get(t)('auth.error.oidcAuthUrlMissing'),
      };
    }

    // Defense-in-depth: validate the authorization URL before redirecting.
    // Even though this comes from our own backend, a misconfiguration or
    // upstream compromise should not turn into an open redirect or
    // javascript:/data: URL injection.
    let parsed: URL;
    try {
      parsed = new URL(authorization_url);
    } catch {
      console.error('OIDC login: invalid authorization_url format');
      return { success: false, message: get(t)('auth.error.oidcAuthUrlInvalid') };
    }
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
      console.error('OIDC login: non-http(s) protocol rejected', parsed.protocol);
      return { success: false, message: get(t)('auth.error.oidcAuthUrlProtocol') };
    }

    // Redirect to the identity provider's login page
    window.location.href = parsed.toString();
    return { success: true };
  } catch (error: unknown) {
    console.error('OIDC login error:', error);
    return {
      success: false,
      message:
        (asAuthError(error).response?.data?.detail as string) ||
        get(t)('auth.error.oidcInitFailed'),
    };
  }
}

// Handle the OIDC callback (called after redirect back from the provider)
export async function handleOIDCCallback(
  code: string,
  state: string
): Promise<{ success: boolean; message?: string }> {
  try {
    const response = await axiosInstance.get('/auth/oidc/callback', {
      params: { code, state },
    });

    if (response.status === 200 && response.data.access_token) {
      // Clear ALL stale user state from any previous session
      await clearUserState();

      // Token is now in httpOnly cookie
      authStore.setToken('cookie');

      await fetchUserInfo();
      authStore.setReady(true);
      void loadCapabilities();

      return { success: true };
    }

    return {
      success: false,
      message: get(t)('auth.error.oidcCallbackInvalidResponse'),
    };
  } catch (error: unknown) {
    console.error('OIDC callback error:', error);
    return {
      success: false,
      message:
        (asAuthError(error).response?.data?.detail as string) ||
        get(t)('auth.error.oidcCallbackFailed'),
    };
  }
}

// Authenticate with PKI certificate
export async function loginWithPKI(): Promise<{
  success: boolean;
  message?: string;
}> {
  try {
    const response = await axiosInstance.post('/auth/pki/authenticate');

    if (response.status === 200 && response.data.access_token) {
      // Clear ALL stale user state from any previous session
      await clearUserState();

      // Token is now in httpOnly cookie
      authStore.setToken('cookie');

      await fetchUserInfo();
      authStore.setReady(true);
      void loadCapabilities();

      return { success: true };
    }

    return {
      success: false,
      message: get(t)('auth.error.pkiInvalidResponse'),
    };
  } catch (rawError: unknown) {
    const error = asAuthError(rawError);
    console.error('PKI login error:', rawError);

    let message = get(t)('auth.error.pkiFailed');
    if (error.response?.status === 401) {
      message = get(t)('auth.error.pkiCertificateMissing');
    } else if (error.response?.status === 400) {
      message = (error.response?.data?.detail as string) || get(t)('auth.error.pkiNotEnabled');
    }

    return { success: false, message };
  }
}

// Verify MFA code during login
export async function verifyMFA(
  mfaToken: string,
  code: string,
  isBackupCode: boolean = false
): Promise<{ success: boolean; message?: string }> {
  try {
    const response = await axiosInstance.post('/auth/mfa/verify', {
      mfa_token: mfaToken,
      code: code,
      is_backup_code: isBackupCode,
    });

    if (response.status === 200 && response.data.access_token) {
      // Clear ALL stale user state from any previous session
      await clearUserState();

      // Token is now in httpOnly cookie
      authStore.setToken('cookie');

      await fetchUserInfo();
      authStore.setReady(true);
      void loadCapabilities();

      return { success: true };
    }

    return {
      success: false,
      message: get(t)('auth.error.mfaInvalidResponse'),
    };
  } catch (rawError: unknown) {
    const error = asAuthError(rawError);
    console.error('MFA verification error:', rawError);

    let message = get(t)('auth.error.mfaFailed');
    if (error.response?.status === 401) {
      message = (error.response?.data?.detail as string) || get(t)('auth.error.mfaInvalidCode');
    } else if (error.response?.status === 400) {
      message = (error.response?.data?.detail as string) || get(t)('auth.error.mfaInvalidToken');
    } else if (error.response?.status === 429) {
      message = get(t)('auth.error.mfaTooManyAttempts');
    }

    return { success: false, message };
  }
}

// ---------------------------------------------------------------------------
// Forced MFA enrolment
//
// When a deployment sets MFA as required and the account has no second factor,
// /auth/login answers with an enrolment half-token instead of a session. That
// token is NOT an access token: it carries an "mfa" type claim, authorizes only
// /auth/mfa/setup and /auth/mfa/verify-setup, and is burned on the first
// successful verify. Sending it anywhere else (notably /auth/mfa/verify) is a
// 401 by design.
//
// It must be held in memory by the caller and never written to storage. There
// is also no cookie yet, so these two calls authenticate with an explicit
// Authorization header — `withCredentials` alone proves nothing here.
// ---------------------------------------------------------------------------

export interface MfaSetupData {
  secret: string;
  provisioning_uri: string;
  qr_code_base64: string;
}

/**
 * How the UI should react to a failed enrolment call.
 *
 * - `retry`       — wrong code (or an unclassified 4xx); the half-token is still
 *                   good, let the user try again in place.
 * - `expired`     — the half-token is spent or past its lifetime; only a fresh
 *                   login can mint another one.
 * - `restart`     — server state moved on (already enabled / setup not
 *                   initiated); re-run /mfa/setup.
 * - `unavailable` — this account can never enrol here (external IdP owns the
 *                   second factor, or MFA is off system-wide). Not a retry.
 */
type MfaEnrollmentErrorKind = 'retry' | 'expired' | 'restart' | 'unavailable';

export interface MfaEnrollmentError {
  kind: MfaEnrollmentErrorKind;
  message: string;
}

function enrollmentAuthConfig(mfaToken: string) {
  // An empty token means "use the ambient cookie session" — that is the path a
  // voluntary enrolment from Settings would take.
  return mfaToken ? { headers: { Authorization: `Bearer ${mfaToken}` } } : {};
}

function classifyEnrollmentError(rawError: unknown): MfaEnrollmentError {
  const error = asAuthError(rawError);
  const status = error.response?.status;
  const detail = typeof error.response?.data?.detail === 'string' ? error.response.data.detail : '';

  // 401 == the half-token was already claimed or has expired. (The axios
  // response interceptor first attempts a cookie refresh, which cannot succeed
  // during enrolment and rejects with its own 401 — either way we land here.)
  if (status === 401) {
    return { kind: 'expired', message: get(t)('auth.mfaEnroll.error.tokenExpired') };
  }

  if (status === 400) {
    // The wire carries no error code, so the API's own English `detail` is the
    // only signal separating "type the code again" from "this account can never
    // enrol". Match narrowly and treat anything unrecognised as a retry — the
    // safe default, since it leaves the user in the flow.
    const lower = detail.toLowerCase();
    if (
      lower.includes('not available for your authentication type') ||
      lower.includes('not enabled on this system')
    ) {
      return { kind: 'unavailable', message: get(t)('auth.mfaEnroll.error.unavailable') };
    }
    if (lower.includes('already enabled') || lower.includes('setup not initiated')) {
      return { kind: 'restart', message: get(t)('auth.mfaEnroll.error.restart') };
    }
    return { kind: 'retry', message: get(t)('auth.mfaEnroll.error.invalidCode') };
  }

  return { kind: 'retry', message: detail || get(t)('auth.mfaEnroll.error.setupFailed') };
}

/**
 * Begin (or re-render) TOTP enrolment. Safe to call repeatedly: /mfa/setup does
 * not consume the half-token, so a page refresh mid-enrolment is recoverable.
 */
export async function setupMfaEnrollment(
  mfaToken: string = ''
): Promise<{ success: true; data: MfaSetupData } | { success: false; error: MfaEnrollmentError }> {
  try {
    const response = await axiosInstance.post(
      '/auth/mfa/setup',
      undefined,
      enrollmentAuthConfig(mfaToken)
    );
    return { success: true, data: response.data };
  } catch (rawError: unknown) {
    console.error('auth.ts: MFA enrolment setup failed:', rawError);
    return { success: false, error: classifyEnrollmentError(rawError) };
  }
}

/**
 * Complete enrolment with the first TOTP code.
 *
 * On success the response carries backup codes AND a real session (cookies are
 * set), so the user is logged in — the caller must NOT hit /auth/login again.
 * We hydrate the store here exactly as the other login paths do.
 */
export async function verifyMfaEnrollment(
  code: string,
  mfaToken: string = ''
): Promise<
  { success: true; backupCodes: string[] } | { success: false; error: MfaEnrollmentError }
> {
  try {
    const response = await axiosInstance.post(
      '/auth/mfa/verify-setup',
      { code },
      enrollmentAuthConfig(mfaToken)
    );

    // Clear ALL stale user state from any previous session
    await clearUserState();

    authStore.setToken('cookie');
    await fetchUserInfo();
    authStore.setReady(true);
    void loadCapabilities();

    return { success: true, backupCodes: response.data?.backup_codes ?? [] };
  } catch (rawError: unknown) {
    console.error('auth.ts: MFA enrolment verification failed:', rawError);
    return { success: false, error: classifyEnrollmentError(rawError) };
  }
}
