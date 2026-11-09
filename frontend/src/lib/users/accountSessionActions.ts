/**
 * The row actions that run through the admin client with the row's buttons disabled:
 * lock, unlock, force logout and MFA reset. Each asks for confirmation first and surfaces
 * the backend's `detail` verbatim on a failure.
 */
import { AdminApi } from '$lib/api/admin';
import { getErrorMessage } from '$lib/utils/apiError';
import { toastStore } from '$stores/toast';
import { nameOf, type AccountActionContext, type AccountRow } from './accountActionContext';

export interface AccountSessionActions {
  lockAccount(target: AccountRow): void;
  unlockAccount(target: AccountRow): void;
  forceLogout(target: AccountRow): void;
  resetUserMFA(target: AccountRow): void;
}

export function createAccountSessionActions(ctx: AccountActionContext): AccountSessionActions {
  const { confirmDialog, onRefresh, pendingUuid, tr } = ctx;

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
              count: result?.sessions_terminated ?? 0,
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

  return { lockAccount, unlockAccount, forceLogout, resetUserMFA };
}
