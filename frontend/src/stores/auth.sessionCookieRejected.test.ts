/**
 * A LAN-only, plain-HTTP deployment (a homelab or small-business install with no
 * TLS-terminating reverse proxy) sets `Secure` on every auth cookie by default —
 * correct behind real TLS, but the browser silently drops a `Secure` cookie sent
 * over plain HTTP to anything other than localhost/127.0.0.1. Before this fix,
 * `POST /auth/login` still answered 200 with a valid `access_token`, so `login()`
 * reported success and redirected into the app even though no cookie had actually
 * been stored — the user then bounced straight back out, which read exactly like
 * a wrong password with no indication of what actually happened.
 *
 * `login()` now treats a failed `/auth/me` follow-up (no cookie came back) as a
 * distinct failure with its own message, rather than reporting success on the
 * strength of the login response alone.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

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
import { login, authStore, isAuthenticated } from './auth';
import { get } from 'svelte/store';

describe('login — the session cookie never came back', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authStore.reset();
  });

  it('reports failure, not success, when /auth/me 401s right after a 200 login', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({
      status: 200,
      data: { access_token: 'server-issued-this-so-the-password-was-correct' },
    } as never);
    vi.mocked(axiosInstance.get).mockRejectedValue({ response: { status: 401 } });

    const result = await login('admin@example.com', 'correct-horse-battery-staple');

    expect(result.success).toBe(false);
    expect(result.message).toBe('auth.error.sessionCookieRejected');
    expect(get(isAuthenticated)).toBe(false);
  });

  it('still reports success on the ordinary path, where /auth/me confirms the cookie stuck', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({
      status: 200,
      data: { access_token: 'server-issued' },
    } as never);
    vi.mocked(axiosInstance.get).mockResolvedValue({
      data: { uuid: 'u1', must_change_password: false },
    } as never);

    const result = await login('admin@example.com', 'correct-horse-battery-staple');

    expect(result.success).toBe(true);
    expect(get(isAuthenticated)).toBe(true);
  });
});
