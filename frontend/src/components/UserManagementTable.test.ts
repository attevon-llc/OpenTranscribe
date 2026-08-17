/**
 * `UserManagementTable.svelte` is the admin account-lifecycle panel: search
 * filtering, super_admin-elevation confirmation gates (create/invite/promote,
 * reverting the role `<select>` on cancel), and the lock/unlock/force-logout/MFA-
 * reset actions that go through `runAccountAction` (row-scoped pending state,
 * and — pinned explicitly below — only `lockAccount` refreshes the list
 * afterwards; `unlockAccount` deliberately does not, since unlocking resets only
 * the failed-login counter and changes nothing else visible in the row). This is
 * exactly the "complex derived state and multi-step orchestration" #475 scopes
 * Priority 3 to. `$lib/axios` is mocked at the transport boundary only — the real
 * `AdminApi`/`invitations.ts` client code runs, so the URLs/params under test are
 * what those modules actually build, not a re-guess of them.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

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
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import UserManagementTable from './UserManagementTable.svelte';
import { user as mockUser } from '$stores/auth';
import { toastStore } from '$stores/toast';

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

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockImplementation((url: string) => {
    if (url === '/auth/password-policy') return Promise.resolve({ data: { min_length: 12 } });
    if (url === '/auth/invitations') return Promise.resolve({ data: [] });
    return Promise.resolve({ data: [] });
  });
  mockAxios.post.mockResolvedValue({ data: {} });
  mockAxios.put.mockResolvedValue({ data: {} });
  mockAxios.delete.mockResolvedValue({ data: {} });
  setCurrentUser('me-uuid', 'super_admin');
});

describe('search filtering', () => {
  it('filters by name or email, case-insensitively, and clears back to all on empty input', async () => {
    const users = [
      makeUser({ uuid: 'u-1', full_name: 'Alice Example', email: 'alice@x.com' }),
      makeUser({ uuid: 'u-2', full_name: 'Bob Jones', email: 'bob@x.com' }),
    ];
    const { container, getByPlaceholderText } = render(UserManagementTable, { props: { users } });

    expect(container.querySelectorAll('tbody tr')).toHaveLength(2);

    const search = getByPlaceholderText('userManagement.searchPlaceholder');
    await fireEvent.input(search, { target: { value: 'ALICE' } });
    expect(container.querySelectorAll('tbody tr')).toHaveLength(1);
    expect(container.querySelector('tbody tr')?.textContent).toContain('Alice Example');

    await fireEvent.input(search, { target: { value: '' } });
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
  });
});

describe('role change', () => {
  it('changes a non-elevated role immediately, without a confirmation', async () => {
    const users = [makeUser({ uuid: 'u-1', role: 'user' })];
    const onRefresh = vi.fn();
    const { container } = render(UserManagementTable, { props: { users, onRefresh } });

    const select = container.querySelector('td select') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: 'admin' } });

    await waitFor(() =>
      expect(mockAxios.put).toHaveBeenCalledWith('/admin/users/u-1/role', null, {
        params: { new_role: 'admin' },
      })
    );
    expect(onRefresh).toHaveBeenCalled();
  });

  it('gates promotion to super_admin behind a confirmation, and reverts the select on cancel', async () => {
    const users = [makeUser({ uuid: 'u-1', role: 'user' })];
    const onRefresh = vi.fn();
    const { container, getByText } = render(UserManagementTable, { props: { users, onRefresh } });

    const select = container.querySelector('td select') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: 'super_admin' } });

    expect(mockAxios.put).not.toHaveBeenCalled();
    expect(getByText('userManagement.confirmSuperAdminButton')).not.toBeNull();
    // UserManagementTable's own ConfirmationModal instance hardcodes cancelText
    // to $t('common.cancel') (not ConfirmationModal's own 'modal.cancel' default).
    await fireEvent.click(getByText('common.cancel'));

    expect(mockAxios.put).not.toHaveBeenCalled();
    expect(select.value).toBe('user');
  });

  it('promotes to super_admin once confirmed', async () => {
    const users = [makeUser({ uuid: 'u-1', role: 'user' })];
    const onRefresh = vi.fn();
    const { container, getByText } = render(UserManagementTable, { props: { users, onRefresh } });

    const select = container.querySelector('td select') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: 'super_admin' } });
    await fireEvent.click(getByText('userManagement.confirmSuperAdminButton'));

    await waitFor(() =>
      expect(mockAxios.put).toHaveBeenCalledWith('/admin/users/u-1/role', null, {
        params: { new_role: 'super_admin' },
      })
    );
  });
});

describe('creating a user directly', () => {
  it('refuses to submit when required fields (or password, for a local account) are missing', async () => {
    const { getByText } = render(UserManagementTable, { props: { users: [] } });

    await fireEvent.click(getByText('userManagement.addUser'));
    await fireEvent.click(getByText('userManagement.createUser'));

    expect(toastStore.error).toHaveBeenCalledWith('userManagement.fillAllFields');
    expect(mockAxios.post).not.toHaveBeenCalled();
  });

  it('gates a super_admin creation behind a confirmation and posts the right payload once confirmed', async () => {
    const onRefresh = vi.fn();
    const { container, getByText, getByLabelText } = render(UserManagementTable, {
      props: { users: [], onRefresh },
    });

    await fireEvent.click(getByText('userManagement.addUser'));
    await fireEvent.input(getByLabelText('userManagement.fullName'), {
      target: { value: 'New Admin' },
    });
    await fireEvent.input(getByLabelText('userManagement.email'), {
      target: { value: 'new@x.com' },
    });
    await fireEvent.input(getByLabelText('userManagement.password'), {
      target: { value: 'a-long-enough-password' },
    });
    const roleSelect = container.querySelector('#role') as HTMLSelectElement;
    await fireEvent.change(roleSelect, { target: { value: 'super_admin' } });

    await fireEvent.click(getByText('userManagement.createUser'));
    expect(mockAxios.post).not.toHaveBeenCalled();

    await fireEvent.click(getByText('userManagement.confirmSuperAdminButton'));

    await waitFor(() =>
      expect(mockAxios.post).toHaveBeenCalledWith(
        '/admin/users',
        expect.objectContaining({ email: 'new@x.com', role: 'super_admin', auth_type: 'local' })
      )
    );
    expect(onRefresh).toHaveBeenCalled();
  });

  it('omits the password field entirely for a non-local auth type', async () => {
    const { container, getByText, getByLabelText, queryByLabelText } = render(UserManagementTable, {
      props: { users: [] },
    });

    await fireEvent.click(getByText('userManagement.addUser'));
    const authSelect = container.querySelector('#auth-type') as HTMLSelectElement;
    await fireEvent.change(authSelect, { target: { value: 'ldap' } });

    expect(queryByLabelText('userManagement.password')).toBeNull();

    await fireEvent.input(getByLabelText('userManagement.fullName'), {
      target: { value: 'LDAP User' },
    });
    await fireEvent.input(getByLabelText('userManagement.email'), {
      target: { value: 'ldap@x.com' },
    });
    await fireEvent.click(getByText('userManagement.createUser'));

    await waitFor(() => expect(mockAxios.post).toHaveBeenCalled());
    const [, body] = mockAxios.post.mock.calls[0];
    expect(body).not.toHaveProperty('password');
    expect(body.auth_type).toBe('ldap');
  });
});

describe('inviting a user', () => {
  it('refuses to submit without an email', async () => {
    const { getByText } = render(UserManagementTable, { props: { users: [] } });

    await fireEvent.click(getByText('userManagement.inviteUser'));
    await fireEvent.click(getByText('userManagement.sendInvite'));

    expect(toastStore.error).toHaveBeenCalledWith('userManagement.fillAllFields');
    expect(mockAxios.post).not.toHaveBeenCalled();
  });

  it('gates a super_admin invitation behind a confirmation, then sends it and refreshes the list', async () => {
    const { container, getByText, getByLabelText } = render(UserManagementTable, {
      props: { users: [] },
    });
    await waitFor(() =>
      expect(mockAxios.get).toHaveBeenCalledWith('/auth/invitations', expect.anything())
    );
    mockAxios.get.mockClear();

    await fireEvent.click(getByText('userManagement.inviteUser'));
    await fireEvent.input(getByLabelText('userManagement.email'), {
      target: { value: 'invitee@x.com' },
    });
    const roleSelect = container.querySelector('#invite-role') as HTMLSelectElement;
    await fireEvent.change(roleSelect, { target: { value: 'super_admin' } });

    await fireEvent.click(getByText('userManagement.sendInvite'));
    expect(mockAxios.post).not.toHaveBeenCalled();

    await fireEvent.click(getByText('userManagement.confirmSuperAdminButton'));

    await waitFor(() =>
      expect(mockAxios.post).toHaveBeenCalledWith(
        '/auth/invitations',
        expect.objectContaining({ email: 'invitee@x.com', role: 'super_admin' })
      )
    );
    await waitFor(() =>
      expect(mockAxios.get).toHaveBeenCalledWith('/auth/invitations', expect.anything())
    );
  });
});

describe('password reset', () => {
  // The submit button is disabled while either field is empty (same condition
  // executePasswordReset itself checks), so the empty-fields branch inside the
  // handler is unreachable via a click — only the mismatch and too-short cases,
  // which leave the button enabled, are exercised here.
  it('rejects a mismatch, then rejects one that is too short for the loaded minimum', async () => {
    const users = [makeUser({ uuid: 'u-1' })];
    const { getByTitle, getByLabelText, getByText } = render(UserManagementTable, {
      props: { users },
    });
    await waitFor(() => expect(mockAxios.get).toHaveBeenCalledWith('/auth/password-policy'));

    await fireEvent.click(getByTitle('userManagement.resetPasswordFor', { exact: false }));
    await fireEvent.input(getByLabelText('userManagement.newPassword'), {
      target: { value: 'abcdefghijkl' },
    });
    await fireEvent.input(getByLabelText('userManagement.confirmPassword'), {
      target: { value: 'different123' },
    });
    await fireEvent.click(getByText('userManagement.resetPasswordButton'));
    expect(toastStore.error).toHaveBeenCalledWith('userManagement.passwordsDoNotMatch');

    await fireEvent.input(getByLabelText('userManagement.newPassword'), {
      target: { value: 'short' },
    });
    await fireEvent.input(getByLabelText('userManagement.confirmPassword'), {
      target: { value: 'short' },
    });
    await fireEvent.click(getByText('userManagement.resetPasswordButton'));
    expect(toastStore.error).toHaveBeenCalledWith('userManagement.passwordMinLength');

    expect(mockAxios.put).not.toHaveBeenCalled();
  });

  it('resets the password and closes the modal on success', async () => {
    const users = [makeUser({ uuid: 'u-1' })];
    const { getByTitle, getByLabelText, getByText, queryByText } = render(UserManagementTable, {
      props: { users },
    });
    await waitFor(() => expect(mockAxios.get).toHaveBeenCalledWith('/auth/password-policy'));

    await fireEvent.click(getByTitle('userManagement.resetPasswordFor', { exact: false }));
    await fireEvent.input(getByLabelText('userManagement.newPassword'), {
      target: { value: 'a-valid-password' },
    });
    await fireEvent.input(getByLabelText('userManagement.confirmPassword'), {
      target: { value: 'a-valid-password' },
    });
    await fireEvent.click(getByText('userManagement.resetPasswordButton'));

    await waitFor(() =>
      expect(mockAxios.put).toHaveBeenCalledWith('/users/u-1', { password: 'a-valid-password' })
    );
    await waitFor(() => expect(queryByText('userManagement.resetPasswordButton')).toBeNull());
  });
});

describe('lock / unlock: only lock refreshes the list', () => {
  it('locking posts with a reason, disables the row action while pending, and refreshes on success', async () => {
    let resolvePost!: (v: { data: { success: boolean } }) => void;
    mockAxios.post.mockReturnValue(new Promise((r) => (resolvePost = r)));
    const users = [makeUser({ uuid: 'u-1', is_active: true })];
    const onRefresh = vi.fn();
    const { getByTitle, getByRole } = render(UserManagementTable, { props: { users, onRefresh } });

    await fireEvent.click(getByTitle('userManagement.lockAccountFor', { exact: false }));
    // The confirmation title and its confirm button share the same i18n key
    // (lockAccount()'s own showConfirmation call), so scope to the button role.
    await fireEvent.click(getByRole('button', { name: 'userManagement.lockAccount' }));

    const lockBtn = getByTitle('userManagement.lockAccountFor', {
      exact: false,
    }) as HTMLButtonElement;
    expect(lockBtn.disabled).toBe(true);

    resolvePost({ data: { success: true } });
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(mockAxios.post).toHaveBeenCalledWith('/admin/users/u-1/lock', null, {
      params: { reason: 'Locked by admin from user management' },
    });
  });

  it('unlocking posts but does NOT refresh the list (nothing visible in the row changes)', async () => {
    mockAxios.post.mockResolvedValue({ data: { success: true, was_locked: true } });
    const users = [makeUser({ uuid: 'u-1' })];
    const onRefresh = vi.fn();
    const { getByTitle, getByRole } = render(UserManagementTable, { props: { users, onRefresh } });

    await fireEvent.click(getByTitle('userManagement.unlockAccountFor', { exact: false }));
    await fireEvent.click(getByRole('button', { name: 'userManagement.unlockAccount' }));

    await waitFor(() => expect(mockAxios.post).toHaveBeenCalledWith('/admin/users/u-1/unlock'));
    expect(onRefresh).not.toHaveBeenCalled();
  });
});

describe('inline account-expiration editor', () => {
  it('pre-fills the existing expiration date when opened', async () => {
    const users = [makeUser({ uuid: 'u-1', account_expires_at: '2026-03-15T23:59:59Z' })];
    const { getByTitle, container } = render(UserManagementTable, { props: { users } });

    await fireEvent.click(getByTitle('userManagement.editExpirationFor', { exact: false }));

    const dateInput = container.querySelector('input[type="date"]') as HTMLInputElement;
    expect(dateInput.value).toBe('2026-03-15');
  });

  it('saves a chosen date as end-of-day ISO, and refreshes', async () => {
    const users = [makeUser({ uuid: 'u-1' })];
    const onRefresh = vi.fn();
    const { getByTitle, container } = render(UserManagementTable, { props: { users, onRefresh } });

    await fireEvent.click(getByTitle('userManagement.setExpirationFor', { exact: false }));
    const dateInput = container.querySelector('input[type="date"]') as HTMLInputElement;
    await fireEvent.input(dateInput, { target: { value: '2026-06-01' } });
    await fireEvent.click(container.querySelector('.expiration-save-button') as HTMLElement);

    await waitFor(() =>
      expect(mockAxios.put).toHaveBeenCalledWith('/users/u-1', {
        account_expires_at: '2026-06-01T23:59:59',
      })
    );
    expect(onRefresh).toHaveBeenCalled();
  });

  it('clearing the date and saving sends null (removes the expiration)', async () => {
    const users = [makeUser({ uuid: 'u-1', account_expires_at: '2026-03-15T23:59:59Z' })];
    const { getByTitle, container } = render(UserManagementTable, { props: { users } });

    await fireEvent.click(getByTitle('userManagement.editExpirationFor', { exact: false }));
    const dateInput = container.querySelector('input[type="date"]') as HTMLInputElement;
    await fireEvent.input(dateInput, { target: { value: '' } });
    await fireEvent.click(container.querySelector('.expiration-save-button') as HTMLElement);

    await waitFor(() =>
      expect(mockAxios.put).toHaveBeenCalledWith('/users/u-1', { account_expires_at: null })
    );
  });

  it('cancel closes the editor without saving', async () => {
    const users = [makeUser({ uuid: 'u-1' })];
    const { getByTitle, container } = render(UserManagementTable, { props: { users } });

    await fireEvent.click(getByTitle('userManagement.setExpirationFor', { exact: false }));
    await fireEvent.click(container.querySelector('.expiration-cancel-button') as HTMLElement);

    expect(container.querySelector('input[type="date"]')).toBeNull();
    expect(mockAxios.put).not.toHaveBeenCalled();
  });
});
