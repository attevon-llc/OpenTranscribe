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

export interface AccountRow {
  uuid: string;
  email: string;
  full_name?: string;
  role: string;
  allow_local_fallback?: boolean;
}

const ROLE_LABEL_KEYS: Record<string, string> = {
  user: 'userManagement.roleUser',
  admin: 'userManagement.roleAdmin',
  super_admin: 'userManagement.roleSuperAdmin',
};

type Translate = (key: string, vars?: Record<string, unknown>) => string;

export interface AccountActions {
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
  lockAccount(target: AccountRow): void;
  unlockAccount(target: AccountRow): void;
  forceLogout(target: AccountRow): void;
  resetUserMFA(target: AccountRow): void;
}

export function createAccountActions(
  confirmDialog: ConfirmDialog,
  onRefresh: () => void
): AccountActions {
  const pendingUuid = writable<string | null>(null);
  const tr: Translate = (key, vars) => get(t)(key, vars);
  const nameOf = (user: AccountRow | undefined) => user?.full_name || user?.email || '';

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

  /**
   * Run an admin account action for one row, with the row's buttons disabled for
   * the duration and the backend's `detail` surfaced verbatim on failure.
   *
   * `refresh` may be a function of the action's result — used by
   * {@link unlockAccount}, whose refresh need depends on whether the call also
   * reactivated a disabled account (issue #570 §A.1.1).
   */
  async function runAccountAction<R>(
    target: AccountRow,
    action: () => Promise<R>,
    successMessage: (result: R, name: string) => string,
    failureMessage: string,
    refresh: boolean | ((result: R) => boolean) = false
  ) {
    pendingUuid.set(target.uuid);
    const name = nameOf(target);
    try {
      const result = await action();
      toastStore.success(successMessage(result, name));
      const shouldRefresh = typeof refresh === 'function' ? refresh(result) : refresh;
      if (shouldRefresh) onRefresh();
    } catch (err) {
      console.error('Account action failed:', err);
      toastStore.error(getErrorMessage(err, failureMessage));
    } finally {
      pendingUuid.set(null);
    }
  }

  /**
   * Lock an account: deactivates it AND revokes every session it holds.
   *
   * The reason is written verbatim into the audit record, so it stays a stable
   * English string rather than the admin's current UI language.
   */
  function lockAccount(target: AccountRow) {
    confirmDialog.request(
      tr('userManagement.lockAccount'),
      tr('userManagement.lockConfirmMessage', { name: nameOf(target) }),
      () =>
        runAccountAction(
          target,
          () => AdminApi.lockAccount(target.uuid, 'Locked by admin from user management'),
          (_result, name) => tr('userManagement.lockSuccess', { name }),
          tr('userManagement.lockFailed'),
          true
        ),
      tr('userManagement.lockAccount')
    );
  }

  /**
   * Clear a failed-login lockout — this IS the inverse of {@link lockAccount}
   * (issue #570 §A.1.1): the endpoint clears both the lockout counter AND
   * reactivates an account deactivated by `lockAccount`, reporting which
   * happened via `was_locked`/`was_disabled`. `was_locked === false` alone is
   * reported as "nothing to clear" rather than a successful unlock. The list
   * is refreshed only when `was_disabled` is true — a reactivated account's row
   * changes (it stops rendering as inactive); a merely lockout-locked one has
   * nothing in the row to update.
   */
  function unlockAccount(target: AccountRow) {
    confirmDialog.request(
      tr('userManagement.unlockAccount'),
      tr('userManagement.unlockConfirmMessage', { name: nameOf(target) }),
      () =>
        runAccountAction(
          target,
          () => AdminApi.unlockAccount(target.uuid),
          (result, name) =>
            result?.was_locked
              ? tr('userManagement.unlockSuccess', { name })
              : tr('userManagement.unlockNotLocked', { name }),
          tr('userManagement.unlockFailed'),
          (result) => Boolean(result?.was_disabled)
        ),
      tr('userManagement.unlockAccount')
    );
  }

  /** Force logout: revoke every refresh token the target holds. */
  function forceLogout(target: AccountRow) {
    confirmDialog.request(
      tr('userManagement.forceLogout'),
      tr('userManagement.forceLogoutConfirmMessage', { name: nameOf(target) }),
      () =>
        runAccountAction(
          target,
          () => AdminApi.terminateUserSessions(target.uuid),
          (result, name) =>
            tr('userManagement.forceLogoutSuccess', {
              name,
              sessions: result?.sessions_terminated ?? 0,
            }),
          tr('userManagement.forceLogoutFailed')
        ),
      tr('userManagement.forceLogout')
    );
  }

  /** Reset the target's MFA enrolment (super_admin only, enforced server-side). */
  function resetUserMFA(target: AccountRow) {
    confirmDialog.request(
      tr('userManagement.resetMfa'),
      tr('userManagement.resetMfaConfirmMessage', { name: nameOf(target) }),
      () =>
        runAccountAction(
          target,
          () => AdminApi.resetUserMFA(target.uuid),
          (_result, name) => tr('userManagement.resetMfaSuccess', { name }),
          tr('userManagement.resetMfaFailed')
        ),
      tr('userManagement.resetMfa')
    );
  }

  return {
    pendingUuid,
    requestRoleChange,
    deleteUser,
    toggleLocalFallback,
    saveExpiration,
    lockAccount,
    unlockAccount,
    forceLogout,
    resetUserMFA,
  };
}
