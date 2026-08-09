import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { axiosInstance, default: axiosInstance };
});

import { axiosInstance } from '$lib/axios';
import { AuthConfigApi, AUTH_CONFIG_AUDIT_MAX_LIMIT } from './authConfig';

const get = vi.mocked(axiosInstance.get);

describe('AuthConfigApi.getAuditLog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue({ data: [] } as never);
  });

  it('requests the category route with a bounded page', async () => {
    await AuthConfigApi.getAuditLog('ldap');

    expect(get).toHaveBeenCalledWith('/admin/auth-config/audit/ldap', {
      params: { limit: 100, offset: 0 },
    });
  });

  it('clamps an over-large page to the server ceiling instead of 422-ing', async () => {
    await AuthConfigApi.getAuditLog('oidc', 100000);

    expect(get.mock.calls[0][1]?.params.limit).toBe(AUTH_CONFIG_AUDIT_MAX_LIMIT);
  });

  it('clamps non-positive limits and offsets', async () => {
    await AuthConfigApi.getAuditLog('pki', 0, -5);

    expect(get.mock.calls[0][1]?.params).toEqual({ limit: 1, offset: 0 });
  });

  it('passes the offset through for paging', async () => {
    await AuthConfigApi.getAuditLog('session', 50, 150);

    expect(get.mock.calls[0][1]?.params).toEqual({ limit: 50, offset: 150 });
  });
});
