import { beforeEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import { createConfirmDialog } from '$lib/confirmDialog';
import { createAccountSessionActions } from './accountSessionActions';
import type { AccountRow } from './accountActionContext';

const { admin, toast } = vi.hoisted(() => ({
  admin: {
    lockAccount: vi.fn(),
    unlockAccount: vi.fn(),
    terminateUserSessions: vi.fn(),
    resetUserMFA: vi.fn(),
  },
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('$lib/api/admin', () => ({ AdminApi: admin }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
vi.mock('$lib/utils/apiError', () => ({
  getErrorMessage: (_err: unknown, fallback: string) => fallback,
}));

const target: AccountRow = { uuid: 'u1', email: 'a@x.com', full_name: 'Ann', role: 'user' };

function setup() {
  const confirmDialog = createConfirmDialog();
  const onRefresh = vi.fn();
  const pendingUuid = writable<string | null>(null);
  const seen: (string | null)[] = [];
  pendingUuid.subscribe((v) => seen.push(v));
  const tr = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}:${JSON.stringify(vars)}` : key;
  const actions = createAccountSessionActions({ confirmDialog, onRefresh, pendingUuid, tr });
  return { actions, confirmDialog, onRefresh, seen };
}

beforeEach(() => vi.clearAllMocks());

describe('createAccountSessionActions', () => {
  it('asks before locking, and does nothing until confirmed', () => {
    const { actions, confirmDialog } = setup();
    actions.lockAccount(target);
    expect(admin.lockAccount).not.toHaveBeenCalled();
    confirmDialog.confirm();
    expect(admin.lockAccount).toHaveBeenCalledWith('u1', 'Locked by admin from user management');
  });

  it('locks, toasts with the display name, refetches, and marks the row pending meanwhile', async () => {
    admin.lockAccount.mockResolvedValue({});
    const { actions, confirmDialog, onRefresh, seen } = setup();
    actions.lockAccount(target);
    confirmDialog.confirm();
    await vi.waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
    expect(toast.success).toHaveBeenCalledWith('userManagement.lockSuccess:{"name":"Ann"}');
    expect(seen).toEqual([null, 'u1', null]);
  });

  it('reports a failure with the fallback sentence, never refetches, and clears the pending row', async () => {
    admin.resetUserMFA.mockRejectedValue(new Error('boom'));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { actions, confirmDialog, onRefresh, seen } = setup();
    actions.resetUserMFA(target);
    confirmDialog.confirm();
    await vi.waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('userManagement.resetMfaFailed')
    );
    expect(onRefresh).not.toHaveBeenCalled();
    expect(seen.at(-1)).toBeNull();
  });

  it.each([
    [{ was_locked: true, was_disabled: true }, 'unlockSuccess', 1],
    [{ was_locked: true, was_disabled: false }, 'unlockSuccess', 0],
    [{ was_locked: false, was_disabled: false }, 'unlockNotLocked', 0],
  ])('unlock %j reports %s and refetches %i time(s)', async (result, key, refreshes) => {
    admin.unlockAccount.mockResolvedValue(result);
    const { actions, confirmDialog, onRefresh } = setup();
    actions.unlockAccount(target);
    confirmDialog.confirm();
    await vi.waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(toast.success).toHaveBeenCalledWith(`userManagement.${key}:{"name":"Ann"}`);
    expect(onRefresh).toHaveBeenCalledTimes(refreshes);
  });

  it('force logout reports how many sessions ended, defaulting to zero', async () => {
    admin.terminateUserSessions.mockResolvedValue({ sessions_terminated: 3 });
    const { actions, confirmDialog } = setup();
    actions.forceLogout(target);
    confirmDialog.confirm();
    await vi.waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        'userManagement.forceLogoutSuccess:{"name":"Ann","count":3}'
      )
    );
  });
});
