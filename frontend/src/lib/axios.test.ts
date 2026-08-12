/**
 * Tests for `$lib/axios` — the shared axios instance, its two interceptors, and
 * the session abort signal.
 *
 * DEFECT THESE CATCH: 15 of 78 lines were covered and BOTH interceptor bodies
 * were at 0%. Everything that makes the app's auth work lives in those bodies:
 *
 *  - **The 401-refresh exclusion list.** Drop `/auth/me` and app bootstrap becomes
 *    an infinite refresh loop (bootstrap 401s → refresh → `/auth/me` 401s → …).
 *    Drop `/auth/token/refresh` and it is unbounded RECURSION: the refresh call's
 *    own 401 re-enters the refresh branch.
 *  - **The concurrent-401 queue.** Queued requests replay via
 *    `.then(() => axiosInstance(originalRequest))` WITHOUT setting `_retry`, so on
 *    a hard-expired session each queued request re-enters the refresh branch and
 *    fires its own refresh and its own `window.location.href = '/login'`.
 *  - **CSRF attach.** `getCsrfToken()` does `.split('=')[1]`, so a base64 cookie
 *    value containing `=` padding is TRUNCATED and every mutation 403s.
 *  - **The session-signal attach and its `isAuthEndpoint` carve-out.** The signal
 *    is what stops a previous session's in-flight response repopulating a store;
 *    the carve-out is what lets `POST /auth/logout` complete. Invert it and logout
 *    stops revoking server-side.
 *
 * The interceptors are exercised through a real axios adapter rather than by
 * calling the handler functions directly: the ordering and re-entry behaviour
 * being tested only exists when axios itself drives them.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { InternalAxiosRequestConfig, AxiosResponse } from 'axios';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

import axiosInstance, { abortAllRequests, getCsrfToken, isRequestCancelled } from '$lib/axios';

/** One recorded request, as the interceptors left it. */
interface Seen {
  url: string;
  method: string;
  headers: Record<string, unknown>;
  signal: AbortSignal | undefined;
  retryFlag: boolean;
}

let seen: Seen[] = [];

/**
 * Per-URL status script. `statuses[url]` is consumed one entry per request;
 * once exhausted (or absent) the URL replies 200.
 *
 * The adapter must NEVER clone or mutate `config`: axios's response interceptor
 * marks the retried request by setting `_retry` ON THAT OBJECT, so a fresh
 * config per call makes the guard un-settable and the refresh loop unbounded.
 */
let statuses: Record<string, number[]> = {};
/** URLs that reply with this status forever. */
let alwaysStatus: Record<string, number> = {};

function nextStatus(url: string): number {
  const scripted = statuses[url];
  if (scripted && scripted.length) return scripted.shift()!;
  if (url in alwaysStatus) return alwaysStatus[url];
  return 200;
}

function stubAdapter(config: InternalAxiosRequestConfig): Promise<AxiosResponse> {
  const url = config.url ?? '';
  seen.push({
    url,
    method: (config.method ?? 'get').toLowerCase(),
    headers: { ...config.headers },
    signal: config.signal as AbortSignal | undefined,
    retryFlag: (config as unknown as { _retry?: boolean })._retry === true,
  });

  if (config.signal?.aborted) {
    return Promise.reject(
      Object.assign(new Error('canceled'), {
        code: 'ERR_CANCELED',
        name: 'CanceledError',
        config,
      })
    );
  }

  const status = nextStatus(url);
  const response = {
    status,
    statusText: String(status),
    data: {},
    headers: {},
    config,
  } as AxiosResponse;

  if (status >= 200 && status < 300) return Promise.resolve(response);
  return Promise.reject(
    Object.assign(new Error(`Request failed with status code ${status}`), {
      response,
      config,
      isAxiosError: true,
    })
  );
}

function urlsSeen(): string[] {
  return seen.map((s) => s.url);
}

function countSeen(url: string): number {
  return seen.filter((s) => s.url === url).length;
}

/** jsdom keeps cookies until they are expired; assigning '' does nothing. */
function clearCookies(): void {
  for (const pair of document.cookie.split(';')) {
    const name = pair.split('=')[0].trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
  }
}

const originalAdapter = axiosInstance.defaults.adapter;
let locationHref = '';

beforeEach(() => {
  seen = [];
  statuses = {};
  alwaysStatus = {};
  locationHref = '';
  axiosInstance.defaults.adapter = stubAdapter;
  clearCookies();
  // jsdom forbids assigning window.location; intercept the href setter instead.
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: {
      pathname: '/files',
      set href(value: string) {
        locationHref = value;
      },
      get href() {
        return locationHref;
      },
    },
  });
});

afterEach(() => {
  axiosInstance.defaults.adapter = originalAdapter;
  clearCookies();
  vi.restoreAllMocks();
});

// ─────────────────────────────────────────────────────────────────────────────
// getCsrfToken
// ─────────────────────────────────────────────────────────────────────────────

