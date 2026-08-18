import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * Transport-only client: these assert the wire shape each method produces
 * (path, method, params/body) plus the response-value mapping for the two
 * methods with real logic — `createUser`'s auth_type-gated password field and
 * `is_active` fallback, and `getAuditLogs`'s dual response-shape normalizer.
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

import { AdminApi } from './admin';

const USER_UUID = '11111111-1111-1111-1111-111111111111';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.post.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('createUser', () => {
  it('includes password when auth_type is local and a password is supplied', async () => {
    const created = { uuid: USER_UUID, email: 'a@example.com', auth_type: 'local' };
    mockInstance.post.mockResolvedValue({ data: created });
    const result = await AdminApi.createUser({
      email: 'a@example.com',
      full_name: 'Ada',
      role: 'user',
      auth_type: 'local',
      password: 'hunter2',
    });
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/users', {
      email: 'a@example.com',
      full_name: 'Ada',
      role: 'user',
      auth_type: 'local',
      is_active: true,
      password: 'hunter2',
    });
    expect(result).toEqual(created);
  });

  it('omits password entirely for a non-local auth_type, even if one is supplied', async () => {
    // A stored credential policy will never accept is worse than a rejected request:
    // the client must never let a caller-supplied password leak into an external
    // account's create payload.
    mockInstance.post.mockResolvedValue({
      data: { uuid: USER_UUID, email: 'b@example.com', auth_type: 'oidc' },
    });
    await AdminApi.createUser({
      email: 'b@example.com',
      full_name: 'Bea',
      role: 'user',
      auth_type: 'oidc',
      password: 'should-not-be-sent',
    });
    const body = mockInstance.post.mock.calls[0][1];
    expect(body).not.toHaveProperty('password');
    expect(body).toEqual({
      email: 'b@example.com',
      full_name: 'Bea',
      role: 'user',
      auth_type: 'oidc',
      is_active: true,
    });
  });

  it('omits password for a local auth_type when no password is supplied', async () => {
    await AdminApi.createUser({
      email: 'c@example.com',
      full_name: 'Cy',
      role: 'user',
      auth_type: 'local',
    });
    const body = mockInstance.post.mock.calls[0][1];
    expect(body).not.toHaveProperty('password');
  });

  it('defaults is_active to true when omitted', async () => {
    await AdminApi.createUser({
      email: 'd@example.com',
      full_name: 'Dee',
      role: 'user',
      auth_type: 'local',
    });
    expect(mockInstance.post.mock.calls[0][1]).toMatchObject({ is_active: true });
  });

  it('preserves an explicit is_active: false rather than falling back to true', async () => {
    await AdminApi.createUser({
      email: 'e@example.com',
      full_name: 'Eli',
      role: 'user',
      auth_type: 'local',
      is_active: false,
    });
    expect(mockInstance.post.mock.calls[0][1]).toMatchObject({ is_active: false });
  });

  it('returns the created user row from the server', async () => {
    const row = { uuid: USER_UUID, email: 'a@example.com', role: 'user' };
    mockInstance.post.mockResolvedValue({ data: row });
    const result = await AdminApi.createUser({
      email: 'a@example.com',
      full_name: 'Ada',
      role: 'user',
      auth_type: 'local',
    });
    expect(result).toEqual(row);
  });
});

describe('resetUserPassword', () => {
  it('sends the new password in the body, not as a query param, with forceChange defaulted true', async () => {
    await AdminApi.resetUserPassword(USER_UUID, 'newpass123');
    const [path, body] = mockInstance.post.mock.calls[0];
    expect(path).toBe(`/admin/users/${USER_UUID}/reset-password`);
    expect(body).toEqual({ new_password: 'newpass123', force_change: true });
  });

  it('passes an explicit forceChange: false through', async () => {
    await AdminApi.resetUserPassword(USER_UUID, 'newpass123', false);
    const [path, body] = mockInstance.post.mock.calls[0];
    expect(path).toBe(`/admin/users/${USER_UUID}/reset-password`);
    expect(body).toEqual({ new_password: 'newpass123', force_change: false });
  });
});

