import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * Transport-only client: these assert the wire shape each method produces
 * (path, method, params/body) plus the response-value mapping. `getAuditLog`
 * gets dedicated boundary coverage for its client-side limit/offset clamp —
 * the endpoint answers an out-of-range `limit` with a 422 rather than
 * truncating, so the clamp is the only thing standing between a caller and a
 * failed request.
 */
const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('$lib/axios', async () => {
  const actual = await vi.importActual<typeof import('$lib/axios')>('$lib/axios');
  return { ...actual, default: mockInstance };
});

import { AuthConfigApi, AUTH_CONFIG_AUDIT_MAX_LIMIT } from './authConfig';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.post.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('getAllConfigs', () => {
  it('fetches all categories and returns the grouped response', async () => {
    const grouped = {
      ldap: [{ id: 1, config_key: 'ldap_enabled' }],
    };
    mockInstance.get.mockResolvedValue({ data: grouped });
    const result = await AuthConfigApi.getAllConfigs();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/auth-config');
    expect(result).toEqual(grouped);
  });
});

describe('getConfigByCategory', () => {
  it('fetches a single category by name', async () => {
    const config = { ldap_enabled: true };
    mockInstance.get.mockResolvedValue({ data: config });
    const result = await AuthConfigApi.getConfigByCategory('ldap');
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/auth-config/ldap');
    expect(result).toEqual(config);
  });
});

describe('updateCategory', () => {
  it('puts the category config to the category route', async () => {
    await AuthConfigApi.updateCategory('oidc', { oidc_enabled: true });
    const [path, body] = mockInstance.put.mock.calls[0];
    expect(path).toBe('/admin/auth-config/oidc');
    expect(body).toEqual({ oidc_enabled: true });
  });
});

describe('testConnection', () => {
  it('posts the config to the category test route and returns the result', async () => {
    mockInstance.post.mockResolvedValue({
      data: { success: true, message: 'Connected' },
    });
    const result = await AuthConfigApi.testConnection('ldap', { ldap_server: 'ldap.example.com' });
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/auth-config/ldap/test', {
      ldap_server: 'ldap.example.com',
    });
    expect(result).toEqual({ success: true, message: 'Connected' });
  });
});

describe('getAuditLog clamping', () => {
  /** Pulls the request path and query params out of the captured call, so assertions
   * read the captured values rather than re-asserting the call itself. */
  function lastGetCall(): { path: string; params: { limit: number; offset: number } } {
    const call = mockInstance.get.mock.calls[mockInstance.get.mock.calls.length - 1];
    const [path, config] = call as [string, { params: { limit: number; offset: number } }];
    return { path, params: config.params };
  }

  it('uses the documented defaults (limit 100, offset 0) when called with none', async () => {
    await AuthConfigApi.getAuditLog('ldap');
    const { path, params } = lastGetCall();
    expect(path).toBe('/admin/auth-config/audit/ldap');
    expect(params).toEqual({ limit: 100, offset: 0 });
  });

  it('clamps a limit below the minimum up to 1', async () => {
    await AuthConfigApi.getAuditLog('ldap', 0);
    const { params } = lastGetCall();
    expect(params).toEqual({ limit: 1, offset: 0 });
  });

  it('clamps a negative limit up to 1', async () => {
    await AuthConfigApi.getAuditLog('ldap', -50);
    const { params } = lastGetCall();
    expect(params).toEqual({ limit: 1, offset: 0 });
  });

  it('clamps a limit above the max down to AUTH_CONFIG_AUDIT_MAX_LIMIT', async () => {
    await AuthConfigApi.getAuditLog('oidc', 10000);
    const { path, params } = lastGetCall();
    expect(path).toBe('/admin/auth-config/audit/oidc');
    expect(params).toEqual({ limit: AUTH_CONFIG_AUDIT_MAX_LIMIT, offset: 0 });
  });

  it('truncates a fractional limit rather than rounding', async () => {
    // Math.trunc(50.9) === 50, not 51 — a caller passing a rounded-up value would
    // silently drift from what the server actually returns.
    await AuthConfigApi.getAuditLog('pki', 50.9);
    const { params } = lastGetCall();
    expect(params).toEqual({ limit: 50, offset: 0 });
  });

  it('leaves an in-range integer limit unchanged', async () => {
    await AuthConfigApi.getAuditLog('saml', 250);
    const { params } = lastGetCall();
    expect(params).toEqual({ limit: 250, offset: 0 });
  });

  it('clamps a negative offset up to 0', async () => {
    await AuthConfigApi.getAuditLog('proxy', 100, -10);
    const { params } = lastGetCall();
    expect(params).toEqual({ limit: 100, offset: 0 });
  });

  it('truncates a fractional offset', async () => {
    await AuthConfigApi.getAuditLog('session', 100, 5.9);
    const { params } = lastGetCall();
    expect(params).toEqual({ limit: 100, offset: 5 });
  });

  it('leaves a large positive offset unchanged (offset has no upper clamp)', async () => {
    await AuthConfigApi.getAuditLog('mfa', 100, 100000);
    const { params } = lastGetCall();
    expect(params).toEqual({ limit: 100, offset: 100000 });
  });

  it('returns the audit rows from the server unchanged', async () => {
    const rows = [
      {
        id: 1,
        uuid: 'a-uuid',
        config_key: 'ldap_bind_password',
        old_value: '[redacted]',
        new_value: '[redacted]',
        change_type: 'update',
        ip_address: '10.0.0.1',
        created_at: '2026-01-01T00:00:00Z',
      },
    ];
    mockInstance.get.mockResolvedValue({ data: rows });
    const result = await AuthConfigApi.getAuditLog('ldap');
    expect(result).toEqual(rows);
  });
});

describe('getAuthMailDesignation', () => {
  it('fetches the current designation and returns it unchanged', async () => {
    const designation = {
      config_uuid: 'cfg-uuid',
      config_name: 'Primary SMTP',
      provider: 'smtp',
      is_enabled: true,
      resolves: true,
      status: 'active' as const,
      env_smtp_configured: false,
    };
    mockInstance.get.mockResolvedValue({ data: designation });
    const result = await AuthConfigApi.getAuthMailDesignation();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/auth-config/email/designation');
    expect(result).toEqual(designation);
  });
});

describe('setAuthMailDesignation', () => {
  it('puts the chosen config uuid and returns the resulting designation', async () => {
    const designation = {
      config_uuid: 'cfg-uuid',
      config_name: 'Primary SMTP',
      provider: 'smtp',
      is_enabled: true,
      resolves: true,
      status: 'active' as const,
      env_smtp_configured: false,
    };
    mockInstance.put.mockResolvedValue({ data: designation });
    const result = await AuthConfigApi.setAuthMailDesignation('cfg-uuid');
    expect(mockInstance.put).toHaveBeenCalledWith('/admin/auth-config/email/designation', {
      config_uuid: 'cfg-uuid',
    });
    expect(result).toEqual(designation);
  });

  it('sends an empty string to clear the designation and fall back to env SMTP', async () => {
    await AuthConfigApi.setAuthMailDesignation('');
    const [path, body] = mockInstance.put.mock.calls[0];
    expect(path).toBe('/admin/auth-config/email/designation');
    expect(body).toEqual({ config_uuid: '' });
  });
});

describe('migrateFromEnv', () => {
  it('posts to the migrate route and returns the migrated count', async () => {
    mockInstance.post.mockResolvedValue({ data: { migrated_count: 5 } });
    const result = await AuthConfigApi.migrateFromEnv();
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/auth-config/migrate');
    expect(result).toEqual({ migrated_count: 5 });
  });
});
