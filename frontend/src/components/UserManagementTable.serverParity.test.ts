/**
 * Every per-row action `UserManagementTable.svelte` offers must be one the backend
 * accepts for that viewer and that target. A button the server always refuses is a
 * dead control at best; on this panel it is a privilege boundary drawn in two places
 * that disagree. The rules below are the server's, read from the endpoints:
 *
 * - `POST /admin/users/{uuid}/lock`, `DELETE /admin/users/{uuid}/sessions`
 *   (admin_accounts.py `_targets_a_super_admin_without_authority`), `PUT /users/{uuid}`
 *   and `DELETE /users/{uuid}` (users_admin_edit.py): a plain admin may not act on a
 *   super_admin account.
 * - `POST /admin/users/{uuid}/unlock`, `POST /tasks/system/recover-user-files/{uuid}`:
 *   any admin, any target.
 * - A local password can be set only where `auth.utils.local_password_allowed` says
 *   so: `local`, or `pki`/`oidc` with `allow_local_fallback`. Never `ldap`/`saml`.
 * - `allow_local_fallback` can be turned ON only for `pki`/`oidc`
 *   (`assert_local_fallback_settable`); turning it off is always accepted.
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

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import UserManagementTable from './UserManagementTable.svelte';
import { user as mockUser } from '$stores/auth';

function setCurrentUser(uuid: string, role: 'admin' | 'super_admin') {
  (mockUser as unknown as { set: (v: { uuid: string; role: string }) => void }).set({ uuid, role });
}

function makeUser(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    uuid: 'u-1',
    email: 'alice@example.com',
    full_name: 'Alice Example',
    role: 'user',
    created_at: '2026-01-01T00:00:00Z',
    is_active: true,
    auth_type: 'local',
    ...overrides,
  };
}

/** The action buttons rendered in one user's row, by their button class. */
function rowButtons(container: HTMLElement, uuidName: string): string[] {
  const row = Array.from(container.querySelectorAll('tbody tr')).find(
    (tr) => tr.textContent?.includes(uuidName)
  );
  if (!row) throw new Error(`no row for ${uuidName}`);
  return Array.from(row.querySelectorAll('button.icon-button')).map(
    (b) => Array.from(b.classList).find((c) => c.endsWith('-button') && c !== 'icon-button') ?? ''
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockImplementation((url: string) => {
    if (url === '/auth/password-policy') return Promise.resolve({ data: { min_length: 12 } });
    return Promise.resolve({ data: [] });
  });
});

describe('a plain admin looking at a super_admin row', () => {
  const users = [
    makeUser({ uuid: 'sa-1', full_name: 'Super Root', role: 'super_admin' }),
    makeUser({ uuid: 'u-1', full_name: 'Plain User', role: 'user' }),
  ];

  beforeEach(() => setCurrentUser('me-uuid', 'admin'));

  it('offers none of the actions the server refuses: lock, force logout, expiration, delete, password', () => {
    const { container } = render(UserManagementTable, { props: { users } });
    const actions = rowButtons(container, 'Super Root');

    expect(actions).not.toContain('lock-button');
    expect(actions).not.toContain('force-logout-button');
    expect(actions).not.toContain('expiration-button');
    expect(actions).not.toContain('delete-button');
    expect(actions).not.toContain('reset-password-button');
  });

  it('still offers the actions the server accepts from any admin: unlock and file recovery', () => {
    const { container } = render(UserManagementTable, { props: { users } });
    expect(rowButtons(container, 'Super Root')).toEqual(['unlock-button', 'recover-button']);
  });

  it('keeps every admin-tier action on an ordinary row', () => {
    const { container } = render(UserManagementTable, { props: { users } });
    expect(rowButtons(container, 'Plain User')).toEqual([
      'reset-password-button',
      'lock-button',
      'unlock-button',
      'force-logout-button',
      'expiration-button',
      'recover-button',
      'delete-button',
    ]);
  });
});

describe('a super_admin looking at a super_admin row', () => {
  it('offers the actions a plain admin is refused', () => {
    setCurrentUser('me-uuid', 'super_admin');
    const users = [makeUser({ uuid: 'sa-1', full_name: 'Super Root', role: 'super_admin' })];
    const { container } = render(UserManagementTable, { props: { users } });
    const actions = rowButtons(container, 'Super Root');

    for (const action of [
      'lock-button',
      'force-logout-button',
      'expiration-button',
      'delete-button',
    ]) {
      expect(actions).toContain(action);
    }
  });
});

describe('local password reset follows local_password_allowed', () => {
  beforeEach(() => setCurrentUser('me-uuid', 'super_admin'));

  it.each([
    ['local', false, true],
    ['oidc', true, true],
    ['pki', true, true],
    ['oidc', false, false],
    ['ldap', true, false],
    ['saml', true, false],
  ])('auth_type=%s, allow_local_fallback=%s -> offered: %s', (authType, fallback, offered) => {
    const users = [
      makeUser({ full_name: 'Target', auth_type: authType, allow_local_fallback: fallback }),
    ];
    const { container } = render(UserManagementTable, { props: { users } });
    expect(rowButtons(container, 'Target').includes('reset-password-button')).toBe(offered);
  });
});

describe('the local-fallback toggle follows assert_local_fallback_settable', () => {
  beforeEach(() => setCurrentUser('me-uuid', 'super_admin'));

  it.each([
    ['oidc', false, true],
    ['pki', false, true],
    ['local', false, false],
    ['ldap', false, false],
    ['saml', false, false],
    // Turning the flag OFF is always accepted, so a stale ON stays switchable off.
    ['saml', true, true],
    ['ldap', true, true],
  ])('auth_type=%s, allow_local_fallback=%s -> offered: %s', (authType, fallback, offered) => {
    const users = [
      makeUser({ full_name: 'Target', auth_type: authType, allow_local_fallback: fallback }),
    ];
    const { container } = render(UserManagementTable, { props: { users } });
    expect(rowButtons(container, 'Target').includes('fallback-toggle-button')).toBe(offered);
  });

  it('is never offered to a plain admin, whose allow_local_fallback write the server strips', () => {
    setCurrentUser('me-uuid', 'admin');
    const users = [makeUser({ full_name: 'Target', auth_type: 'oidc' })];
    const { container } = render(UserManagementTable, { props: { users } });
    expect(rowButtons(container, 'Target')).not.toContain('fallback-toggle-button');
  });
});
