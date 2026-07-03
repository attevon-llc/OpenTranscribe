import axios from 'axios';
import { isCloudEdition } from '$lib/edition';

// Create axios instance with consistent base URL for all environments
// This ensures the same behavior in development and production with nginx
export const axiosInstance = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
  // Timeout for API requests (60s covers filter queries under heavy processing load)
  timeout: 60000,
  // Let Axios handle 4xx and 5xx as errors appropriately
  validateStatus: (status) => status >= 200 && status < 300,
  // Enable automatic redirect following
  maxRedirects: 5,
  // Send cookies with every request (httpOnly auth cookies).
  // Community: httpOnly auth cookies. Cloud: not used for auth (bearer instead),
  // but harmless to keep enabled.
  withCredentials: true,
});

export default axiosInstance;

/**
 * Session-scoped AbortController.
 *
 * Every request that passes through axiosInstance gets this signal attached
 * (unless the caller provides their own signal). On logout, `abortAllRequests()`
 * triggers the signal, cancelling all in-flight requests and preventing their
 * responses from updating stores with stale data from the previous session.
 *
 * After abort, a fresh controller is created so the next session starts clean.
 */
let sessionAbortController = new AbortController();

/**
 * Cancel all in-flight axios requests and reset the session signal.
 * Called from `auth.ts` logout() to close the race window where a response
 * could resolve after `clearUserState()` and repopulate a store.
 */
export function abortAllRequests(reason: string = 'Session ended'): void {
  sessionAbortController.abort(reason);
  // Reset for the next session — subsequent requests get a fresh signal
  sessionAbortController = new AbortController();
}

/**
 * Type guard: distinguishes a user-cancelled request (from abortAllRequests)
 * from a real error. Use this in catch blocks to suppress error toasts when
 * a request was cancelled due to logout/navigation.
 */
export function isRequestCancelled(error: unknown): boolean {
  if (axios.isCancel(error)) return true;
  if (error && typeof error === 'object') {
    const err = error as { code?: string; name?: string; message?: string };
    if (err.code === 'ERR_CANCELED') return true;
    if (err.name === 'CanceledError') return true;
    if (err.name === 'AbortError') return true;
  }
  return false;
}

// Helper to read the csrf_token cookie (non-httpOnly, readable by JS)
// Exported for use by code that bypasses axiosInstance (e.g. raw fetch)
export function getCsrfToken(): string | undefined {
  return document.cookie
    .split(';')
    .find((c) => c.trim().startsWith('csrf_token='))
    ?.split('=')[1];
}

// Request interceptor: CSRF token + (cloud) external bearer + session abort signal
axiosInstance.interceptors.request.use(
  async (config) => {
    if (isCloudEdition) {
      // Cloud edition: mint a FRESH hosted-IdP session JWT per request. These
      // tokens are short-lived (~60s), so we never cache them and never use the
      // cookie /auth/token/refresh path (which would log users out mid-session).
      // The backend's external verifier validates this bearer.
      try {
        const { getSessionToken } = await import('$lib/cloud');
        const sessionToken = await getSessionToken();
        if (sessionToken) {
          config.headers['Authorization'] = `Bearer ${sessionToken}`;
        }
      } catch {
        // No active external session — request proceeds unauthenticated; the
        // backend will 401 as appropriate.
      }
    } else {
      // Community edition: CSRF token for mutating requests (double-submit
      // pattern), paired with httpOnly auth cookies. Cloud uses bearer auth and
      // does not rely on the CSRF cookie.
      const method = (config.method || '').toLowerCase();
      if (['post', 'put', 'patch', 'delete'].includes(method)) {
        const csrfToken = getCsrfToken();
        if (csrfToken) {
          config.headers['X-CSRF-Token'] = csrfToken;
        }
      }
    }

    // Attach the session abort signal so logout can cancel this request.
    // Skip if the caller already provided their own signal (e.g. prefetch
    // utilities that manage their own cancellation lifecycle) or if the
    // request is an auth endpoint that should never be cancelled by logout
    // (the logout endpoint itself must complete).
    const url = config.url || '';
    const isAuthEndpoint =
      url.includes('/auth/logout') ||
      url.includes('/auth/token/refresh') ||
      url.includes('/auth/login');
    if (!config.signal && !isAuthEndpoint) {
      config.signal = sessionAbortController.signal;
    }

    return config;
  },
  (error) => {
    console.error('[Axios] Request error:', error);
    return Promise.reject(error);
  }
);

