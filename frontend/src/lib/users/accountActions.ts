/**
 * The user-management table's per-row account mutations: role change, delete, the
 * local-fallback flag, account expiry, lock, unlock, force logout and MFA reset.
 *
 * Every action reports through a toast, surfaces the backend's `detail` verbatim on a
 * failure, and asks the table to refetch only when the row it rendered is now wrong.
 * Which actions a row OFFERS is decided separately, by `rowPermissions.ts`; the server
 * stays the authority for both.
 *
 *     const accountActions = createAccountActions(confirmDialog, () => onRefresh());
 *     const { pendingUuid } = accountActions;   // disable that row's buttons
 */
import { get, writable, type Writable } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { AdminApi } from '$lib/api/admin';
import type { ConfirmDialog } from '$lib/confirmDialog';
import { getErrorMessage } from '$lib/utils/apiError';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';
import { ROLE_SUPER_ADMIN } from './rowPermissions';
import { toEndOfDayTimestamp } from './accountExpiry';
import {
  nameOf,
  type AccountActionContext,
  type AccountRow,
  type Translate,
} from './accountActionContext';
import { createAccountSessionActions, type AccountSessionActions } from './accountSessionActions';

const ROLE_LABEL_KEYS: Record<string, string> = {
  user: 'userManagement.roleUser',
  admin: 'userManagement.roleAdmin',
  super_admin: 'userManagement.roleSuperAdmin',
};

export interface AccountActions extends AccountSessionActions {
  /** UUID of the row with an account action in flight, or null. */
  pendingUuid: Writable<string | null>;
  requestRoleChange(
    userId: string,
    target: AccountRow | undefined,
    newRole: string,
    revert: () => void
  ): void;
  deleteUser(userId: string): void;
  toggleLocalFallback(target: AccountRow): Promise<void>;
  saveExpiration(target: AccountRow, dateValue: string, onSaved: () => void): Promise<void>;
}

export function createAccountActions(
  confirmDialog: ConfirmDialog,
  onRefresh: () => void
): AccountActions {
  const pendingUuid = writable<string | null>(null);
  const tr: Translate = (key, vars) => get(t)(key, vars);
  const ctx: AccountActionContext = { confirmDialog, onRefresh, pendingUuid, tr };

  /**
   * Update a user's role.
   *
   * Goes through the admin client (PUT /admin/users/{uuid}/role), not the
   * generic user update — that route is the one that writes an audit record,
   * revokes the target's sessions, and refuses to demote the last super_admin.
   */
  async function updateUserRole(userId: string, role: string) {
    try {
      await AdminApi.changeUserRole(userId, role);
      const labelKey = ROLE_LABEL_KEYS[role];
      toastStore.success(
        tr('userManagement.userRoleUpdated', { role: (labelKey && tr(labelKey)) || role })
      );

      onRefresh();
    } catch (err) {
      console.error('Error updating user role:', err);
      toastStore.error(getErrorMessage(err, tr('userManagement.updateRoleFailed')));

      // Unlike the sibling handlers below, this one refreshes on error too:
      // the role <select> is one-way bound (`value={currentUser.role}`, not
      // `bind:value`), so a rejected change leaves the DOM select showing the
      // rejected role with nothing re-deriving it from server state until the
      // list is refetched and re-rendered.
      onRefresh();
    }
  }

  function requestRoleChange(
    userId: string,
    target: AccountRow | undefined,
    newRole: string,
    revert: () => void
  ) {
    const previousRole = target?.role ?? 'user';
    if (newRole === previousRole) return;

    // Promoting to super_admin hands over auth-configuration and role-change
    // power — make it deliberate, and put the select back if they back out.
    if (newRole === ROLE_SUPER_ADMIN) {
      confirmDialog.request(
        tr('userManagement.confirmSuperAdminTitle'),
        tr('userManagement.confirmSuperAdminPromote', { name: nameOf(target) }),
        () => updateUserRole(userId, newRole),
        tr('userManagement.confirmSuperAdminButton'),
        revert
      );
      return;
    }

    void updateUserRole(userId, newRole);
  }

  async function executeDeleteUser(userId: string) {
    try {
      await axiosInstance.delete(`/users/${userId}`);
      toastStore.success(tr('userManagement.userDeletedSuccess'));
      onRefresh();
    } catch (err) {
      console.error('Error deleting user:', err);
      toastStore.error(getErrorMessage(err, tr('userManagement.deleteUserFailed')));
    }
  }

  function deleteUser(userId: string) {
    confirmDialog.request(
      tr('userManagement.deleteUser'),
      tr('userManagement.deleteUserConfirm'),
      () => executeDeleteUser(userId)
    );
  }

  /** Toggle allow_local_fallback for a user (super_admin only). */
  async function toggleLocalFallback(target: AccountRow) {
    const newValue = !target.allow_local_fallback;
    try {
      await axiosInstance.put(`/users/${target.uuid}`, {
        allow_local_fallback: newValue,
      });
      const name = nameOf(target);
      if (newValue) {
        toastStore.success(tr('userManagement.localFallbackEnabled', { name }));
      } else {
        toastStore.success(tr('userManagement.localFallbackDisabled', { name }));
      }
      onRefresh();
    } catch (err) {
      console.error('Error toggling local fallback:', err);
      toastStore.error(getErrorMessage(err, tr('userManagement.localFallbackFailed')));
    }
  }

  /**
   * Save (or clear, if the date field was emptied) the row's account_expires_at
   * (FedRAMP AC-2 time-boxed accounts). `onSaved` runs only on success.
   */
  async function saveExpiration(target: AccountRow, dateValue: string, onSaved: () => void) {
    const isoValue = toEndOfDayTimestamp(dateValue);
    pendingUuid.set(target.uuid);
    try {
      await axiosInstance.put(`/users/${target.uuid}`, {
        account_expires_at: isoValue,
      });
      const name = nameOf(target);
      toastStore.success(
        isoValue
          ? tr('userManagement.expirationSet', { name })
          : tr('userManagement.expirationCleared', { name })
      );
      onSaved();
      onRefresh();
    } catch (err) {
      console.error('Error setting account expiration:', err);
      toastStore.error(getErrorMessage(err, tr('userManagement.expirationFailed')));
    } finally {
      pendingUuid.set(null);
    }
  }

  return {
    pendingUuid,
    requestRoleChange,
    deleteUser,
    toggleLocalFallback,
    saveExpiration,
    ...createAccountSessionActions(ctx),
  };
}
