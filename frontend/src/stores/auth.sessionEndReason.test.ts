/**
 * `logout(reason)` records why the app ended the session (issue #1106) so the login
 * page can explain it. Only the two timeout reasons count: `logout` is also wired
 * straight to `on:click`, and a DOM event must read as a voluntary sign-out. A new
 * session clears the reason. Also pins that the first-seen key of an external
 * session does not survive a sign-out (the next session would inherit it and hit
 * the absolute timeout at once).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/axios', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ status: 200, data: {} }),
    post: vi.fn().mockResolvedValue({ status: 200, data: {} }),
    put: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  },
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
vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

import { authStore, logout, sessionEndReason } from './auth';

describe('sessionEndReason', () => {
  beforeEach(() => {
    sessionEndReason.set(null);
    authStore.setToken('cookie');
    localStorage.clear();
  });

  it('records a timeout reason', async () => {
    await logout('idle_timeout');
    expect(get(sessionEndReason)).toBe('idle_timeout');
  });

  it('a voluntary sign-out records none', async () => {
    await logout();
    expect(get(sessionEndReason)).toBeNull();
  });

  it('a DOM event passed as the argument is not a reason', async () => {
    await logout(new MouseEvent('click') as never);
    expect(get(sessionEndReason)).toBeNull();
  });

  it('is cleared when a new session starts', async () => {
    await logout('absolute_timeout');
    expect(get(sessionEndReason)).toBe('absolute_timeout');
    authStore.setToken('cookie');
    expect(get(sessionEndReason)).toBeNull();
  });

  it('sign-out removes the external-session first-seen key', async () => {
    localStorage.setItem('opentr:externalSessionFirstSeen', String(Date.now()));
    await logout();
    expect(localStorage.getItem('opentr:externalSessionFirstSeen')).toBeNull();
  });
});