// Token refresh state — shared across concurrent 401s so only one refresh fires
let isRefreshing = false;
let refreshQueue: Array<{
  resolve: (value?: unknown) => void;
  reject: (reason?: unknown) => void;
}> = [];

function processQueue(error: unknown | null) {
  refreshQueue.forEach((p) => (error ? p.reject(error) : p.resolve()));
  refreshQueue = [];
}

// Response interceptor — auto-refresh on 401, log 5xx
axiosInstance.interceptors.response.use(
  (response) => response,
  async (error) => {
    // Suppress cancelled-request errors — they're expected on logout/navigation
    // and should not trigger 401 refresh or error logging.
    if (isRequestCancelled(error)) {
      return Promise.reject(error);
    }

    // Quota exceeded (cloud edition): the backend returns HTTP 402 when an upload
    // would push the org over its plan limit. Pop the shared QuotaExceededModal +
    // upgrade CTA. Still rejects so the caller's catch runs (no double toast — the
    // modal carries the message). Lazy-imported to avoid pulling cloud stores into
    // the community bundle's eager graph.
    if (isCloudEdition && error.response?.status === 402) {
      try {
        const detail = error.response?.data?.detail;
        const message = typeof detail === 'string' ? detail : '';
        const { showQuotaExceeded } = await import('$lib/cloud');
        showQuotaExceeded(message);
      } catch {
        // Modal trigger failed — fall through to normal rejection.
      }
      return Promise.reject(error);
    }

    const originalRequest = error.config;

    // Cloud edition: there is NO cookie refresh token. The hosted IdP mints a
    // fresh short-lived bearer on the next request, so a 401 here means the
    // external session is gone (or the token was rejected) — bounce to login
    // rather than attempting the cookie /auth/token/refresh flow.
    if (isCloudEdition) {
      if (
        error.response?.status === 401 &&
        typeof window !== 'undefined' &&
        !window.location.pathname.startsWith('/login')
      ) {
        const { authStore } = await import('../stores/auth');
        authStore.reset();
        window.location.href = '/login';
      }
      if (error.response?.status >= 500) {
        console.error(
          `Server error for ${error.config?.url}: ${error.response.status} - ${JSON.stringify(
            error.response.data
          )}`
        );
      }
      return Promise.reject(error);
    }

    // Auto-refresh: if we get a 401 and haven't already retried this request
    if (
      error.response?.status === 401 &&
      originalRequest &&
      !originalRequest._retry &&
      // Don't try to refresh on auth endpoints themselves
      !originalRequest.url?.includes('/auth/login') &&
      !originalRequest.url?.includes('/auth/token/refresh') &&
      !originalRequest.url?.includes('/auth/me')
    ) {
      if (isRefreshing) {
        // Another refresh is in progress — queue this request
        return new Promise((resolve, reject) => {
          refreshQueue.push({ resolve, reject });
        }).then(() => axiosInstance(originalRequest));
      }

      originalRequest._retry = true;
      isRefreshing = true;

      try {
        // Call refresh endpoint — refresh_token cookie is sent automatically
        // (it's scoped to /api/auth path)
        await axiosInstance.post('/auth/token/refresh', {});

        // Refresh succeeded — new cookies are set, retry queued requests
        processQueue(null);

        // Retry the original failed request
        return axiosInstance(originalRequest);
      } catch (refreshError) {
        // Refresh failed — session is truly expired, redirect to login
        processQueue(refreshError);

        // Lazy import to avoid circular dependency
        const { authStore } = await import('../stores/auth');
        authStore.reset();

        // Only redirect if we're not already on the login page
        if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
          window.location.href = '/login';
        }

        return Promise.reject(refreshError);
      } finally {
        isRefreshing = false;
      }
    }

    // Log server errors (5xx) - client errors are often expected
    if (error.response?.status >= 500) {
      console.error(
        `Server error for ${error.config?.url}: ${error.response.status} - ${JSON.stringify(
          error.response.data
        )}`
      );
    }
    return Promise.reject(error);
  }
);
