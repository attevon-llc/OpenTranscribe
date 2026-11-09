/**
 * The admin user table offers "reset password" only to accounts that can hold a local
 * password: `local` rows, and `pki`/`oidc` rows whose `allow_local_fallback` is set. An
 * `ldap` row never gets it, fallback flag or not: its password lives in the directory and
 * the server refuses the write with a 400 (`assert_password_auth_possible`).
 * `rowPermissions.test.ts` pins the rule as a pure function; this pins the rendered table.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { user: writable<{ uuid: string; role: string } | null>(null) };
});

vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);

import UserManagementTable from './UserManagementTable.svelte';
import { user as mockUser } from '$stores/auth';

function row(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    uuid: 'u-1',
    email: 'alice@example.com',
    full_name: 'Alice Example',
    role: 'user',
    created_at: '2026-01-01T00:00:00Z',
    is_active: true,
    ...overrides,
  };
}

function resetButtons(users: ReturnType<typeof row>[]) {
  const { container } = render(UserManagementTable, { props: { users } });
  return container.querySelectorAll('.reset-password-button');
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockResolvedValue({ data: [] });
  (mockUser as unknown as { set: (v: { uuid: string; role: string }) => void }).set({
    uuid: 'me-uuid',
    role: 'super_admin',
  });
});

describe('reset-password action by auth_type', () => {
  it('is offered for a local account', () => {
    expect(resetButtons([row({ auth_type: 'local' })])).toHaveLength(1);
  });

  it('is never offered for an LDAP account', () => {
    expect(resetButtons([row({ auth_type: 'ldap' })])).toHaveLength(0);
  });

  it('is not offered for an LDAP account even with the local fallback flag set', () => {
    expect(resetButtons([row({ auth_type: 'ldap', allow_local_fallback: true })])).toHaveLength(0);
  });

  it('is offered for an OIDC account only once its local fallback is enabled', () => {
    expect(resetButtons([row({ auth_type: 'oidc', allow_local_fallback: false })])).toHaveLength(0);
    expect(resetButtons([row({ auth_type: 'oidc', allow_local_fallback: true })])).toHaveLength(1);
  });

  it('is decided per row in a mixed table', () => {
    const buttons = resetButtons([
      row({ uuid: 'u-local', auth_type: 'local', full_name: 'Local Person' }),
      row({ uuid: 'u-ldap', auth_type: 'ldap', full_name: 'Directory Person' }),
    ]);

    expect(buttons).toHaveLength(1);
    expect(buttons[0].getAttribute('title')).toBe('userManagement.resetPasswordFor');
    expect(buttons[0].closest('tr')?.textContent).toContain('Local Person');
  });
});
