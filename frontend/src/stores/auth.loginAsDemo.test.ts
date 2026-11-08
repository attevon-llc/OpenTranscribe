/**
 * "Try the demo" signs in with no password through `POST /auth/demo/session` and then proves
 * the session cookie stuck with `/auth/me`, exactly like the PKI path: a 200 alone is not a
 * session (a dropped `Secure` cookie must read as a failure, not a redirect loop).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
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
import { loginAsDemo, authStore, isAuthenticated } from './auth';

describe('loginAsDemo', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authStore.reset();
  });

  it('posts to the passwordless endpoint, sending no credentials, and signs in', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({
      status: 200,
      data: { access_token: 'demo-token' },
    } as never);
    vi.mocked(axiosInstance.get).mockResolvedValue({
      status: 200,
      data: { id: 1, email: 'demo@example.com' },
    } as never);

    const result = await loginAsDemo();

    expect(result).toEqual({ success: true });
    expect(axiosInstance.post).toHaveBeenCalledTimes(1);
    expect(vi.mocked(axiosInstance.post).mock.calls[0]).toEqual(['/auth/demo/session']);
    expect(get(isAuthenticated)).toBe(true);
  });

  it('fails when the cookie never came back', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({
      status: 200,
      data: { access_token: 'demo-token' },
    } as never);
    vi.mocked(axiosInstance.get).mockRejectedValue({ response: { status: 401 } });

    const result = await loginAsDemo();

    expect(result.success).toBe(false);
    expect(result.message).toBe('auth.error.sessionCookieRejected');
    expect(get(isAuthenticated)).toBe(false);
  });

  it('reports the demo as unavailable when the endpoint refuses', async () => {
    vi.mocked(axiosInstance.post).mockRejectedValue({ response: { status: 503 } });

    const result = await loginAsDemo();

    expect(result).toEqual({ success: false, message: 'demo.signInFailed' });
    expect(get(isAuthenticated)).toBe(false);
  });

  it('tells a rate-limited visitor to wait rather than that the demo is down', async () => {
    vi.mocked(axiosInstance.post).mockRejectedValue({ response: { status: 429 } });

    const result = await loginAsDemo();

    expect(result).toEqual({ success: false, message: 'auth.error.tooManyLoginAttempts' });
  });
});
