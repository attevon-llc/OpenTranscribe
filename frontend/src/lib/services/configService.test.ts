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
  getAuthConfigForUrl,
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
    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toMatchObject({
      auth_type: 'basic',
    });
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
    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toMatchObject({
      auth_type: 'basic',
    });
  });

  it('swallows a fetch failure, leaves configs empty, and still marks itself loaded (does not retry-loop)', async () => {
    mockAxiosInstance.get.mockRejectedValue(new Error('network down'));

    await loadProtectedMediaAuthConfig();
    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toBeNull();

    await loadProtectedMediaAuthConfig(); // must not refetch despite the earlier failure
    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);
  });
});

describe('getAuthConfigForUrl', () => {
  it('returns the config whose hosts array contains the hostname', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: CONFIG });
    await loadProtectedMediaAuthConfig();

    expect(getAuthConfigForUrl(new URL('https://nas2.internal'))).toMatchObject({
      auth_type: 'digest',
    });
  });

  it('returns null for a host with no matching config', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: CONFIG });
    await loadProtectedMediaAuthConfig();

    expect(getAuthConfigForUrl(new URL('https://unknown.example.com'))).toBeNull();
  });

  it('matches a source configured with a port, and the same host on the default port', async () => {
    mockAxiosInstance.get.mockResolvedValue({
      data: [
        {
          hosts: ['media.example.com:8443', 'Plain.Example.com'],
          auth_type: 'user_password',
          fields: [],
        },
      ],
    });
    await loadProtectedMediaAuthConfig();

    expect(getAuthConfigForUrl(new URL('https://media.example.com:8443/view?m=1'))).not.toBeNull();
    expect(getAuthConfigForUrl(new URL('https://media.example.com/view?m=1'))).toBeNull();
    expect(getAuthConfigForUrl(new URL('https://media.example.com:9000/view?m=1'))).toBeNull();
    expect(getAuthConfigForUrl(new URL('https://plain.example.com:443/view?m=1'))).not.toBeNull();
    expect(getAuthConfigForUrl(new URL('http://plain.example.com/view?m=1'))).not.toBeNull();
    expect(getAuthConfigForUrl(new URL('https://plain.example.com:8443/'))).toBeNull();
  });

  it('returns null before anything has been loaded', () => {
    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toBeNull();
  });
});

describe('resetProtectedMediaAuthConfig', () => {
  it('drops the cache so the next call re-fetches — the multi-user credential boundary', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: CONFIG });
    await loadProtectedMediaAuthConfig();
    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).not.toBeNull();

    resetProtectedMediaAuthConfig();

    // User A's config must not leak to whoever loads next, before the refetch resolves.
    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toBeNull();

    mockAxiosInstance.get.mockResolvedValue({ data: [] }); // User B has no protected hosts configured
    await loadProtectedMediaAuthConfig();

    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);
    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toBeNull();
  });

  it('discards a User-A fetch that resolves AFTER logout, instead of leaking it into User B (BC-4)', async () => {
    // The actual race: User A's fetch is still in flight when logout fires.
    let resolveUserAFetch!: (v: { data: typeof CONFIG }) => void;
    mockAxiosInstance.get.mockReturnValueOnce(new Promise((r) => (resolveUserAFetch = r)));

    const userAFetch = loadProtectedMediaAuthConfig();

    // Logout happens BEFORE the in-flight fetch resolves.
    resetProtectedMediaAuthConfig();
    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toBeNull();

    // The stale response now arrives — it must be discarded, not applied.
    resolveUserAFetch({ data: CONFIG });
    await userAFetch;

    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toBeNull();
    expect(getAuthConfigForUrl(new URL('https://nas.internal'))).toBeNull();

    // A fresh load for User B (post-reset) must still work normally.
    mockAxiosInstance.get.mockResolvedValueOnce({ data: [] });
    await loadProtectedMediaAuthConfig();
    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toBeNull();
  });
});
