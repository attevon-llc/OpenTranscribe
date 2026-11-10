/**
 * Pins the `/auth/methods` sharing and fail-closed rules. A remembered failure would
 * keep the fail-closed answer (no SSO buttons, no signup link) for the whole page life
 * after one backend hiccup; a fail-closed answer with `local_enabled: false` would lock
 * every operator out of the sign-in form.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { failClosedAuthMethods, sharedFreshRequest } from './authMethods';

afterEach(() => {
  vi.useRealTimers();
});

describe('failClosedAuthMethods', () => {
  it('keeps the local form reachable and offers nothing else', () => {
    expect(failClosedAuthMethods()).toEqual({
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
    });
  });

  it('returns a fresh object each time', () => {
    expect(failClosedAuthMethods()).not.toBe(failClosedAuthMethods());
  });
});

describe('sharedFreshRequest', () => {
  it('shares one request while fresh, and asks again once stale', async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockResolvedValue({ value: 'a', ok: true });
    const shared = sharedFreshRequest(load, 1000);
    const first = shared.get();
    expect(shared.get()).toBe(first);
    await expect(first).resolves.toBe('a');
    vi.advanceTimersByTime(999);
    await shared.get();
    expect(load).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    await shared.get();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('never remembers a failed answer', async () => {
    const load = vi
      .fn()
      .mockResolvedValueOnce({ value: 'closed', ok: false })
      .mockResolvedValueOnce({ value: 'open', ok: true });
    const shared = sharedFreshRequest(load, 60_000);
    await expect(shared.get()).resolves.toBe('closed');
    await Promise.resolve();
    await expect(shared.get()).resolves.toBe('open');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('invalidate forces the next call to ask again', async () => {
    const load = vi.fn().mockResolvedValue({ value: 1, ok: true });
    const shared = sharedFreshRequest(load, 60_000);
    await shared.get();
    shared.invalidate();
    await shared.get();
    expect(load).toHaveBeenCalledTimes(2);
  });
});