describe('getCsrfToken', () => {
  it('reads the csrf_token cookie', () => {
    document.cookie = 'csrf_token=abc123';
    expect(getCsrfToken()).toBe('abc123');
  });

  it('is not confused by another cookie whose name ENDS with csrf_token', () => {
    document.cookie = 'x_csrf_token=wrong';
    document.cookie = 'csrf_token=right';
    expect(getCsrfToken()).toBe('right');
  });

  it('returns undefined when the cookie is absent', () => {
    document.cookie = 'session=1';
    expect(getCsrfToken()).toBeUndefined();
  });

  it('TRUNCATES a value containing "=" — the base64-padding defect', () => {
    // `.split('=')[1]` keeps only the first segment. A base64/base64url token
    // with `=` padding (or any `=` at all) is silently cut short, the backend's
    // double-submit comparison fails, and EVERY mutation 403s with no client-side
    // error to point at. Pinned as a known limitation so a change to the token
    // format is a deliberate decision, not a mystery outage.
    document.cookie = 'csrf_token=YWJjZGVmZw==';
    expect(getCsrfToken()).toBe('YWJjZGVmZw');
    expect(getCsrfToken()).not.toBe('YWJjZGVmZw==');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Request interceptor
// ─────────────────────────────────────────────────────────────────────────────

describe('request interceptor — CSRF', () => {
  it.each(['post', 'put', 'patch', 'delete'] as const)(
    'attaches X-CSRF-Token to %s',
    async (method) => {
      document.cookie = 'csrf_token=tok';
      await axiosInstance.request({ url: '/files/1', method });

      expect(seen[0].headers['X-CSRF-Token']).toBe('tok');
    }
  );

  it('does NOT attach X-CSRF-Token to a GET', async () => {
    document.cookie = 'csrf_token=tok';
    await axiosInstance.get('/files');

    expect(seen[0].headers['X-CSRF-Token']).toBeUndefined();
  });

  it('omits the header entirely when no cookie exists (rather than sending empty)', async () => {
    // An empty header is worse than none: it passes a `toBeDefined()` style check
    // while authenticating nothing — the exact trap the auditor's `weak-only`
    // detector exists for.
    await axiosInstance.post('/files', {});

    expect(seen[0].headers['X-CSRF-Token']).toBeUndefined();
  });
});

describe('request interceptor — session abort signal', () => {
  it('attaches the session signal to an ordinary request', async () => {
    await axiosInstance.get('/files');

    expect(seen[0].signal).toBeInstanceOf(AbortSignal);
  });

  it('does not overwrite a caller-provided signal', async () => {
    const own = new AbortController();
    await axiosInstance.get('/files', { signal: own.signal });

    expect(seen[0].signal).toBe(own.signal);
  });

  it.each(['/auth/logout', '/auth/token/refresh', '/auth/login'])(
    'leaves %s UNSIGNALLED so logout can never cancel it',
    async (url) => {
      // Invert this carve-out and `abortAllRequests()` cancels the very
      // `POST /auth/logout` that revokes the session server-side — the client
      // looks logged out while the refresh cookie stays valid.
      await axiosInstance.post(url, {});

      expect(seen[0].signal).toBeUndefined();
    }
  );

  it('abortAllRequests cancels a request issued before it, and the next one is fresh', async () => {
    const signalBefore = await axiosInstance.get('/files').then(() => seen[0].signal!);

    abortAllRequests('User logged out');
    expect(signalBefore.aborted).toBe(true);

    // A brand-new controller means the NEXT session's requests are not
    // pre-aborted. Without the reset, every request after one logout fails.
    seen = [];
    await axiosInstance.get('/files');
    expect(seen[0].signal!.aborted).toBe(false);
  });

  it('a request made while aborted rejects as cancelled, not as an error to toast', async () => {
    // `isRequestCancelled` is what suppresses error toasts on logout/navigation.
    const controller = new AbortController();
    controller.abort();

    await expect(axiosInstance.get('/files', { signal: controller.signal })).rejects.toSatisfy(
      isRequestCancelled
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Response interceptor — 401 refresh
// ─────────────────────────────────────────────────────────────────────────────

describe('response interceptor — 401 auto-refresh', () => {
  it('refreshes once and retries the original request', async () => {
    statuses = { '/files': [401, 200] };

    const response = await axiosInstance.get('/files');

    expect(response.status).toBe(200);
    expect(urlsSeen()).toEqual(['/files', '/auth/token/refresh', '/files']);
    // The retry carries `_retry`, which is what stops a second 401 looping.
    expect(seen[2].retryFlag).toBe(true);
  });

  it.each([
    ['/auth/token/refresh', 'unbounded recursion — the refresh call refreshing itself'],
    ['/auth/login', 'a refresh attempt on a failed sign-in'],
    ['/auth/me', 'an infinite refresh loop at app bootstrap'],
    ['/auth/mfa/setup', 'a guaranteed-useless round-trip on a spent half-token'],
    ['/auth/mfa/verify-setup', 'a guaranteed-useless round-trip on a spent half-token'],
  ])('never refreshes on a 401 from %s (%s)', async (url) => {
    alwaysStatus = { [url]: 401 };

    await expect(axiosInstance.get(url)).rejects.toBeDefined();

    // Exactly one attempt: no refresh round-trip, no retry.
    expect(seen).toHaveLength(1);
    expect(countSeen(url)).toBe(1);
  });

  it('DOES refresh on a 401 from an ordinary endpoint (calibrates the list above)', async () => {
    // Without this control, the exclusion tests would pass just as happily if the
    // refresh branch were dead code.
    statuses = { '/files': [401, 200] };

    await axiosInstance.get('/files');

    expect(countSeen('/auth/token/refresh')).toBe(1);
  });

  it('SUBSTRING MATCH: "/auth/methods" is excluded because it contains "/auth/me"', async () => {
    // The exclusion list uses `url.includes('/auth/me')`, which also matches
    // `/auth/methods` (and anything else prefixed `/auth/me`). Harmless today —
    // `/auth/methods` is unauthenticated and does not 401 — but it means the list
    // is a substring filter, not a path filter. Pinned so a future `/auth/members`
    // or `/auth/message` endpoint does not silently lose 401 recovery.
    alwaysStatus = { '/auth/methods': 401 };

    await expect(axiosInstance.get('/auth/methods')).rejects.toBeDefined();

    expect(urlsSeen()).not.toContain('/auth/token/refresh');
  });

  it('fires exactly ONE refresh for concurrent 401s', async () => {
    // The shared `isRefreshing` flag + queue exist for this. N refreshes would
    // spend the single-use refresh token N times and log the user out.
    statuses = { '/a': [401, 200], '/b': [401, 200], '/c': [401, 200] };

    await Promise.all([axiosInstance.get('/a'), axiosInstance.get('/b'), axiosInstance.get('/c')]);

    expect(countSeen('/auth/token/refresh')).toBe(1);
    // All three were retried and all three succeeded.
    expect(countSeen('/a')).toBe(2);
    expect(countSeen('/b')).toBe(2);
    expect(countSeen('/c')).toBe(2);
  });

  it('redirects to /login once when the refresh itself fails', async () => {
    alwaysStatus = { '/files': 401, '/auth/token/refresh': 401 };

    await expect(axiosInstance.get('/files')).rejects.toBeDefined();

    expect(locationHref).toBe('/login');
    // One refresh attempt, not a cascade.
    expect(countSeen('/auth/token/refresh')).toBe(1);
  });

  it('does not redirect when already on /login', async () => {
    (window.location as unknown as { pathname: string }).pathname = '/login';
    alwaysStatus = { '/files': 401, '/auth/token/refresh': 401 };

    await expect(axiosInstance.get('/files')).rejects.toBeDefined();

    expect(urlsSeen()).toContain('/auth/token/refresh');
    // A redirect to the page we are already on would wipe the sign-in form.
    expect(locationHref).toBe('');
  });

  it('QUEUE DEFECT: a queued replay carries no _retry, so a hard-expired session refreshes again', async () => {
    // Documents LIVE behaviour, not the ideal. Queued requests replay via
    // `.then(() => axiosInstance(originalRequest))`; `_retry` is set only on the
    // request that WON the race. So when the session is hard-expired (the retry
    // 401s too), each queued request re-enters the refresh branch and fires its
    // own refresh and its own `location.href = '/login'`.
    //
    // If someone sets `_retry` on the queued replay this count drops to 1 — update
    // the expectation then. It exists so the change is noticed either way.
    alwaysStatus = { '/a': 401, '/b': 401, '/c': 401, '/auth/token/refresh': 200 };

    const results = await Promise.allSettled([
      axiosInstance.get('/a'),
      axiosInstance.get('/b'),
      axiosInstance.get('/c'),
    ]);

    expect(results.every((r) => r.status === 'rejected')).toBe(true);
    expect(countSeen('/auth/token/refresh')).toBeGreaterThan(1);
  });

  it('does not treat a cancelled request as a 401 to refresh', async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(axiosInstance.get('/files', { signal: controller.signal })).rejects.toBeDefined();

    expect(urlsSeen()).not.toContain('/auth/token/refresh');
    expect(locationHref).toBe('');
  });

  it('logs a 5xx and rejects without refreshing', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    alwaysStatus = { '/files': 503 };

    await expect(axiosInstance.get('/files')).rejects.toBeDefined();

    expect(urlsSeen()).not.toContain('/auth/token/refresh');
    expect(spy).toHaveBeenCalled();
  });

  it('passes a 403 straight through — lifecycle 403s are auth.ts’s job', async () => {
    alwaysStatus = { '/files': 403 };

    await expect(axiosInstance.get('/files')).rejects.toBeDefined();

    expect(urlsSeen()).not.toContain('/auth/token/refresh');
  });
});
