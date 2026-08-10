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

const post = vi.mocked(axiosInstance.post);
const del = vi.mocked(axiosInstance.delete);

describe('AdminApi account actions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    post.mockResolvedValue({ data: { success: true } } as never);
    del.mockResolvedValue({ data: { success: true, sessions_terminated: 3 } } as never);
  });

  describe('resetUserPassword', () => {
    it('sends the password in the request body, never as a query parameter', async () => {
      await AdminApi.resetUserPassword('user-uuid', 'sup3r-secret-pw', true);

      expect(post).toHaveBeenCalledTimes(1);
      const [url, body, config] = post.mock.calls[0];
      expect(url).toBe('/admin/users/user-uuid/reset-password');
      expect(body).toEqual({ new_password: 'sup3r-secret-pw', force_change: true });

      // The backend takes AdminPasswordResetRequest as a body precisely so the
      // password stays out of server logs, browser history and referrer headers.
      // Any query params here would put it back into all three (and 422).
      expect(config?.params).toBeUndefined();
      expect(JSON.stringify(config ?? {})).not.toContain('sup3r-secret-pw');
    });

    it('defaults force_change to true', async () => {
      await AdminApi.resetUserPassword('user-uuid', 'another-secret-pw');
      expect(post.mock.calls[0][1]).toEqual({
        new_password: 'another-secret-pw',
        force_change: true,
      });
    });

    it('honours force_change: false', async () => {
      await AdminApi.resetUserPassword('user-uuid', 'another-secret-pw', false);
      expect(post.mock.calls[0][1]).toMatchObject({ force_change: false });
    });
  });

  it('locks an account with an audit reason and returns the payload', async () => {
    const result = await AdminApi.lockAccount('user-uuid', 'Locked by admin from user management');

    expect(post).toHaveBeenCalledWith('/admin/users/user-uuid/lock', null, {
      params: { reason: 'Locked by admin from user management' },
    });
    expect(result).toEqual({ success: true });
  });

  it('surfaces was_locked from the unlock endpoint', async () => {
    post.mockResolvedValue({ data: { success: true, was_locked: false } } as never);

    const result = await AdminApi.unlockAccount('user-uuid');

    expect(post).toHaveBeenCalledWith('/admin/users/user-uuid/unlock');
    expect(result).toEqual({ success: true, was_locked: false });
  });

  it('returns the terminated session count from a force logout', async () => {
    const result = await AdminApi.terminateUserSessions('user-uuid');

    expect(del).toHaveBeenCalledWith('/admin/users/user-uuid/sessions');
    expect(result.sessions_terminated).toBe(3);
  });

  it('resets MFA through the admin endpoint', async () => {
    const result = await AdminApi.resetUserMFA('user-uuid');

    expect(post).toHaveBeenCalledWith('/admin/users/user-uuid/mfa/reset');
    expect(result).toEqual({ success: true });
  });
});
