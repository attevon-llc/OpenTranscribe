import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

const h = vi.hoisted(() => ({
  getGrant: vi.fn(),
  listMyGrants: vi.fn(),
  goto: vi.fn(),
  purge: vi.fn(),
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
  toastInfo: vi.fn(),
}));

vi.mock('$lib/api/supportAccess', () => ({
  SupportAccessApi: { getGrant: h.getGrant, listMyGrants: h.listMyGrants },
}));
vi.mock('$app/navigation', () => ({ goto: h.goto }));
vi.mock('$lib/session/clearUserState', () => ({ purgeTenantDataCaches: h.purge }));
vi.mock('$stores/toast', () => ({
  toastStore: { error: h.toastError, success: h.toastSuccess, info: h.toastInfo },
}));
vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));

import { capabilities } from '$stores/capabilities';
import { getSupportAccessHeaders } from '$lib/supportAccess/headers';
import { supportSession, supportSessionGate } from './supportSession';

const UUID = '11111111-2222-3333-4444-555555555555';
const STORAGE_KEY = 'opentr:supportSession';

function grant(over: Record<string, unknown> = {}) {
  const now = Date.now();
  return {
    uuid: UUID,
    status: 'active',
    target_kind: 'organization',
    grant_mode: 'approved',
    access_level: 'read',
    organization: { uuid: 'o1', name: 'Acme', slug: 'acme' },
    subject_user: null,
    expires_at: new Date(now + 3600_000).toISOString(),
    ...over,
  };
}

function setMode(mode: 'multi' | 'single' | undefined) {
  capabilities.update((s) => ({ ...s, tenancyMode: mode }));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-09T12:00:00Z'));
  vi.clearAllMocks();
  sessionStorage.clear();
  setMode('multi');
});

afterEach(async () => {
  await supportSession.end('logout');
  vi.useRealTimers();
});

/**
 * `GET /support-access/grants/{uuid}` carries no `server_time` (only list pages do), so the
 * store reads the skew off a one-row list page fetched alongside it.
 */
function grantWithServerTime(g: Record<string, unknown>, serverOffsetMs: number) {
  h.getGrant.mockResolvedValue(g);
  h.listMyGrants.mockResolvedValue({
    items: [],
    total: 0,
    server_time: new Date(Date.now() + serverOffsetMs).toISOString(),
  });
}

describe('supportSession.activate', () => {
  it('refuses outside multi-tenant mode without calling the API', async () => {
    setMode('single');
    const ok = await supportSession.activate(UUID);
    expect(ok).toBe(false);
    expect(h.getGrant).not.toHaveBeenCalled();
    expect(get(supportSession).active).toBe(false);
    expect(getSupportAccessHeaders('/files/x')).toEqual({});
  });

  it('refuses when the mode is not yet known (fail closed)', async () => {
    setMode(undefined);
    expect(await supportSession.activate(UUID)).toBe(false);
    expect(h.getGrant).not.toHaveBeenCalled();
  });

  it('refuses a grant that is not active and says so', async () => {
    grantWithServerTime(grant({ status: 'pending' }), 0);
    expect(await supportSession.activate(UUID)).toBe(false);
    expect(h.toastError).toHaveBeenCalledWith('supportAccess.session.activateFailed');
    expect(get(supportSession).active).toBe(false);
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('starts the session: header, tab storage, cache purge, redirect', async () => {
    grantWithServerTime(grant(), 0);
    expect(await supportSession.activate(UUID)).toBe(true);

    const s = get(supportSession);
    expect(s.active).toBe(true);
    expect(s.grantUuid).toBe(UUID);
    expect(s.targetLabel).toBe('Acme');
    expect(s.level).toBe('read');
    expect(getSupportAccessHeaders('/files/x')).toEqual({ 'X-Support-Access-Grant': UUID });
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEY) as string)).toEqual({ grantUuid: UUID });
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(h.purge).toHaveBeenCalledTimes(1);
    expect(h.goto).toHaveBeenCalledWith('/');
  });

  it('labels a personal grant with the subject user', async () => {
    grantWithServerTime(
      grant({
        target_kind: 'personal',
        organization: null,
        subject_user: { uuid: 'u1', full_name: 'Jane Doe', email: 'jane@example.com' },
      }),
      0
    );
    await supportSession.activate(UUID);
    expect(get(supportSession).targetLabel).toBe('Jane Doe');
    expect(get(supportSession).targetKind).toBe('personal');
  });

  it('corrects the countdown for client clock skew (server 120 s ahead = 120 s shorter)', async () => {
    grantWithServerTime(grant(), 120_000);
    await supportSession.activate(UUID);
    expect(get(supportSession).remainingSeconds).toBe(3600 - 120);
  });

  it('counts down once a second and ends as expired at zero', async () => {
    grantWithServerTime(grant({ expires_at: new Date(Date.now() + 3000).toISOString() }), 0);
    await supportSession.activate(UUID);
    expect(get(supportSession).remainingSeconds).toBe(3);

    await vi.advanceTimersByTimeAsync(2000);
    expect(get(supportSession).remainingSeconds).toBe(1);
    expect(get(supportSession).active).toBe(true);

    await vi.advanceTimersByTimeAsync(1000);
    expect(get(supportSession).active).toBe(false);
    expect(h.toastError).toHaveBeenCalledWith('supportAccess.session.expired');
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(getSupportAccessHeaders('/files/x')).toEqual({});
    expect(h.purge).toHaveBeenCalledTimes(2);
  });
});

