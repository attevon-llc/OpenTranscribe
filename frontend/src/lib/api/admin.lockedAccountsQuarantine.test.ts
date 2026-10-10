/**
 * `AdminApi` client coverage for issue #570 (locked-account management) and
 * issue #576 (quarantine/takedown). Includes the §B.2 guard: this file also
 * pins that `AdminApi` carries zero GDPR/erasure methods — the mechanical
 * reason no GDPR button can exist in the quarantine UI. If a later PR adds one
 * "while it's in there", this test fails.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => {
  const axiosInstance = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  };
  return { default: axiosInstance };
});

import axiosInstance from '$lib/axios';
import { AdminApi } from './admin';

const get = vi.mocked(axiosInstance.get);
const post = vi.mocked(axiosInstance.post);

describe('AdminApi locked accounts (issue #570)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('listLockedAccounts calls the exact path with params', async () => {
    get.mockResolvedValue({
      data: {
        accounts: [],
        next_cursor: null,
        lockout_enabled: true,
        store_backend: 'redis',
        truncated: false,
      },
    } as never);

    await AdminApi.listLockedAccounts({ cursor: 'abc', limit: 25, include_unlocked: true });

    expect(get).toHaveBeenCalledWith('/admin/locked-accounts', {
      params: { cursor: 'abc', limit: 25, include_unlocked: true },
    });
  });

  it('resetLockoutCounter posts to the path-encoded identifier', async () => {
    post.mockResolvedValue({
      data: {
        success: true,
        identifier: 'weird+user@example.com',
        previous_lockout_count: 3,
        was_locked: true,
        unlocked: false,
      },
    } as never);

    await AdminApi.resetLockoutCounter('weird+user@example.com');

    // encodeURIComponent — the identifier can contain characters (e.g. '+')
    // that are not safe unencoded in a path segment.
    expect(post).toHaveBeenCalledWith('/admin/locked-accounts/weird%2Buser%40example.com/reset');
  });
});

describe('AdminApi quarantine (issue #576)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('listQuarantinedFiles calls the exact path with params', async () => {
    get.mockResolvedValue({ data: { files: [], total: 0 } } as never);

    await AdminApi.listQuarantinedFiles({ limit: 50, offset: 0, include_legal_holds: true });

    expect(get).toHaveBeenCalledWith('/admin/files/quarantined', {
      params: { limit: 50, offset: 0, include_legal_holds: true },
    });
  });

  it('quarantineFile posts {reason, legal_hold} to the exact path', async () => {
    post.mockResolvedValue({
      data: { uuid: 'f-1', is_quarantined: true, legal_hold: true, status: 'quarantined' },
    } as never);

    await AdminApi.quarantineFile('f-1', 'DMCA notice #123', true);

    expect(post).toHaveBeenCalledWith('/admin/files/f-1/quarantine', {
      reason: 'DMCA notice #123',
      legal_hold: true,
    });
  });

  it('releaseFile posts {clear_legal_hold} to the exact path', async () => {
    post.mockResolvedValue({
      data: { uuid: 'f-1', is_quarantined: false, legal_hold: false, status: 'completed' },
    } as never);

    await AdminApi.releaseFile('f-1', true);

    expect(post).toHaveBeenCalledWith('/admin/files/f-1/release', {
      clear_legal_hold: true,
    });
  });
});

describe('§B.2 guard — AdminApi has no GDPR/erasure surface', () => {
  it('no AdminApi static method name matches /gdpr|erase/i', () => {
    // The repo's compliance rule (backend/app/api/CLAUDE.md): a legally-
    // significant, irreversible action stays curl-only unless it satisfies all
    // three of reversible / dual-audited / notified. Quarantine does;
    // erasure explicitly does not. Keeping AdminApi at zero GDPR methods is
    // the mechanical guarantee that this UI cannot reach one "while adding a
    // quarantine panel" — see #576 §B.2's corollary.
    const methodNames = Object.getOwnPropertyNames(AdminApi).filter(
      (name) => typeof (AdminApi as unknown as Record<string, unknown>)[name] === 'function'
    );

    const offenders = methodNames.filter((name) => /gdpr|erase/i.test(name));

    expect(offenders).toEqual([]);
  });
});
