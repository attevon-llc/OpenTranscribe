/**
 * Wiring of the idle guard to the session (issue #1106): lock → upload deferral →
 * sign-out, the absolute-timeout re-authentication hook, first-seen fallback, and
 * the server-refusal path. The guard's own timing rules live in idleGuard.test.ts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get, writable } from 'svelte/store';

const cloud = vi.hoisted(() => ({
  externalReauthenticate: vi.fn(async (_reason: string) => false),
  getExternalSessionAuthTime: vi.fn(async () => null as number | null),
}));
const auth = vi.hoisted(() => ({
  logout: vi.fn(async (_reason?: string | null) => {}),
  getAuthMethods: vi.fn(async () => ({
    session_idle_timeout_minutes: 15,
    session_absolute_timeout_minutes: 480,
  })),
}));

vi.mock('$lib/cloud', () => cloud);
vi.mock('$stores/auth', () => auth);

import { gotoCalls } from '../../test-mocks/app-navigation';
import {
  FIRST_SEEN_KEY,
  UPLOAD_GRACE_MS,
  endExpiredSession,
  expireSessionNow,
  loadSessionTimeoutConfig,
  resolveSessionStartMs,
  sessionLock,
  startSessionTimeouts,
  staySignedIn,
  stopSessionTimeouts,
} from './sessionTimeouts';

const MIN = 60_000;

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) {
    await vi.dynamicImportSettled();
    await Promise.resolve();
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-30T12:00:00Z'));
  localStorage.clear();
  gotoCalls.length = 0;
  cloud.externalReauthenticate.mockReset().mockResolvedValue(false);
  cloud.getExternalSessionAuthTime.mockReset().mockResolvedValue(null);
  auth.logout.mockClear();
});

afterEach(() => {
  stopSessionTimeouts();
  vi.useRealTimers();
});

describe('idle expiry', () => {
  it('warns, then locks and signs out with the idle reason', async () => {
    const endSession = vi.fn(async () => {});
    startSessionTimeouts(
      { idleMinutes: 15, absoluteMinutes: 0, authTimeSeconds: null },
      { hasActiveUploads: writable(false), endSession }
    );

    vi.advanceTimersByTime(13 * MIN + 1000);
    expect(get(sessionLock)).toMatchObject({ phase: 'warning', reason: 'idle' });

    vi.advanceTimersByTime(2 * MIN);
    expect(get(sessionLock)).toEqual({ phase: 'locked', reason: 'idle', waitingForUploads: false });
    expect(endSession).toHaveBeenCalledWith('idle');
  });

  it('"stay signed in" returns to active', () => {
    startSessionTimeouts(
      { idleMinutes: 15, absoluteMinutes: 0, authTimeSeconds: null },
      { hasActiveUploads: writable(false), endSession: vi.fn(async () => {}) }
    );
    vi.advanceTimersByTime(14 * MIN);
    staySignedIn();
    expect(get(sessionLock)).toEqual({ phase: 'active' });
  });

  it('does nothing when both limits are off', () => {
    const started = startSessionTimeouts(
      { idleMinutes: 0, absoluteMinutes: 0, authTimeSeconds: null },
      { hasActiveUploads: writable(false), endSession: vi.fn(async () => {}) }
    );
    vi.advanceTimersByTime(24 * 60 * MIN);
    expect(started).toBe(false);
    expect(get(sessionLock)).toEqual({ phase: 'active' });
  });
});

describe('uploads keep the page alive, but the UI still locks', () => {
  it('locks immediately and signs out only when uploads finish', () => {
    const uploads = writable(true);
    const endSession = vi.fn(async () => {});
    startSessionTimeouts(
      { idleMinutes: 15, absoluteMinutes: 0, authTimeSeconds: null },
      { hasActiveUploads: uploads, endSession }
    );

    vi.advanceTimersByTime(16 * MIN);
    expect(get(sessionLock)).toEqual({ phase: 'locked', reason: 'idle', waitingForUploads: true });
    expect(endSession).not.toHaveBeenCalled();

    uploads.set(false);
    expect(endSession).toHaveBeenCalledTimes(1);
  });

  it('input while locked does not unlock', () => {
    startSessionTimeouts(
      { idleMinutes: 15, absoluteMinutes: 0, authTimeSeconds: null },
      { hasActiveUploads: writable(true), endSession: vi.fn(async () => {}) }
    );
    vi.advanceTimersByTime(16 * MIN);
    window.dispatchEvent(new Event('keydown'));
    staySignedIn();
    expect(get(sessionLock)).toMatchObject({ phase: 'locked' });
  });

  it('a stuck upload cannot hold the session open forever', () => {
    const endSession = vi.fn(async () => {});
    startSessionTimeouts(
      { idleMinutes: 15, absoluteMinutes: 0, authTimeSeconds: null },
      { hasActiveUploads: writable(true), endSession }
    );
    vi.advanceTimersByTime(16 * MIN);
    vi.advanceTimersByTime(UPLOAD_GRACE_MS);
    expect(endSession).toHaveBeenCalledTimes(1);
  });
});

describe('absolute timeout', () => {
  it('counts from the provider-reported auth_time', () => {
    const endSession = vi.fn(async () => {});
    const authTimeSeconds = Math.floor(Date.now() / 1000) - 470 * 60;
    startSessionTimeouts(
      { idleMinutes: 0, absoluteMinutes: 480, authTimeSeconds },
      { hasActiveUploads: writable(false), endSession }
    );
    expect(get(sessionLock)).toEqual({ phase: 'active' });
    vi.advanceTimersByTime(10 * MIN + 1000);
    expect(endSession).toHaveBeenCalledWith('absolute');
  });

  it('falls back to first-seen, persisted across reloads', () => {
    const t0 = Date.now();
    expect(resolveSessionStartMs(null)).toBe(t0);
    vi.setSystemTime(t0 + 30 * MIN);
    expect(resolveSessionStartMs(null)).toBe(t0);
    expect(localStorage.getItem(FIRST_SEEN_KEY)).toBe(String(t0));
  });

  it('prefers the adapter auth_time over first-seen', () => {
    localStorage.setItem(FIRST_SEEN_KEY, String(Date.now()));
    expect(resolveSessionStartMs(1_700_000_000)).toBe(1_700_000_000_000);
  });
});

describe('ending the session', () => {
  it('idle: signs out with the reason and goes to /login', async () => {
    localStorage.setItem(FIRST_SEEN_KEY, '123');
    await endExpiredSession('idle');
    expect(auth.logout).toHaveBeenCalledWith('idle_timeout');
    expect(cloud.externalReauthenticate).not.toHaveBeenCalled();
    expect(gotoCalls).toEqual(['/login']);
    expect(localStorage.getItem(FIRST_SEEN_KEY)).toBeNull();
  });

  it('absolute: an adapter that takes over re-authentication skips the sign-out', async () => {
    cloud.externalReauthenticate.mockResolvedValue(true);
    await endExpiredSession('absolute');
    expect(auth.logout).not.toHaveBeenCalled();
    expect(gotoCalls).toEqual([]);
  });

  it('absolute: an adapter that declines falls back to sign-out', async () => {
    await endExpiredSession('absolute');
    expect(auth.logout).toHaveBeenCalledWith('absolute_timeout');
    expect(gotoCalls).toEqual(['/login']);
  });

  it('absolute: an adapter that throws falls back to sign-out', async () => {
    cloud.externalReauthenticate.mockRejectedValue(new Error('provider down'));
    await endExpiredSession('absolute');
    expect(auth.logout).toHaveBeenCalledWith('absolute_timeout');
  });

  it('concurrent refusals end the session once', async () => {
    await Promise.all([endExpiredSession('idle'), endExpiredSession('idle')]);
    expect(auth.logout).toHaveBeenCalledTimes(1);
  });

  it('a server refusal with no guard running ends the session directly', async () => {
    expireSessionNow('absolute');
    await flush();
    expect(auth.logout).toHaveBeenCalledWith('absolute_timeout');
  });

  it('a server refusal with a guard running locks first', () => {
    const endSession = vi.fn(async () => {});
    startSessionTimeouts(
      { idleMinutes: 15, absoluteMinutes: 480, authTimeSeconds: null },
      { hasActiveUploads: writable(true), endSession }
    );
    expireSessionNow('absolute');
    expect(get(sessionLock)).toEqual({
      phase: 'locked',
      reason: 'absolute',
      waitingForUploads: true,
    });
  });
});

describe('loading the config', () => {
  it('reads limits from /auth/methods and auth_time from the adapter', async () => {
    cloud.getExternalSessionAuthTime.mockResolvedValue(1_700_000_000);
    await expect(loadSessionTimeoutConfig()).resolves.toEqual({
      idleMinutes: 15,
      absoluteMinutes: 480,
      authTimeSeconds: 1_700_000_000,
    });
  });

  it('an older backend without the fields leaves both limits off', async () => {
    auth.getAuthMethods.mockResolvedValueOnce({} as never);
    await expect(loadSessionTimeoutConfig()).resolves.toMatchObject({
      idleMinutes: 0,
      absoluteMinutes: 0,
    });
  });
});
