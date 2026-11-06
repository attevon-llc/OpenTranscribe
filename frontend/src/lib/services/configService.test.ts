/**
 * `configService` caches `/system/config/protected-media-auth` in a module-level
 * singleton. `resetProtectedMediaAuthConfig`'s own docstring explains why it must
 * exist: `hosts_with_stored_credentials` is per-user, and without a reset on
 * logout, User B would see which protected hosts User A had saved credentials
 * for until a hard reload — these tests pin exactly that boundary.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'fs';
import { resolve } from 'path';

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

  it('asks the protected-media endpoint and resolves to nothing', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: CONFIG });

    await expect(loadProtectedMediaAuthConfig()).resolves.toBeUndefined();

    expect(mockAxiosInstance.get).toHaveBeenCalledWith('/system/config/protected-media-auth');
  });

  it('treats a missing body as no configs, without refetching', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: null });

    await loadProtectedMediaAuthConfig();
    await loadProtectedMediaAuthConfig();

    expect(getAuthConfigForUrl(new URL('https://media.example.com'))).toBeNull();
    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);
  });

  it('logs a failed load', async () => {
    const error = new Error('network down');
    mockAxiosInstance.get.mockRejectedValue(error);
    const log = vi.spyOn(console, 'error').mockImplementation(() => {});

    await loadProtectedMediaAuthConfig();

    expect(log).toHaveBeenCalledWith(expect.any(String), error);
    log.mockRestore();
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
    // A host stored without a scheme is https only.
    expect(getAuthConfigForUrl(new URL('http://plain.example.com/view?m=1'))).toBeNull();
    expect(getAuthConfigForUrl(new URL('https://plain.example.com:8443/'))).toBeNull();
  });

  it('matches a source configured for plain http only over http', async () => {
    mockAxiosInstance.get.mockResolvedValue({
      data: [
        { hosts: ['http://intranet.example.com:8080'], auth_type: 'user_password', fields: [] },
      ],
    });
    await loadProtectedMediaAuthConfig();

    expect(
      getAuthConfigForUrl(new URL('http://intranet.example.com:8080/view?m=1'))
    ).not.toBeNull();
    expect(getAuthConfigForUrl(new URL('https://intranet.example.com:8080/view?m=1'))).toBeNull();
    expect(getAuthConfigForUrl(new URL('http://intranet.example.com/view?m=1'))).toBeNull();
  });

  it('trims and case-folds a configured host, and refuses a space or percent-escape in it', async () => {
    mockAxiosInstance.get.mockResolvedValue({
      data: [
        { hosts: ['  Media.Example.COM  '], auth_type: 'padded', fields: [] },
        { hosts: ['media example.net', 'media%2eexample.net'], auth_type: 'refused', fields: [] },
      ],
    });
    await loadProtectedMediaAuthConfig();

    expect(getAuthConfigForUrl(new URL('https://media.example.com/view?m=1'))).toMatchObject({
      auth_type: 'padded',
    });
    expect(getAuthConfigForUrl(new URL('https://media.example.net/view?m=1'))).toBeNull();
  });

  it('returns the first config listing the host and tolerates one without hosts', async () => {
    mockAxiosInstance.get.mockResolvedValue({
      data: [
        { auth_type: 'no-hosts', fields: [] },
        { hosts: ['media.example.com'], auth_type: 'first', fields: [] },
        { hosts: ['media.example.com'], auth_type: 'second', fields: [] },
      ],
    });
    await loadProtectedMediaAuthConfig();

    expect(getAuthConfigForUrl(new URL('https://media.example.com/'))?.auth_type).toBe('first');
  });

  it('matches on the host alone, whatever the path', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: CONFIG });
    await loadProtectedMediaAuthConfig();

    expect(getAuthConfigForUrl(new URL('https://media.example.com/'))?.auth_type).toBe('basic');
    expect(getAuthConfigForUrl(new URL('https://user@media.example.com/view?m=1'))?.auth_type).toBe(
      'basic'
    );
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

describe('host matching is never looser than the backend', () => {
  // Shared with backend/tests/unit/test_mediacms_host_matching.py: one list, two implementations.
  const vectors = JSON.parse(
    readFileSync(
      resolve(process.cwd(), '../backend/tests/fixtures/mediacms_host_vectors.json'),
      'utf-8'
    )
  ).vectors as { configured: string; candidate: string; scheme: string; expected: boolean }[];

  it('reads a non-empty vector list', () => {
    expect(vectors.length).toBeGreaterThan(10);
  });

  it.each(vectors.map((v) => [v.configured, v.candidate, v.scheme, v.expected] as const))(
    'configured %j vs %j (%s) -> %s',
    async (configured, candidate, scheme, expected) => {
      mockAxiosInstance.get.mockResolvedValue({
        data: [{ hosts: [configured], auth_type: 'basic', fields: [] }],
      });
      await loadProtectedMediaAuthConfig();
      expect(getAuthConfigForUrl(new URL(`${scheme}://${candidate}`)) !== null).toBe(expected);
    }
  );
});
