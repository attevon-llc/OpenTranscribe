/**
 * `UserCreate` refuses a password on an external auth_type with a 422 rather
 * than dropping it silently — storing a credential policy will never accept is
 * worse than an error. The client must therefore OMIT the key, not send an
 * empty string.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance };
});

import axiosInstance from '$lib/axios';
import { AdminApi } from './admin';

const post = vi.mocked(axiosInstance.post);

describe('AdminApi.createUser', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    post.mockResolvedValue({ data: { uuid: 'u1' } } as never);
  });

  it('sends the password for a local account', async () => {
    await AdminApi.createUser({
      email: 'a@b.test',
      full_name: 'Ada',
      role: 'user',
      auth_type: 'local',
      password: 'a-strong-password',
    });

    expect(post).toHaveBeenCalledWith('/admin/users', {
      email: 'a@b.test',
      full_name: 'Ada',
      role: 'user',
      auth_type: 'local',
      is_active: true,
      password: 'a-strong-password',
    });
  });

  it.each(['ldap', 'oidc', 'pki'] as const)(
    'omits the password key entirely for auth_type=%s',
    async (authType) => {
      await AdminApi.createUser({
        email: 'a@b.test',
        full_name: 'Ada',
        role: 'user',
        auth_type: authType,
        // A stale value left in the form must not reach the wire.
        password: 'leftover-from-the-form',
      });

      const body = post.mock.calls[0][1] as Record<string, unknown>;
      expect(body).not.toHaveProperty('password');
      expect(body.auth_type).toBe(authType);
      expect(JSON.stringify(body)).not.toContain('leftover-from-the-form');
    }
  );

  it('omits the password for a local account when none was typed', async () => {
    await AdminApi.createUser({
      email: 'a@b.test',
      full_name: 'Ada',
      role: 'user',
      auth_type: 'local',
    });

    expect(post.mock.calls[0][1]).not.toHaveProperty('password');
  });

  it('never sends is_superuser — it is derived from role server-side', async () => {
    await AdminApi.createUser({
      email: 'a@b.test',
      full_name: 'Ada',
      role: 'super_admin',
      auth_type: 'local',
      password: 'a-strong-password',
    });

    expect(post.mock.calls[0][1]).not.toHaveProperty('is_superuser');
  });
});
