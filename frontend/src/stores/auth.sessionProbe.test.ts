/**
 * `initAuth()` is the gate in front of the whole app, so what it does when the backend is slow
 * or down decides whether a visitor sees a retry state or a minute of nothing, and whether a
 * valid session gets bounced to /login. It also owns the one capabilities fetch for a page load
 * with a session (the layout used to fire a second, parallel one).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn() },
  abortAllRequests: vi.fn(),
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));
vi.mock('$lib/session/clearUserState', () => ({ clearUserState: vi.fn() }));
vi.mock('$lib/edition', () => ({ isCloudEdition: false }));
vi.mock('$stores/capabilities', () => ({ loadCapabilities: vi.fn() }));

import axiosInstance from '$lib/axios';
import { loadCapabilities } from '$stores/capabilities';
import { authStore, initAuth, isAuthenticated } from './auth';
import {
  retrySessionProbeNow,
  SESSION_PROBE_TIMEOUT_MS,
  sessionProbe,
} from '$lib/auth/sessionProbe';

const mockedGet = vi.mocked(axiosInstance.get);
const mockedPost = vi.mocked(axiosInstance.post);
const mockedCaps = vi.mocked(loadCapabilities);

const USER = { id: 1, email: 'a@example.com', role: 'admin' };

beforeEach(() => {
  vi.clearAllMocks();
  authStore.reset();
  authStore.setReady(false);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('initAuth — the session probe', () => {
  it('bounds the probe well under the 60 s axios default', async () => {
    mockedGet.mockResolvedValueOnce({ data: { authenticated: false } } as never);

    await initAuth();

    expect(SESSION_PROBE_TIMEOUT_MS).toBeLessThanOrEqual(10_000);
    expect(mockedGet).toHaveBeenCalledWith(
      '/auth/session',
      expect.objectContaining({ timeout: SESSION_PROBE_TIMEOUT_MS })
    );
  });

  it('loads capabilities exactly once for a page load that has a session', async () => {
    mockedGet.mockResolvedValueOnce({ data: { authenticated: true, user: USER } } as never);

    await initAuth();

    expect(get(isAuthenticated)).toBe(true);
    expect(mockedCaps).toHaveBeenCalledTimes(1);
  });

  it('does not load capabilities for an anonymous visitor (nothing there uses them)', async () => {
    mockedGet.mockResolvedValueOnce({ data: { authenticated: false } } as never);

    await initAuth();

    expect(get(isAuthenticated)).toBe(false);
    expect(mockedCaps).not.toHaveBeenCalled();
  });

  it('silent refresh that fails ends anonymous instead of retrying forever', async () => {
    mockedGet.mockResolvedValueOnce({ data: { authenticated: false, refreshable: true } } as never);
    mockedPost.mockRejectedValueOnce(new Error('401'));

    await initAuth();

    expect(get(isAuthenticated)).toBe(false);
    expect(get(authStore).ready).toBe(true);
    expect(mockedGet).toHaveBeenCalledTimes(1);
  });
});

describe('initAuth — an unreachable backend', () => {
  it('shows a retry state and does NOT treat the failure as a logged-out visitor', async () => {
    vi.useFakeTimers();
    mockedGet.mockRejectedValueOnce(new Error('timeout of 8000ms exceeded'));
    mockedGet.mockResolvedValueOnce({ data: { authenticated: true, user: USER } } as never);

    const done = initAuth();
    await vi.advanceTimersByTimeAsync(0);

    // Failed once, retry pending: the app must not be "ready" as anonymous (that would redirect
    // a valid session to /login), and the UI is told the server is unreachable.
    expect(get(authStore).ready).toBe(false);
    expect(get(sessionProbe).unreachable).toBe(true);

    await vi.advanceTimersByTimeAsync(10_000);
    await done;

    expect(get(isAuthenticated)).toBe(true);
    expect(get(sessionProbe).unreachable).toBe(false);
  });

  it('"retry now" skips the backoff wait', async () => {
    vi.useFakeTimers();
    mockedGet.mockRejectedValueOnce(new Error('network'));
    mockedGet.mockResolvedValueOnce({ data: { authenticated: false } } as never);

    const done = initAuth();
    await vi.advanceTimersByTimeAsync(0);
    expect(get(sessionProbe).unreachable).toBe(true);

    retrySessionProbeNow();
    await vi.advanceTimersByTimeAsync(0); // no timer advance: the click alone must be enough
    await done;

    expect(mockedGet).toHaveBeenCalledTimes(2);
    expect(get(authStore).ready).toBe(true);
  });
});