describe('unlockAccount', () => {
  it('posts to the unlock route and returns the was_locked flag', async () => {
    mockInstance.post.mockResolvedValue({ data: { success: true, was_locked: true } });
    const result = await AdminApi.unlockAccount(USER_UUID);
    expect(mockInstance.post).toHaveBeenCalledWith(`/admin/users/${USER_UUID}/unlock`);
    expect(result).toEqual({ success: true, was_locked: true });
  });
});

describe('lockAccount', () => {
  it('sends the reason as a query param with a null body', async () => {
    mockInstance.post.mockResolvedValue({ data: { success: true } });
    const result = await AdminApi.lockAccount(USER_UUID, 'policy violation');
    expect(mockInstance.post).toHaveBeenCalledWith(`/admin/users/${USER_UUID}/lock`, null, {
      params: { reason: 'policy violation' },
    });
    expect(result).toEqual({ success: true });
  });
});

describe('terminateUserSessions', () => {
  it('deletes the sessions collection and returns the terminated count', async () => {
    mockInstance.delete.mockResolvedValue({ data: { sessions_terminated: 3 } });
    const result = await AdminApi.terminateUserSessions(USER_UUID);
    expect(mockInstance.delete).toHaveBeenCalledWith(`/admin/users/${USER_UUID}/sessions`);
    expect(result).toEqual({ sessions_terminated: 3 });
  });
});

describe('getUserSessions', () => {
  it('fetches and returns the sessions list unchanged', async () => {
    const sessions = [
      {
        id: 's1',
        created_at: '2026-01-01T00:00:00Z',
        expires_at: '2026-01-02T00:00:00Z',
        ip_address: '10.0.0.1',
        user_agent: 'curl',
      },
    ];
    mockInstance.get.mockResolvedValue({ data: { sessions } });
    const result = await AdminApi.getUserSessions(USER_UUID);
    expect(mockInstance.get).toHaveBeenCalledWith(`/admin/users/${USER_UUID}/sessions`);
    expect(result).toEqual({ sessions });
  });
});

describe('changeUserRole', () => {
  it('sends the new role as a query param via PUT', async () => {
    await AdminApi.changeUserRole(USER_UUID, 'admin');
    const [path, body, config] = mockInstance.put.mock.calls[0];
    expect(path).toBe(`/admin/users/${USER_UUID}/role`);
    expect(body).toBeNull();
    expect(config).toEqual({ params: { new_role: 'admin' } });
  });
});

describe('resetUserMFA', () => {
  it('posts to the mfa reset route and returns the result', async () => {
    mockInstance.post.mockResolvedValue({ data: { success: true } });
    const result = await AdminApi.resetUserMFA(USER_UUID);
    expect(mockInstance.post).toHaveBeenCalledWith(`/admin/users/${USER_UUID}/mfa/reset`);
    expect(result).toEqual({ success: true });
  });
});

describe('linkExternalIdentity', () => {
  it('puts the provider and identifier and returns the server response', async () => {
    mockInstance.put.mockResolvedValue({
      data: { success: true, provider: 'oidc', identifier: 'sub-123' },
    });
    const result = await AdminApi.linkExternalIdentity(USER_UUID, 'oidc', 'sub-123');
    expect(mockInstance.put).toHaveBeenCalledWith(`/admin/users/${USER_UUID}/link-identity`, {
      provider: 'oidc',
      identifier: 'sub-123',
    });
    expect(result).toEqual({ success: true, provider: 'oidc', identifier: 'sub-123' });
  });
});