describe('supportSession.end', () => {
  it('is idempotent: a second call raises no second toast or redirect', async () => {
    grantWithServerTime(grant(), 0);
    await supportSession.activate(UUID);
    h.goto.mockClear();

    await supportSession.end('revoked');
    await supportSession.end('revoked');

    expect(h.toastError).toHaveBeenCalledTimes(1);
    expect(h.toastError).toHaveBeenCalledWith('supportAccess.session.revoked');
    expect(h.goto).toHaveBeenCalledTimes(1);
  });

  it('a user-initiated end is an info toast, not an error', async () => {
    grantWithServerTime(grant(), 0);
    await supportSession.activate(UUID);
    await supportSession.end('user');
    expect(h.toastError).not.toHaveBeenCalled();
    expect(h.toastInfo).toHaveBeenCalledWith('supportAccess.session.user');
  });

  it('logout clears everything silently and does not navigate', async () => {
    grantWithServerTime(grant(), 0);
    await supportSession.activate(UUID);
    h.goto.mockClear();
    h.toastError.mockClear();
    h.toastInfo.mockClear();

    await supportSession.end('logout');

    expect(get(supportSession).active).toBe(false);
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(h.goto).not.toHaveBeenCalled();
    expect(h.toastError).not.toHaveBeenCalled();
    expect(h.toastInfo).not.toHaveBeenCalled();
  });

  it('stops the countdown: no tick fires after the session ended', async () => {
    grantWithServerTime(grant({ expires_at: new Date(Date.now() + 5000).toISOString() }), 0);
    await supportSession.activate(UUID);
    await supportSession.end('user');
    h.toastError.mockClear();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.toastError).not.toHaveBeenCalled();
  });
});

describe('ordering around the store flip (the layout remounts the page on it)', () => {
  it('start: the header is already live and the caches already purged when the store goes active', async () => {
    grantWithServerTime(grant(), 0);
    const seen: { active: boolean; header: boolean; purges: number }[] = [];
    const unsubscribe = supportSession.subscribe((s) => {
      seen.push({
        active: s.active,
        header: 'X-Support-Access-Grant' in getSupportAccessHeaders('/files/x'),
        purges: h.purge.mock.calls.length,
      });
    });
    await supportSession.activate(UUID);
    unsubscribe();
    const activeSnapshot = seen.find((x) => x.active);
    expect(activeSnapshot).toEqual({ active: true, header: true, purges: 1 });
  });

  it('end: the header is already gone and the caches already purged when the store goes inactive', async () => {
    grantWithServerTime(grant(), 0);
    await supportSession.activate(UUID);
    h.purge.mockClear();
    const seen: { active: boolean; header: boolean; purges: number }[] = [];
    const unsubscribe = supportSession.subscribe((s) => {
      seen.push({
        active: s.active,
        header: 'X-Support-Access-Grant' in getSupportAccessHeaders('/files/x'),
        purges: h.purge.mock.calls.length,
      });
    });
    await supportSession.end('user');
    unsubscribe();
    expect(seen.at(-1)).toEqual({ active: false, header: false, purges: 1 });
  });

  it('two overlapping end() calls still raise one toast', async () => {
    grantWithServerTime(grant(), 0);
    await supportSession.activate(UUID);
    await Promise.all([supportSession.end('revoked'), supportSession.end('revoked')]);
    expect(h.toastError).toHaveBeenCalledTimes(1);
  });
});

describe('supportSession.restore', () => {
  it('re-activates a still-active grant from tab storage without redirecting', async () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ grantUuid: UUID }));
    grantWithServerTime(grant(), 0);
    await supportSession.restore();
    expect(get(supportSession).active).toBe(true);
    expect(getSupportAccessHeaders('/files/x')).toEqual({ 'X-Support-Access-Grant': UUID });
    expect(h.goto).not.toHaveBeenCalled();
  });

  it('clears silently when the grant was revoked meanwhile', async () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ grantUuid: UUID }));
    grantWithServerTime(grant({ status: 'revoked' }), 0);
    await supportSession.restore();
    expect(get(supportSession).active).toBe(false);
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(h.toastError).not.toHaveBeenCalled();
    expect(h.toastInfo).not.toHaveBeenCalled();
  });

  it('clears silently when the lookup fails', async () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ grantUuid: UUID }));
    h.getGrant.mockRejectedValue(new Error('404'));
    await supportSession.restore();
    expect(get(supportSession).active).toBe(false);
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('clears silently outside multi-tenant mode and never calls the API', async () => {
    setMode('single');
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ grantUuid: UUID }));
    await supportSession.restore();
    expect(h.getGrant).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('does nothing when there is no stored session', async () => {
    await supportSession.restore();
    expect(h.getGrant).not.toHaveBeenCalled();
  });
});

describe('supportSessionGate', () => {
  it('is inactive and not read-only with no session', () => {
    expect(get(supportSessionGate)).toEqual({ active: false, readOnly: false });
  });

  it('is active and read-only for a read grant', async () => {
    grantWithServerTime(grant({ access_level: 'read' }), 0);
    await supportSession.activate(UUID);
    expect(get(supportSessionGate)).toEqual({ active: true, readOnly: true });
  });

  it('is active but writable for a write grant', async () => {
    grantWithServerTime(grant({ access_level: 'write' }), 0);
    await supportSession.activate(UUID);
    expect(get(supportSessionGate)).toEqual({ active: true, readOnly: false });
  });
});
