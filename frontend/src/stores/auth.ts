import { writable, derived, get } from 'svelte/store';
import axiosInstance, { abortAllRequests } from '../lib/axios';
import { t } from '$stores/locale';
import { clearUserState } from '$lib/session/clearUserState';
import { isCloudEdition } from '$lib/edition';

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

// Certificate information for PKI authentication
export interface CertificateInfo {
  has_certificate: boolean;
  subject_dn?: string;
  serial_number?: string;
  issuer_dn?: string;
  organization?: string;
  organizational_unit?: string;
  valid_from?: string;
  valid_until?: string;
  fingerprint?: string;
}

// Define user interface
export interface User {
  uuid: string;
  email: string;
  full_name: string;
  role: 'user' | 'admin' | 'super_admin';
  // Authentication type. External/SSO provider strings beyond the core four
  // are registered by the managed edition's auth layer; the password-change UI
  // keys off `auth_type === 'local'`.
  auth_type: 'local' | 'ldap' | 'keycloak' | 'pki' | string;
  allow_local_fallback?: boolean;
  certificate?: CertificateInfo;
  // Cloud-edition tenancy/billing context (populated by the backend from the
  // external IdP's org claim). Absent in the community edition.
  org_id?: string;
  org_role?: string;
  subscription_tier?: string;
  created_at: string;
  updated_at: string;
}

// Available authentication methods
export interface AuthMethods {
  methods: string[];
  keycloak_enabled: boolean;
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
export interface AuthState {
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

    await fetchUserInfo();

    authStore.setReady(true);

    return { success: true };
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
    };
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
    return { success: true };
  } catch (error) {
    console.error('auth.ts: external-auth login hydration failed:', error);
    authStore.reset();
    return { success: false, message: get(t)('auth.error.externalSignInIncomplete') };
  }
}

// Logout function
export async function logout() {
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
      keycloak_enabled: false,
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

// Initiate Keycloak login
export async function loginWithKeycloak(): Promise<{
  success: boolean;
  message?: string;
}> {
  try {
    const response = await axiosInstance.get('/auth/keycloak/login');
    const { authorization_url } = response.data;

    if (!authorization_url) {
      return {
        success: false,
        message: get(t)('auth.error.keycloakAuthUrlMissing'),
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
      console.error('Keycloak login: invalid authorization_url format');
      return { success: false, message: get(t)('auth.error.keycloakAuthUrlInvalid') };
    }
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
      console.error('Keycloak login: non-http(s) protocol rejected', parsed.protocol);
      return { success: false, message: get(t)('auth.error.keycloakAuthUrlProtocol') };
    }

    // Redirect to Keycloak login page
    window.location.href = parsed.toString();
    return { success: true };
  } catch (error: unknown) {
    console.error('Keycloak login error:', error);
    return {
      success: false,
      message:
        (asAuthError(error).response?.data?.detail as string) ||
        get(t)('auth.error.keycloakInitFailed'),
    };
  }
}

// Handle Keycloak callback (called after redirect back from Keycloak)
export async function handleKeycloakCallback(
  code: string,
  state: string
): Promise<{ success: boolean; message?: string }> {
  try {
    const response = await axiosInstance.get('/auth/keycloak/callback', {
      params: { code, state },
    });

    if (response.status === 200 && response.data.access_token) {
      // Clear ALL stale user state from any previous session
      await clearUserState();

      // Token is now in httpOnly cookie
      authStore.setToken('cookie');

      await fetchUserInfo();
      authStore.setReady(true);

      return { success: true };
    }

    return {
      success: false,
      message: get(t)('auth.error.keycloakCallbackInvalidResponse'),
    };
  } catch (error: unknown) {
    console.error('Keycloak callback error:', error);
    return {
      success: false,
      message:
        (asAuthError(error).response?.data?.detail as string) ||
        get(t)('auth.error.keycloakCallbackFailed'),
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
      message =
        (error.response?.data?.detail as string) || get(t)('auth.error.pkiNotEnabled');
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
      message =
        (error.response?.data?.detail as string) || get(t)('auth.error.mfaInvalidToken');
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
export type MfaEnrollmentErrorKind = 'retry' | 'expired' | 'restart' | 'unavailable';

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

    return { success: true, backupCodes: response.data?.backup_codes ?? [] };
  } catch (rawError: unknown) {
    console.error('auth.ts: MFA enrolment verification failed:', rawError);
    return { success: false, error: classifyEnrollmentError(rawError) };
  }
}

// Fetch certificate info for PKI authenticated users
export async function fetchCertificateInfo(): Promise<CertificateInfo | null> {
  try {
    const response = await axiosInstance.get('/auth/me/certificate');
    return response.data;
  } catch (error) {
    console.error('Failed to fetch certificate info:', error);
    return null;
  }
}