describe('searchUsers', () => {
  it('forwards the filter params and returns total + users', async () => {
    const users = [
      {
        uuid: USER_UUID,
        email: 'a@example.com',
        full_name: 'Ada',
        role: 'user',
        auth_type: 'local',
        is_active: true,
        last_login_at: null,
        created_at: '2026-01-01T00:00:00Z',
      },
    ];
    mockInstance.get.mockResolvedValue({ data: { total: 1, users } });
    const params = { query: 'ada', role: 'user', is_active: true, limit: 10, offset: 0 };
    const result = await AdminApi.searchUsers(params);
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/users/search', { params });
    expect(result).toEqual({ total: 1, users });
  });
});

describe('getAuditLogs', () => {
  it('normalizes a raw array response into the logs/total/offset/limit shape', async () => {
    const rows = [
      {
        id: 1,
        timestamp: '2026-01-01T00:00:00Z',
        event_type: 'login',
        user_id: 1,
        username: 'admin',
        outcome: 'success',
        source_ip: '10.0.0.1',
        user_agent: 'curl',
        details: {},
      },
    ];
    mockInstance.get.mockResolvedValue({ data: rows });
    const result = await AdminApi.getAuditLogs({ limit: 50 });
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/audit-logs', { params: { limit: 50 } });
    expect(result).toEqual({ logs: rows, total: rows.length, offset: 0, limit: rows.length });
  });

  it('passes through a well-formed object response unchanged', async () => {
    const shaped = {
      logs: [
        {
          id: 2,
          timestamp: '2026-01-02T00:00:00Z',
          event_type: 'logout',
          user_id: 2,
          username: 'bob',
          outcome: 'success',
          source_ip: '10.0.0.2',
          user_agent: 'curl',
          details: {},
        },
      ],
      total: 42,
      offset: 10,
      limit: 20,
    };
    mockInstance.get.mockResolvedValue({ data: shaped });
    const result = await AdminApi.getAuditLogs({});
    expect(result).toEqual(shaped);
  });

  it('falls back to defaults for missing fields on an object response', async () => {
    // The object branch applies `??` per field, independently — a response missing
    // just `offset` (say) must not fall through to the array-shape defaults either.
    mockInstance.get.mockResolvedValue({ data: {} });
    const result = await AdminApi.getAuditLogs({});
    expect(result).toEqual({ logs: [], total: 0, offset: 0, limit: 100 });
  });

  it('preserves a legitimate zero rather than treating it as missing', async () => {
    mockInstance.get.mockResolvedValue({ data: { logs: [], total: 0, offset: 0, limit: 0 } });
    const result = await AdminApi.getAuditLogs({});
    expect(result).toEqual({ logs: [], total: 0, offset: 0, limit: 0 });
  });
});

describe('exportAuditLogs', () => {
  it('requests a blob with the format and date range as params', async () => {
    const blob = new Blob(['csv,data'], { type: 'text/csv' });
    mockInstance.get.mockResolvedValue({ data: blob });
    const result = await AdminApi.exportAuditLogs('csv', '2026-01-01', '2026-01-31');
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/audit-logs/export', {
      params: { export_format: 'csv', start_date: '2026-01-01', end_date: '2026-01-31' },
      responseType: 'blob',
    });
    expect(result).toBe(blob);
  });

  it('sends undefined dates through when omitted', async () => {
    await AdminApi.exportAuditLogs('json');
    const [path, config] = mockInstance.get.mock.calls[0];
    expect(path).toBe('/admin/audit-logs/export');
    expect(config).toEqual({
      params: { export_format: 'json', start_date: undefined, end_date: undefined },
      responseType: 'blob',
    });
  });
});

describe('getAccountStatusReport', () => {
  it('fetches and returns the report unchanged', async () => {
    const report = {
      total_users: 10,
      active_users: 8,
      inactive_users: 2,
      mfa_enabled_users: 3,
      password_expired_users: 1,
    };
    mockInstance.get.mockResolvedValue({ data: report });
    const result = await AdminApi.getAccountStatusReport();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/reports/account-status');
    expect(result).toEqual(report);
  });
});
