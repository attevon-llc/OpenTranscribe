/**
 * `configService` caches `/system/config/protected-media-auth` in a module-level
 * singleton. `resetProtectedMediaAuthConfig`'s own docstring explains why it must
 * exist: `hosts_with_stored_credentials` is per-user, and without a reset on
 * logout, User B would see which protected hosts User A had saved credentials
 * for until a hard reload — these tests pin exactly that boundary.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxiosInstance = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('../axios', () => ({ default: mockAxiosInstance }));

import {
  loadProtectedMediaAuthConfig,
  resetProtectedMediaAuthConfig,
  getAuthConfigForHost,
} from './configService';

const CONFIG = [
  { hosts: ['media.example.com'], auth_type: 'basic', fields: [] },
  { hosts: ['nas.internal', 'nas2.internal'], auth_type: 'digest', fields: [] },
];

beforeEach(() => {
  vi.clearAllMocks();
  resetProtectedMediaAuthConfig();
});

describe('loadProtectedMediaAuthConfig', () => {
  it('fetches once and caches — a second call does not refetch', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: CONFIG });

    await loadProtectedMediaAuthConfig();
    await loadProtectedMediaAuthConfig();

    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);
    expect(getAuthConfigForHost('media.example.com')).toMatchObject({ auth_type: 'basic' });
  });

  it('deduplicates concurrent calls into a single in-flight request', async () => {
    let resolveGet!: (v: { data: typeof CONFIG }) => void;
    mockAxiosInstance.get.mockReturnValue(new Promise((r) => (resolveGet = r)));

    const first = loadProtectedMediaAuthConfig();
    const second = loadProtectedMediaAuthConfig();
    resolveGet({ data: CONFIG });
    await first;
    await second;

    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);
    // Both concurrent callers must observe the SAME resolved config, not a stale/empty one.
    expect(getAuthConfigForHost('media.example.com')).toMatchObject({ auth_type: 'basic' });
  });

  it('swallows a fetch failure, leaves configs empty, and still marks itself loaded (does not retry-loop)', async () => {
    mockAxiosInstance.get.mockRejectedValue(new Error('network down'));

    await loadProtectedMediaAuthConfig();
    expect(getAuthConfigForHost('media.example.com')).toBeNull();

    await loadProtectedMediaAuthConfig(); // must not refetch despite the earlier failure
    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);
  });
});

describe('getAuthConfigForHost', () => {
  it('returns the config whose hosts array contains the hostname', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: CONFIG });
    await loadProtectedMediaAuthConfig();

    expect(getAuthConfigForHost('nas2.internal')).toMatchObject({ auth_type: 'digest' });
  });

  it('returns null for a host with no matching config', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: CONFIG });
    await loadProtectedMediaAuthConfig();

    expect(getAuthConfigForHost('unknown.example.com')).toBeNull();
  });

  it('returns null before anything has been loaded', () => {
    expect(getAuthConfigForHost('media.example.com')).toBeNull();
  });
});

describe('resetProtectedMediaAuthConfig', () => {
  it('drops the cache so the next call re-fetches — the multi-user credential boundary', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: CONFIG });
    await loadProtectedMediaAuthConfig();
    expect(getAuthConfigForHost('media.example.com')).not.toBeNull();

    resetProtectedMediaAuthConfig();

    // User A's config must not leak to whoever loads next, before the refetch resolves.
    expect(getAuthConfigForHost('media.example.com')).toBeNull();

    mockAxiosInstance.get.mockResolvedValue({ data: [] }); // User B has no protected hosts configured
    await loadProtectedMediaAuthConfig();

    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);
    expect(getAuthConfigForHost('media.example.com')).toBeNull();
  });
});
