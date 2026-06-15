import { writable, derived, get } from 'svelte/store';
import axiosInstance, { abortAllRequests } from '../lib/axios';
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
  // Authentication type. 'clerk' (and other external/SSO providers) only appear
  // in the cloud edition; the password-change UI keys off `auth_type === 'local'`.
  auth_type: 'local' | 'ldap' | 'keycloak' | 'pki' | 'clerk' | string;
  allow_local_fallback?: boolean;
  certificate?: CertificateInfo;
  // Cloud-edition tenancy/billing context (populated by the backend from the
  // Clerk org claim). Absent in the community edition.
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
 * Cloud-edition session init: read the Clerk session instead of the cookie
 * `/auth/session` probe. If Clerk has an active session, the per-request axios
 * interceptor mints a bearer token and `/auth/me` is authenticated by the
 * backend's external verifier. Community edition never reaches this path.
 */
async function initAuthClerk(): Promise<void> {
  try {
    const { loadClerk, hasClerkSession } = await import('$lib/clerk');
    await loadClerk();

    if (await hasClerkSession()) {
      // Bearer is attached per-request by the axios interceptor (cloud build).
      const userData = await fetchUserInfo();
      if (userData) {
        authStore.setToken('clerk');
        authStore.setReady(true);
        return;
      }
    }

    authStore.reset();
  } catch (error) {
    console.error('auth.ts: Clerk initAuth failed:', error);
    authStore.reset();
  }
}

// Initialize auth state by verifying the cookie session with the backend
export async function initAuth() {
  authStore.setReady(false);

  // Clear any legacy localStorage tokens (migration from pre-cookie auth)
  localStorage.removeItem('token');
  localStorage.removeItem('user');

  // Cloud edition delegates session detection to Clerk (no cookie session).
  if (isCloudEdition) {
    await initAuthClerk();
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
  mfa_required?: boolean;
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

    // Check if MFA is required
    if (response.status === 200 && response.data.mfa_required) {
      return {
        success: false,
        mfa_required: true,
        mfa_token: response.data.mfa_token,
      };
    }

    if (response.status !== 200 || !response.data.access_token) {
      console.error('auth.ts: Invalid login response');
      return { success: false, message: 'Invalid login response from server' };
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
    let errorMessage = 'Login failed. Please check your credentials and try again.';

    if (err.response) {
      // Server responded with an error status
      switch (err.response.status) {
        case 401:
          errorMessage =
            (err.response.data?.detail as string) || 'Invalid email or password. Please try again.';
          break;
        case 400:
          errorMessage =
            (err.response.data?.detail as string) || 'Invalid request. Please check your input.';
          break;
        case 429:
          errorMessage = 'Too many login attempts. Please try again later.';
          break;
        case 500:
        case 502:
        case 503:
          errorMessage = 'Server error. Please try again later.';
          break;
        default:
          errorMessage =
            (err.response.data?.detail as string) ||
            (err.response.data?.message as string) ||
            'Login failed. Please try again.';
      }
    } else if (err.request) {
      // Network error - no response received
      errorMessage = 'Unable to connect to the server. Please check your internet connection.';
    } else if (err.message) {
      // Something else happened
      errorMessage = 'An unexpected error occurred. Please try again.';
    }

    return {
      success: false,
      message: errorMessage,
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
    let errorMessage = 'Registration failed. Please try again.';
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
 * Cloud-edition login: called after Clerk's prebuilt `<SignIn/>` reports an
 * active session. There is no local credential round-trip — Clerk owns auth and
 * MFA. We just hydrate the local user store from `/auth/me` (bearer-authenticated
 * by the axios interceptor) so the app shell renders.
 */
export async function loginWithClerk(): Promise<{ success: boolean; message?: string }> {
  try {
    await clearUserState();
    const userData = await fetchUserInfo();
    if (!userData) {
      authStore.reset();
      return { success: false, message: 'Failed to load account after sign-in.' };
    }
    authStore.setToken('clerk');
    authStore.setReady(true);
    return { success: true };
  } catch (error) {
    console.error('auth.ts: Clerk login hydration failed:', error);
    authStore.reset();
    return { success: false, message: 'Sign-in could not be completed.' };
  }
}

// Logout function
export async function logout() {
  // Cloud edition: Clerk owns the session. Sign out of Clerk (revokes its
  // session + clears its cookies) instead of hitting the local revoke endpoint.
  if (isCloudEdition) {
    try {
      const { clerkSignOut } = await import('$lib/clerk');
      await clerkSignOut();
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
    // Return default methods if fetch fails
    return {
      methods: ['local'],
      keycloak_enabled: false,
      pki_enabled: false,
      ldap_enabled: false,
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
        message: 'Failed to get Keycloak authorization URL',
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
      return { success: false, message: 'Invalid authorization URL' };
    }
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
      console.error('Keycloak login: non-http(s) protocol rejected', parsed.protocol);
      return { success: false, message: 'Invalid authorization URL protocol' };
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
        'Failed to initiate Keycloak login',
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
      message: 'Invalid response from Keycloak callback',
    };
  } catch (error: unknown) {
    console.error('Keycloak callback error:', error);
    return {
      success: false,
      message:
        (asAuthError(error).response?.data?.detail as string) ||
        'Failed to complete Keycloak authentication',
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
      message: 'Invalid response from PKI authentication',
    };
  } catch (rawError: unknown) {
    const error = asAuthError(rawError);
    console.error('PKI login error:', rawError);

    let message = 'PKI authentication failed';
    if (error.response?.status === 401) {
      message = 'Invalid or missing client certificate';
    } else if (error.response?.status === 400) {
      message = (error.response?.data?.detail as string) || 'PKI authentication is not enabled';
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
      message: 'Invalid response from MFA verification',
    };
  } catch (rawError: unknown) {
    const error = asAuthError(rawError);
    console.error('MFA verification error:', rawError);

    let message = 'MFA verification failed';
    if (error.response?.status === 401) {
      message = (error.response?.data?.detail as string) || 'Invalid verification code';
    } else if (error.response?.status === 400) {
      message = (error.response?.data?.detail as string) || 'Invalid MFA token or code';
    } else if (error.response?.status === 429) {
      message = 'Too many verification attempts. Please try again later.';
    }

    return { success: false, message };
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
