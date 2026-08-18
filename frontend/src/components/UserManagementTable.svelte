<script>
  import { onMount } from 'svelte';
  import axiosInstance from '../lib/axios';
  import { AdminApi } from '$lib/api/admin';
  import {
    AUTH_TYPES,
    INVITE_EXPIRY_DEFAULT_HOURS,
    INVITE_EXPIRY_MIN_HOURS,
    INVITE_EXPIRY_MAX_HOURS,
    createInvitation,
    listInvitations,
    revokeInvitation
  } from '$lib/api/invitations';
  import { user } from '../stores/auth';
  import { toastStore } from '../stores/toast';
  import ConfirmationModal from './ConfirmationModal.svelte';
  import LinkIdentityModal from './LinkIdentityModal.svelte';
  import { t } from '$stores/locale';
  import EmptyState from './ui/EmptyState.svelte';

  /**
   * @typedef {Object} User
   * @property {string} uuid
   * @property {string} email
   * @property {string} role
   * @property {string} created_at
   * @property {string|null} [last_login]
   * @property {boolean} [is_active]
   * @property {string} [full_name]
   * @property {string} [auth_type]
   * @property {boolean} [allow_local_fallback]
   * @property {boolean} [email_verified]
   * @property {string|null} [email_verified_at]
   * @property {'pending'|'approved'|'rejected'|string} [approval_status]
   * @property {string|null} [approved_at]
   * @property {string|null} [account_expires_at]
   */

  /** @type {Array<User>} */
  export let users = [];

  /** @type {boolean} */
  export let loading = false;

  /** @type {Function} */
  export let onRefresh = () => {};

  /** @type {Function} */
  export let onUserRecovery = () => {};

  /** @type {string} */
  let newUsername = '';

  /** @type {string} */
  let newEmail = '';

  /** @type {string} */
  let newPassword = '';

  /** @type {string} */
  let newRole = 'user';

  /**
   * Identity source for a directly-created account. Without this every
   * admin-created user was `local`, which on a deployment that has turned local
   * passwords off produces an account that can never sign in.
   * @type {import('$lib/api/invitations').AuthType}
   */
  let newAuthType = 'local';

  // Only a local account holds a password; the server rejects one on any other
  // auth_type rather than storing a credential it will never accept.
  $: createNeedsPassword = newAuthType === 'local';

  // ── invitations ───────────────────────────────────────────────────────────
  /** @type {boolean} */
  let showInviteForm = false;
  /** @type {string} */
  let inviteEmail = '';
  /** @type {string} */
  let inviteFullName = '';
  /** @type {string} */
  let inviteRole = 'user';
  /** @type {import('$lib/api/invitations').AuthType} */
  let inviteAuthType = 'local';
  /** @type {number} */
  let inviteExpiresInHours = INVITE_EXPIRY_DEFAULT_HOURS;
  /** @type {boolean} */
  let inviteSubmitting = false;
  /** @type {Array<import('$lib/api/invitations').Invitation>} */
  let invitations = [];
  /** @type {boolean} */
  let invitationsLoading = false;
  /** @type {string|null} */
  let pendingInviteUuid = null;

  // Confirmation modal state
  let showConfirmModal = false;
  let confirmModalTitle = '';
  let confirmModalMessage = '';
  /** @type {string} */
  let confirmModalConfirmText = '';
  /** @type {(() => void) | null} */
  let confirmCallback = null;
  /** @type {(() => void) | null} */
  let confirmCancelCallback = null;

  // Server-owned password policy (GET /auth/password-policy). Hardcoding a
  // client-side minimum drifts from the backend's — it said 8 while the policy
  // required 12, so every "valid" password bounced off the API.
  /** @type {number} */
  let passwordMinLength = 12;

  onMount(() => {
    (async () => {
      try {
        const { data } = await axiosInstance.get('/auth/password-policy');
        if (typeof data?.min_length === 'number' && data.min_length > 0) {
          passwordMinLength = data.min_length;
        }
      } catch (err) {
        console.warn('Could not load password policy; using default minimum', err);
      }
      await refreshInvitations();
    })();
  });

  // Reactive so a language switch re-renders the labels; a plain helper that
  // reads $t internally would not re-run on locale change.
  $: roleLabels = /** @type {Record<string, string>} */ ({
    user: $t('userManagement.roleUser'),
    admin: $t('userManagement.roleAdmin'),
    super_admin: $t('userManagement.roleSuperAdmin')
  });

  $: authTypeLabels = /** @type {Record<string, string>} */ ({
    local: $t('userManagement.authTypeLocal'),
    ldap: $t('userManagement.authTypeLdap'),
    oidc: $t('userManagement.authTypeOidc'),
    pki: $t('userManagement.authTypePki')
  });

  // The backend pre-computes `status` (pending|accepted|revoked|expired) — this
  // maps it to a label only. Never re-derive it from expires_at/used_at here.
  $: invitationStatusLabels = /** @type {Record<string, string>} */ ({
    pending: $t('userManagement.invitationStatusPending'),
    accepted: $t('userManagement.invitationStatusAccepted'),
    revoked: $t('userManagement.invitationStatusRevoked'),
    expired: $t('userManagement.invitationStatusExpired')
  });

  // Password reset modal state
  let showPasswordResetModal = false;
  /** @type {User|null} */
  let passwordResetUser = null;
  let resetPassword = '';
  let confirmResetPassword = '';
  let passwordResetLoading = false;
  let showResetPassword = false;
  let showConfirmResetPassword = false;

  /** @type {boolean} */
  let showAddUserForm = false;

  /** @type {string} */
  let searchTerm = '';

  /** @type {Array<User>} */
  let filteredUsers = [];

  /** @type {string|null} */
  let currentUserId = null;

  // Subscribe to the user store to get the current user UUID and role
  /** @type {boolean} */
  let isSuperAdmin = false;

  $: if ($user) {
    currentUserId = $user.uuid;
    isSuperAdmin = $user.role === 'super_admin';
  } else {
    currentUserId = null;
    isSuperAdmin = false;
  }

  // Reactively update filtered users when users prop or search term changes
  $: {
    if (!searchTerm.trim()) {
      filteredUsers = [...users];
    } else {
      const term = searchTerm.toLowerCase();
      filteredUsers = users.filter(user =>
        (user.full_name && user.full_name.toLowerCase().includes(term)) ||
        (user.email && user.email.toLowerCase().includes(term))
      );
    }
  }

  /**
   * Pull the backend's `detail` out of an axios error.
   *
   * `err.message` is the axios string ("Request failed with status code 403"),
   * which hides the reason the API actually gave.
   * @param {any} err
   * @param {string} fallback
   * @returns {string}
   */
  function extractErrorMessage(err, fallback) {
    const detail = err?.response?.data?.detail;
    if (!detail) return fallback;
    if (Array.isArray(detail)) {
      return detail.map(d => d.msg || d).join('; ');
    }
    return String(detail);
  }

  /**
   * Create a new user
   */
  async function createUser() {
    if (!newUsername || !newEmail || (createNeedsPassword && !newPassword)) {
      toastStore.error($t('userManagement.fillAllFields'));
      return;
    }

    // A super_admin can change auth configuration and other users' roles —
    // confirm before minting one.
    if (newRole === 'super_admin') {
      showConfirmation(
        $t('userManagement.confirmSuperAdminTitle'),
        $t('userManagement.confirmSuperAdminCreate', { name: newUsername || newEmail }),
        () => executeCreateUser(),
        $t('userManagement.confirmSuperAdminButton')
      );
      return;
    }

    await executeCreateUser();
  }

  /**
   * Create the user after any confirmation step
   */
  async function executeCreateUser() {
    try {
      // AdminApi.createUser omits `password` entirely for an external auth_type
      // (the backend 422s on the combination) — don't inline the POST again.
      await AdminApi.createUser({
        email: newEmail,
        full_name: newUsername, // Required field
        role: newRole,
        auth_type: newAuthType,
        password: newPassword,
        is_active: true
      });

      // Capture name for toast before resetting
      const createdUserName = newUsername || newEmail;

      // Add new user to the list and reset form
      newUsername = '';
      newEmail = '';
      newPassword = '';
      newRole = 'user';
      newAuthType = 'local';
      showAddUserForm = false;

      toastStore.success($t('userManagement.userCreatedSuccess', { name: createdUserName }));

      // Refresh the user list
      onRefresh();
    } catch (err) {
      console.error('Error creating user:', err);
      toastStore.error(extractErrorMessage(err, $t('userManagement.createUserFailed')));
    }
  }

  // ── invitations ───────────────────────────────────────────────────────────

  /**
   * Load the pending invitation list.
   *
   * Each row carries a server-computed `status`; render it, never re-derive it
   * from `expires_at` / `used_at` (fat backend, thin frontend).
   */
  async function refreshInvitations() {
    invitationsLoading = true;
    try {
      invitations = await listInvitations(false);
    } catch (err) {
      // A plain admin still sees the rest of the panel; the endpoint is admin-
      // gated exactly like the user list, so a failure here is not fatal.
      console.warn('Could not load invitations', err);
      invitations = [];
    } finally {
      invitationsLoading = false;
    }
  }

  /**
   * Invite someone to create their own account.
   *
   * Preferred over {@link createUser}: the admin never sees or chooses the
   * invitee's password, the invitee gets an email, and `auth_type` is carried
   * through so an IdP-fronted deployment can pre-provision matching accounts.
   */
  async function sendInvitation() {
    if (!inviteEmail.trim()) {
      toastStore.error($t('userManagement.fillAllFields'));
      return;
    }

    if (inviteRole === 'super_admin') {
      showConfirmation(
        $t('userManagement.confirmSuperAdminTitle'),
        $t('userManagement.confirmSuperAdminInvite', { name: inviteFullName || inviteEmail }),
        () => executeSendInvitation(),
        $t('userManagement.confirmSuperAdminButton')
      );
      return;
    }

    await executeSendInvitation();
  }

  async function executeSendInvitation() {
    inviteSubmitting = true;
    const invitedAddress = inviteEmail.trim();
    try {
      await createInvitation({
        email: invitedAddress,
        full_name: inviteFullName.trim() || undefined,
        role: inviteRole,
        auth_type: inviteAuthType,
        expires_in_hours: inviteExpiresInHours
      });

      inviteEmail = '';
      inviteFullName = '';
      inviteRole = 'user';
      inviteAuthType = 'local';
      inviteExpiresInHours = INVITE_EXPIRY_DEFAULT_HOURS;
      showInviteForm = false;

      toastStore.success($t('userManagement.inviteSent', { email: invitedAddress }));
      await refreshInvitations();
    } catch (err) {
      console.error('Error creating invitation:', err);
      toastStore.error(extractErrorMessage(err, $t('userManagement.inviteFailed')));
    } finally {
      inviteSubmitting = false;
    }
  }

  /**
   * Revoke a pending invitation.
   * @param {import('$lib/api/invitations').Invitation} invitation
   */
  function revokeInvite(invitation) {
    showConfirmation(
      $t('userManagement.revokeInvitation'),
      $t('userManagement.revokeInvitationConfirm', { email: invitation.email }),
      () => executeRevokeInvite(invitation),
      $t('userManagement.revokeInvitation')
    );
  }

  /**
   * @param {import('$lib/api/invitations').Invitation} invitation
   */
  async function executeRevokeInvite(invitation) {
    pendingInviteUuid = invitation.uuid;
    try {
      await revokeInvitation(invitation.uuid);
      toastStore.success($t('userManagement.inviteRevoked', { email: invitation.email }));
      await refreshInvitations();
    } catch (err) {
      console.error('Error revoking invitation:', err);
      toastStore.error(extractErrorMessage(err, $t('userManagement.revokeInviteFailed')));
    } finally {
      pendingInviteUuid = null;
    }
  }

  /**
   * Toggle the invite form, resetting it on open.
   */
  function toggleInviteForm() {
    showInviteForm = !showInviteForm;
    if (showInviteForm) {
      showAddUserForm = false;
      inviteEmail = '';
      inviteFullName = '';
      inviteRole = 'user';
      inviteAuthType = 'local';
      inviteExpiresInHours = INVITE_EXPIRY_DEFAULT_HOURS;
    }
  }

  /**
   * Show confirmation modal
   * @param {string} title - The modal title
   * @param {string} message - The confirmation message
   * @param {() => void} callback - The callback to execute on confirmation
   * @param {string} [confirmText] - Label for the confirm button
   * @param {(() => void) | null} [onCancel] - Callback to run if the user backs out
   */
  function showConfirmation(title, message, callback, confirmText = '', onCancel = null) {
    confirmModalTitle = title;
    confirmModalMessage = message;
    confirmModalConfirmText = confirmText;
    confirmCallback = callback;
    confirmCancelCallback = onCancel;
    showConfirmModal = true;
  }

  /**
   * Handle confirmation modal confirm
   */
  function handleConfirmModalConfirm() {
    if (confirmCallback) {
      confirmCallback();
      confirmCallback = null;
    }
    confirmCancelCallback = null;
    showConfirmModal = false;
  }

  /**
   * Handle confirmation modal cancel
   */
  function handleConfirmModalCancel() {
    if (confirmCancelCallback) {
      confirmCancelCallback();
      confirmCancelCallback = null;
    }
    confirmCallback = null;
    showConfirmModal = false;
  }

  /**
   * Delete a user
   * @param {string} userId
   */
  async function deleteUser(userId) {
    showConfirmation(
      $t('userManagement.deleteUser'),
      $t('userManagement.deleteUserConfirm'),
      () => executeDeleteUser(userId)
    );
  }

  /**
   * Execute user deletion after confirmation
   * @param {string} userId
   */
  async function executeDeleteUser(userId) {
    try {
      await axiosInstance.delete(`/users/${userId}`);
      toastStore.success($t('userManagement.userDeletedSuccess'));

      // Refresh user list
      onRefresh();
    } catch (err) {
      console.error('Error deleting user:', err);
      toastStore.error(extractErrorMessage(err, $t('userManagement.deleteUserFailed')));
    }
  }

  /**
   * Update a user's role.
   *
   * Goes through the admin client (PUT /admin/users/{uuid}/role), not the
   * generic user update — that route is the one that writes an audit record,
   * revokes the target's sessions, and refuses to demote the last super_admin.
   * @param {string} userId
   * @param {string} role
   */
  async function updateUserRole(userId, role) {
    try {
      await AdminApi.changeUserRole(userId, role);
      toastStore.success($t('userManagement.userRoleUpdated', { role: roleLabels[role] || role }));

      // Refresh user list
      onRefresh();
    } catch (err) {
      console.error('Error updating user role:', err);
      toastStore.error(extractErrorMessage(err, $t('userManagement.updateRoleFailed')));

      // Unlike the sibling handlers below, this one refreshes on error too:
      // the role <select> is one-way bound (`value={currentUser.role}`, not
      // `bind:value`), so a rejected change leaves the DOM select showing the
      // rejected role with nothing re-deriving it from server state until the
      // list is refetched and re-rendered.
      onRefresh();
    }
  }

  /**
   * Handle role change event
   * @param {string} userId
   * @param {Event} e
   */
  function handleUserRoleChange(userId, e) {
    const select = /** @type {HTMLSelectElement} */ (e.currentTarget);
    if (!select) return;

    const newRole = select.value;
    const target = users.find(u => u.uuid === userId);
    const previousRole = target?.role ?? 'user';
    if (newRole === previousRole) return;

    // Promoting to super_admin hands over auth-configuration and role-change
    // power — make it deliberate, and put the select back if they back out.
    if (newRole === 'super_admin') {
      showConfirmation(
        $t('userManagement.confirmSuperAdminTitle'),
        $t('userManagement.confirmSuperAdminPromote', {
          name: target?.full_name || target?.email || ''
        }),
        () => updateUserRole(userId, newRole),
        $t('userManagement.confirmSuperAdminButton'),
        () => { select.value = previousRole; }
      );
      return;
    }

    updateUserRole(userId, newRole);
  }

  /**
   * Open password reset modal for a user
   * @param {User} userToReset
   */
  function openPasswordResetModal(userToReset) {
    passwordResetUser = userToReset;
    resetPassword = '';
    confirmResetPassword = '';
    showResetPassword = false;
    showConfirmResetPassword = false;
    showPasswordResetModal = true;
  }

  /**
   * Close password reset modal
   */
  function closePasswordResetModal() {
    showPasswordResetModal = false;
    passwordResetUser = null;
    resetPassword = '';
    confirmResetPassword = '';
    showResetPassword = false;
    showConfirmResetPassword = false;
  }

  /**
   * Reset user password
   */
  async function executePasswordReset() {
    if (!passwordResetUser) return;

    // Validation
    if (!resetPassword || !confirmResetPassword) {
      toastStore.error($t('userManagement.fillBothPasswordFields'));
      return;
    }

    if (resetPassword !== confirmResetPassword) {
      toastStore.error($t('userManagement.passwordsDoNotMatch'));
      return;
    }

    if (resetPassword.length < passwordMinLength) {
      toastStore.error($t('userManagement.passwordMinLength', { min: passwordMinLength }));
      return;
    }

    passwordResetLoading = true;

    try {
      await axiosInstance.put(`/users/${passwordResetUser.uuid}`, {
        password: resetPassword
      });

      const userName = passwordResetUser.full_name || passwordResetUser.email;
      toastStore.success($t('userManagement.passwordResetSuccess', { userName }));
      closePasswordResetModal();
    } catch (err) {
      console.error('Error resetting password:', err);
      toastStore.error(extractErrorMessage(err, $t('userManagement.passwordResetFailed')));
    } finally {
      passwordResetLoading = false;
    }
  }

  /**
   * Toggle allow_local_fallback for a user (super_admin only)
   * @param {User} targetUser
   */
  async function toggleLocalFallback(targetUser) {
    const newValue = !targetUser.allow_local_fallback;
    try {
      await axiosInstance.put(`/users/${targetUser.uuid}`, {
        allow_local_fallback: newValue
      });
      const userName = targetUser.full_name || targetUser.email;
      if (newValue) {
        toastStore.success($t('userManagement.localFallbackEnabled', { name: userName }));
      } else {
        toastStore.success($t('userManagement.localFallbackDisabled', { name: userName }));
      }
      onRefresh();
    } catch (err) {
      console.error('Error toggling local fallback:', err);
      toastStore.error(extractErrorMessage(err, $t('userManagement.localFallbackFailed')));
    }
  }

  /**
   * UUID of the row with an account action in flight, so its buttons can be
   * disabled without freezing the whole table.
   * @type {string|null}
   */
  let pendingActionUuid = null;

  /**
   * UUID of the row whose expiration-date editor is open, or null.
   * @type {string|null}
   */
  let expirationEditUuid = null;

  /** Value bound to the open row's `<input type="date">`. @type {string} */
  let expirationEditValue = '';

  /**
   * Open the inline expiration editor for a row (FedRAMP AC-2 time-boxed
   * accounts — `account_expires_at` was enforced on every request with no way
   * to ever set it; this is that write path).
   * @param {User} targetUser
   */
  function openExpirationEditor(targetUser) {
    expirationEditUuid = targetUser.uuid;
    expirationEditValue = targetUser.account_expires_at
      ? targetUser.account_expires_at.slice(0, 10)
      : '';
  }

  function closeExpirationEditor() {
    expirationEditUuid = null;
    expirationEditValue = '';
  }

  /**
   * Save (or clear, if the date field was emptied) the row's account_expires_at.
   * @param {User} targetUser
   */
  async function saveExpiration(targetUser) {
    const isoValue = expirationEditValue ? `${expirationEditValue}T23:59:59` : null;
    pendingActionUuid = targetUser.uuid;
    try {
      await axiosInstance.put(`/users/${targetUser.uuid}`, {
        account_expires_at: isoValue
      });
      const userName = targetUser.full_name || targetUser.email;
      toastStore.success(
        isoValue
          ? $t('userManagement.expirationSet', { name: userName })
          : $t('userManagement.expirationCleared', { name: userName })
      );
      closeExpirationEditor();
      onRefresh();
    } catch (err) {
      console.error('Error setting account expiration:', err);
      toastStore.error(extractErrorMessage(err, $t('userManagement.expirationFailed')));
    } finally {
      pendingActionUuid = null;
    }
  }

  /** Target for {@link LinkIdentityModal} (P1.3 — the account_linking operator remedy). */
  let linkIdentityModalOpen = false;
  /** @type {User|null} */
  let linkIdentityTarget = null;

  /** @param {User} targetUser */
  function openLinkIdentityModal(targetUser) {
    linkIdentityTarget = targetUser;
    linkIdentityModalOpen = true;
  }

  function closeLinkIdentityModal() {
    linkIdentityModalOpen = false;
  }

  function handleIdentityLinked() {
    onRefresh();
  }

  /**
   * Run an admin account action for one row, with the row's buttons disabled for
   * the duration and the backend's `detail` surfaced verbatim on failure.
   * @param {User} targetUser
   * @param {() => Promise<unknown>} action
   * @param {(result: any, name: string) => string} successMessage
   * @param {string} failureMessage
   * @param {boolean} [refresh] - Refresh the user list afterwards
   */
  async function runAccountAction(targetUser, action, successMessage, failureMessage, refresh = false) {
    pendingActionUuid = targetUser.uuid;
    const userName = targetUser.full_name || targetUser.email;
    try {
      const result = await action();
      toastStore.success(successMessage(result, userName));
      if (refresh) onRefresh();
    } catch (err) {
      console.error('Account action failed:', err);
      toastStore.error(extractErrorMessage(err, failureMessage));
    } finally {
      pendingActionUuid = null;
    }
  }

  /**
   * Lock an account: deactivates it AND revokes every session it holds.
   *
   * The reason is written verbatim into the audit record, so it stays a stable
   * English string rather than the admin's current UI language.
   * @param {User} targetUser
   */
  function lockAccount(targetUser) {
    showConfirmation(
      $t('userManagement.lockAccount'),
      $t('userManagement.lockConfirmMessage', { name: targetUser.full_name || targetUser.email }),
      () => runAccountAction(
        targetUser,
        () => AdminApi.lockAccount(targetUser.uuid, 'Locked by admin from user management'),
        (_result, name) => $t('userManagement.lockSuccess', { name }),
        $t('userManagement.lockFailed'),
        true
      ),
      $t('userManagement.lockAccount')
    );
  }

  /**
   * Clear a failed-login lockout.
   *
   * This is NOT the inverse of {@link lockAccount}: the endpoint resets the
   * lockout counter only and leaves `is_active` alone, so `was_locked === false`
   * is reported as "nothing to clear" rather than as a successful unlock.
   * @param {User} targetUser
   */
  function unlockAccount(targetUser) {
    showConfirmation(
      $t('userManagement.unlockAccount'),
      $t('userManagement.unlockConfirmMessage', { name: targetUser.full_name || targetUser.email }),
      () => runAccountAction(
        targetUser,
        () => AdminApi.unlockAccount(targetUser.uuid),
        (result, name) => result?.was_locked
          ? $t('userManagement.unlockSuccess', { name })
          : $t('userManagement.unlockNotLocked', { name }),
        $t('userManagement.unlockFailed')
      ),
      $t('userManagement.unlockAccount')
    );
  }

  /**
   * Force logout: revoke every refresh token the target holds.
   * @param {User} targetUser
   */
  function forceLogout(targetUser) {
    showConfirmation(
      $t('userManagement.forceLogout'),
      $t('userManagement.forceLogoutConfirmMessage', { name: targetUser.full_name || targetUser.email }),
      () => runAccountAction(
        targetUser,
        () => AdminApi.terminateUserSessions(targetUser.uuid),
        (result, name) => $t('userManagement.forceLogoutSuccess', {
          name,
          sessions: result?.sessions_terminated ?? 0
        }),
        $t('userManagement.forceLogoutFailed')
      ),
      $t('userManagement.forceLogout')
    );
  }

  /**
   * Reset the target's MFA enrolment (super_admin only, enforced server-side).
   * @param {User} targetUser
   */
  function resetUserMFA(targetUser) {
    showConfirmation(
      $t('userManagement.resetMfa'),
      $t('userManagement.resetMfaConfirmMessage', { name: targetUser.full_name || targetUser.email }),
      () => runAccountAction(
        targetUser,
        () => AdminApi.resetUserMFA(targetUser.uuid),
        (_result, name) => $t('userManagement.resetMfaSuccess', { name }),
        $t('userManagement.resetMfaFailed')
      ),
      $t('userManagement.resetMfa')
    );
  }

  /**
   * Process search input
   * @param {Event} e
   */
  function handleSearchInput(e) {
    if (e.target && 'value' in e.target) {
      searchTerm = /** @type {HTMLInputElement} */ (e.target).value;
      // Reactive statement handles filtering automatically when searchTerm changes
    }
  }

  /**
   * Format date to locale string
   * @param {string} dateString
   * @returns {string}
   */
  function formatDate(dateString) {
    if (!dateString) return $t('userManagement.notAvailable');

    const date = new Date(dateString);
    return date.toLocaleString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
  }

  /**
   * Toggle add user form
   */
  function toggleAddUserForm() {
    showAddUserForm = !showAddUserForm;
    // Reset form when toggling
    if (showAddUserForm) {
      showInviteForm = false;
      newUsername = '';
      newEmail = '';
      newPassword = '';
      newRole = 'user';
      newAuthType = 'local';
    }
  }

</script>

<div class="user-management">
  <div class="table-controls">
    <div class="search-container">
      <input
        type="text"
        placeholder={$t('userManagement.searchPlaceholder')}
        on:input={handleSearchInput}
        value={searchTerm}
        title={$t('userManagement.searchTitle')}
      />
    </div>

    <button
      on:click={toggleInviteForm}
      class={showInviteForm ? 'btn-cancel' : 'add-button'}
      title={showInviteForm ? $t('userManagement.cancelInvite') : $t('userManagement.inviteUserTitle')}
    >
      {showInviteForm ? $t('common.cancel') : $t('userManagement.inviteUser')}
    </button>

    <button
      on:click={toggleAddUserForm}
      class={showAddUserForm ? 'btn-cancel' : 'add-button'}
      title={showAddUserForm ? $t('userManagement.cancelAddUser') : $t('userManagement.createNewUser')}
    >
      {showAddUserForm ? $t('common.cancel') : $t('userManagement.addUser')}
    </button>
  </div>

  {#if showInviteForm}
    <div class="add-user-form">
      <h3>{$t('userManagement.inviteUser')}</h3>
      <p class="form-hint">{$t('userManagement.inviteDescription')}</p>

      <div class="form-group">
        <label for="invite-email">{$t('userManagement.email')}</label>
        <input
          type="email"
          id="invite-email"
          bind:value={inviteEmail}
          placeholder={$t('userManagement.email')}
          disabled={inviteSubmitting}
          required
        />
      </div>

      <div class="form-group">
        <label for="invite-full-name">{$t('userManagement.fullName')}</label>
        <input
          type="text"
          id="invite-full-name"
          bind:value={inviteFullName}
          placeholder={$t('userManagement.fullName')}
          disabled={inviteSubmitting}
        />
      </div>

      <div class="form-group">
        <label for="invite-role">{$t('userManagement.role')}</label>
        <select id="invite-role" bind:value={inviteRole} disabled={inviteSubmitting}>
          <option value="user">{$t('userManagement.roleUser')}</option>
          {#if isSuperAdmin}
            <!-- An invitation is a deferred account creation, so it inherits the
                 same gate as POST /admin/users: only a super_admin may mint
                 elevated accounts (the backend 403s otherwise). -->
            <option value="admin">{$t('userManagement.roleAdmin')}</option>
            <option value="super_admin">{$t('userManagement.roleSuperAdmin')}</option>
          {/if}
        </select>
        {#if isSuperAdmin && inviteRole === 'super_admin'}
          <span class="role-warning">{$t('userManagement.superAdminWarning')}</span>
        {/if}
      </div>

      <div class="form-group">
        <label for="invite-auth-type">{$t('userManagement.authType')}</label>
        <select id="invite-auth-type" bind:value={inviteAuthType} disabled={inviteSubmitting}>
          {#each AUTH_TYPES as authType (authType)}
            <option value={authType}>{authTypeLabels[authType] || authType}</option>
          {/each}
        </select>
        <span class="form-hint">{$t('userManagement.authTypeHint')}</span>
      </div>

      <div class="form-group">
        <label for="invite-expiry">{$t('userManagement.inviteExpiry')}</label>
        <input
          type="number"
          id="invite-expiry"
          bind:value={inviteExpiresInHours}
          min={INVITE_EXPIRY_MIN_HOURS}
          max={INVITE_EXPIRY_MAX_HOURS}
          disabled={inviteSubmitting}
        />
        <span class="form-hint">
          {$t('userManagement.inviteExpiryHint', {
            min: INVITE_EXPIRY_MIN_HOURS,
            max: INVITE_EXPIRY_MAX_HOURS
          })}
        </span>
      </div>

      <button
        on:click={sendInvitation}
        class="create-button"
        disabled={inviteSubmitting}
        title={$t('userManagement.inviteUserTitle')}
      >
        {inviteSubmitting ? $t('userManagement.inviteSending') : $t('userManagement.sendInvite')}
      </button>
    </div>
  {/if}

  {#if showAddUserForm}
    <div class="add-user-form">
      <h3>{$t('userManagement.addNewUser')}</h3>
      <div class="form-group">
        <label for="username">{$t('userManagement.fullName')}</label>
        <input
          type="text"
          id="username"
          bind:value={newUsername}
          placeholder={$t('userManagement.fullName')}
          required
        />
      </div>

      <div class="form-group">
        <label for="email">{$t('userManagement.email')}</label>
        <input
          type="email"
          id="email"
          bind:value={newEmail}
          placeholder={$t('userManagement.email')}
          required
        />
      </div>

      <div class="form-group">
        <label for="auth-type">{$t('userManagement.authType')}</label>
        <select id="auth-type" bind:value={newAuthType}>
          {#each AUTH_TYPES as authType (authType)}
            <option value={authType}>{authTypeLabels[authType] || authType}</option>
          {/each}
        </select>
        <span class="form-hint">{$t('userManagement.authTypeHint')}</span>
      </div>

      {#if createNeedsPassword}
        <!-- Only a local account holds a password. Sending one for ldap/oidc/
             pki is a 422, not a silent no-op — the field is hidden rather than
             disabled so nothing stale is submitted. -->
        <div class="form-group">
          <label for="password">{$t('userManagement.password')}</label>
          <input
            type="password"
            id="password"
            bind:value={newPassword}
            placeholder={$t('userManagement.password')}
            minlength={passwordMinLength}
            required
          />
          <span class="form-hint">{$t('userManagement.minimumCharacters', { min: passwordMinLength })}</span>
        </div>
      {:else}
        <p class="form-hint external-note">{$t('userManagement.externalAccountNote')}</p>
      {/if}

      <div class="form-group">
        <label for="role">{$t('userManagement.role')}</label>
        <select id="role" bind:value={newRole}>
          <option value="user">{$t('userManagement.roleUser')}</option>
          {#if isSuperAdmin}
            <!-- Only a super_admin may mint elevated accounts (backend enforces this) -->
            <option value="admin">{$t('userManagement.roleAdmin')}</option>
            <option value="super_admin">{$t('userManagement.roleSuperAdmin')}</option>
          {/if}
        </select>
        {#if isSuperAdmin && newRole === 'super_admin'}
          <span class="role-warning">{$t('userManagement.superAdminWarning')}</span>
        {/if}
      </div>

      <button
        on:click={createUser}
        class="create-button"
        title={$t('userManagement.createUserTitle')}
      >{$t('userManagement.createUser')}</button>
    </div>
  {/if}

  {#if loading}
    <div class="loading-state">
      <p>{$t('userManagement.loadingUsers')}</p>
    </div>
  {:else if !users || users.length === 0}
    <EmptyState title={$t('userManagement.noUsersFound')} padding="2rem" />
  {:else}
    <div class="table-scroll-wrapper">
    <table class="users-table user-management-table">
      <thead>
        <tr>
          <th>{$t('userManagement.name')}</th>
          <th>{$t('userManagement.email')}</th>
          <th>{$t('userManagement.role')}</th>
          <th>{$t('userManagement.created')}</th>
          <th>{$t('userManagement.actions')}</th>
        </tr>
      </thead>
      <tbody>
        {#each filteredUsers as currentUser (currentUser.uuid)}
          <tr>
            <td>
              {currentUser.full_name || $t('userManagement.notAvailable')}
              {#if currentUser.is_active === false}
                <span class="status-badge inactive">{$t('userManagement.inactiveBadge')}</span>
              {/if}
              <!-- Admission state (v379). `approved` is the overwhelming majority
                   and the backend's default, so only the two held states are
                   badged — chipping every row "Approved" would be noise. -->
              {#if currentUser.approval_status === 'pending'}
                <span class="status-badge status-pending">{$t('userManagement.pendingBadge')}</span>
              {:else if currentUser.approval_status === 'rejected'}
                <span class="status-badge status-revoked">{$t('userManagement.rejectedBadge')}</span>
              {/if}
            </td>
            <td>
              {currentUser.email}
              <!-- Only meaningful for local accounts: an external identity's
                   address is asserted by its IdP (ExternalIdentity.email_verified,
                   a separate flag), so `email_verified` is false there by default
                   and badging it would libel every LDAP/OIDC user. -->
              {#if currentUser.auth_type === 'local' && currentUser.email_verified === false}
                <span class="status-badge unverified">{$t('userManagement.unverifiedBadge')}</span>
              {/if}
            </td>
            <td>
              <!-- Role changes require super_admin server-side
                   (PUT /admin/users/{uuid}/role). A plain admin used to get a
                   select they could not act on: picking "Administrator" just
                   returned 403. A super_admin row also had no matching option,
                   so it rendered blank and any accidental change silently
                   demoted the account. Both cases now render as static text. -->
              {#if currentUser.uuid !== currentUserId && isSuperAdmin}
                <select
                  value={currentUser.role}
                  on:change={(e) => handleUserRoleChange(currentUser.uuid, e)}
                  title={$t('userManagement.changeRoleFor', { name: currentUser.full_name || currentUser.email })}
                >
                  <option value="user">{$t('userManagement.roleUser')}</option>
                  <option value="admin">{$t('userManagement.roleAdmin')}</option>
                  <option value="super_admin">{$t('userManagement.roleSuperAdmin')}</option>
                </select>
              {:else}
                <span class="current-role">{roleLabels[currentUser.role] || currentUser.role}</span>
              {/if}
            </td>
            <td>{formatDate(currentUser.created_at)}</td>
            <td>
              <div class="table-actions">
                {#if currentUser.uuid !== currentUserId}
                  {#if isSuperAdmin && currentUser.auth_type && currentUser.auth_type !== 'local' && currentUser.auth_type !== 'ldap'}
                  <button
                    class="icon-button fallback-toggle-button"
                    class:active={currentUser.allow_local_fallback}
                    on:click={() => toggleLocalFallback(currentUser)}
                    title={currentUser.allow_local_fallback
                      ? $t('userManagement.disableLocalFallback', { name: currentUser.full_name || currentUser.email })
                      : $t('userManagement.enableLocalFallback', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>
                      <polyline points="10 17 15 12 10 7"/>
                      <line x1="15" y1="12" x2="3" y2="12"/>
                    </svg>
                  </button>
                  {/if}
                  {#if currentUser.auth_type === 'local' || currentUser.allow_local_fallback}
                  <button
                    class="icon-button reset-password-button"
                    on:click={() => openPasswordResetModal(currentUser)}
                    title={$t('userManagement.resetPasswordFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
                      <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                    </svg>
                  </button>
                  {/if}
                  {#if currentUser.is_active !== false}
                  <button
                    class="icon-button lock-button"
                    disabled={pendingActionUuid === currentUser.uuid}
                    on:click={() => lockAccount(currentUser)}
                    title={$t('userManagement.lockAccountFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
                      <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                      <line x1="12" x2="12" y1="15" y2="18"/>
                    </svg>
                  </button>
                  {/if}
                  <!-- Clears a failed-login lockout. Deliberately NOT paired with
                       the lock button: the endpoint resets the lockout counter and
                       does not re-activate a deactivated account. -->
                  <button
                    class="icon-button unlock-button"
                    disabled={pendingActionUuid === currentUser.uuid}
                    on:click={() => unlockAccount(currentUser)}
                    title={$t('userManagement.unlockAccountFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
                      <path d="M7 11V7a5 5 0 0 1 9.9-1"/>
                    </svg>
                  </button>
                  <button
                    class="icon-button force-logout-button"
                    disabled={pendingActionUuid === currentUser.uuid}
                    on:click={() => forceLogout(currentUser)}
                    title={$t('userManagement.forceLogoutFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
                      <polyline points="16 17 21 12 16 7"/>
                      <line x1="21" y1="12" x2="9" y2="12"/>
                    </svg>
                  </button>
                  {#if isSuperAdmin}
                  <!-- MFA reset requires super_admin server-side; a plain admin
                       would only get a 403 from the button. -->
                  <button
                    class="icon-button mfa-reset-button"
                    disabled={pendingActionUuid === currentUser.uuid}
                    on:click={() => resetUserMFA(currentUser)}
                    title={$t('userManagement.resetMfaFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M20 13c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V6l8-3 8 3Z"/>
                      <line x1="9.5" y1="9.5" x2="14.5" y2="14.5"/>
                      <line x1="14.5" y1="9.5" x2="9.5" y2="14.5"/>
                    </svg>
                  </button>
                  {/if}
                  {#if isSuperAdmin && currentUser.role !== 'super_admin'}
                  <!-- The account_linking operator remedy (P1.3): sets the
                       provider's own identifier so a login that a source
                       like Authentik (hardcodes email_verified: false)
                       cannot link by email matches directly instead. Hidden
                       entirely for a super_admin target — the backend
                       refuses it unconditionally, that account is
                       local-only by design. -->
                  <button
                    class="icon-button link-identity-button"
                    disabled={pendingActionUuid === currentUser.uuid}
                    on:click={() => openLinkIdentityModal(currentUser)}
                    title={$t('userManagement.linkIdentityFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
                      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
                    </svg>
                  </button>
                  {/if}
                  {#if expirationEditUuid === currentUser.uuid}
                    <span class="expiration-editor">
                      <input
                        type="date"
                        bind:value={expirationEditValue}
                        aria-label={$t('userManagement.setExpirationFor', { name: currentUser.full_name || currentUser.email })}
                      />
                      <button
                        class="icon-button expiration-save-button"
                        disabled={pendingActionUuid === currentUser.uuid}
                        on:click={() => saveExpiration(currentUser)}
                        title={$t('common.save')}
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <polyline points="20 6 9 17 4 12"/>
                        </svg>
                      </button>
                      <button
                        class="icon-button expiration-cancel-button"
                        disabled={pendingActionUuid === currentUser.uuid}
                        on:click={closeExpirationEditor}
                        title={$t('common.cancel')}
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <line x1="18" x2="6" y1="6" y2="18"/>
                          <line x1="6" x2="18" y1="6" y2="18"/>
                        </svg>
                      </button>
                    </span>
                  {:else}
                  <button
                    class="icon-button expiration-button"
                    class:active={currentUser.account_expires_at}
                    on:click={() => openExpirationEditor(currentUser)}
                    title={currentUser.account_expires_at
                      ? $t('userManagement.editExpirationFor', { name: currentUser.full_name || currentUser.email, date: formatDate(currentUser.account_expires_at) })
                      : $t('userManagement.setExpirationFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <rect width="18" height="18" x="3" y="4" rx="2" ry="2"/>
                      <line x1="16" x2="16" y1="2" y2="6"/>
                      <line x1="8" x2="8" y1="2" y2="6"/>
                      <line x1="3" x2="21" y1="10" y2="10"/>
                    </svg>
                  </button>
                  {/if}
                  <button
                    class="icon-button recover-button"
                    on:click={() => onUserRecovery(currentUser.uuid)}
                    title={$t('userManagement.recoverFilesFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
                      <path d="M3 3v5h5"/>
                      <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/>
                      <path d="M16 16h5v5"/>
                    </svg>
                  </button>
                  <button
                    class="icon-button delete-button"
                    on:click={() => deleteUser(currentUser.uuid)}
                    title={$t('userManagement.deleteAccount', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M3 6h18"/>
                      <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>
                      <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>
                      <line x1="10" x2="10" y1="11" y2="17"/>
                      <line x1="14" x2="14" y1="11" y2="17"/>
                    </svg>
                  </button>
                {:else}
                  <span class="self-user">{$t('userManagement.currentUser')}</span>
                {/if}
              </div>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    </div>
  {/if}

  <!-- Pending invitations. `status` is computed server-side; this renders it. -->
  <section class="invitations-section">
    <h3>{$t('userManagement.pendingInvitations')}</h3>
    {#if invitationsLoading}
      <div class="loading-state"><p>{$t('userManagement.loadingInvitations')}</p></div>
    {:else if invitations.length === 0}
      <EmptyState title={$t('userManagement.noPendingInvitations')} padding="1.5rem" />
    {:else}
      <div class="table-scroll-wrapper">
        <table class="users-table">
          <thead>
            <tr>
              <th>{$t('userManagement.email')}</th>
              <th>{$t('userManagement.role')}</th>
              <th>{$t('userManagement.authType')}</th>
              <th>{$t('userManagement.invitationStatus')}</th>
              <th>{$t('userManagement.inviteExpires')}</th>
              <th>{$t('userManagement.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {#each invitations as invitation (invitation.uuid)}
              <tr>
                <td>
                  {invitation.email}
                  {#if invitation.full_name}
                    <span class="invite-name">{invitation.full_name}</span>
                  {/if}
                </td>
                <td>{roleLabels[invitation.role] || invitation.role}</td>
                <td>{authTypeLabels[invitation.auth_type] || invitation.auth_type}</td>
                <td>
                  <span class="status-badge status-{invitation.status}">
                    {invitationStatusLabels[invitation.status] || invitation.status}
                  </span>
                </td>
                <td>{formatDate(invitation.expires_at)}</td>
                <td>
                  <div class="table-actions">
                    <button
                      class="icon-button delete-button"
                      disabled={pendingInviteUuid === invitation.uuid}
                      on:click={() => revokeInvite(invitation)}
                      title={$t('userManagement.revokeInvitationFor', { email: invitation.email })}
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="10" />
                        <line x1="4.9" y1="4.9" x2="19.1" y2="19.1" />
                      </svg>
                    </button>
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>
</div>

<!-- Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showConfirmModal}
  title={confirmModalTitle}
  message={confirmModalMessage}
  confirmText={confirmModalConfirmText || $t('common.delete')}
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={handleConfirmModalConfirm}
  on:cancel={handleConfirmModalCancel}
  on:close={handleConfirmModalCancel}
/>

<!-- Link External Identity Modal (P1.3) -->
<LinkIdentityModal
  bind:isOpen={linkIdentityModalOpen}
  targetUser={linkIdentityTarget}
  on:linked={handleIdentityLinked}
  on:close={closeLinkIdentityModal}
/>

<!-- Password Reset Modal -->
{#if showPasswordResetModal && passwordResetUser}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="password-reset-modal-backdrop" on:click={closePasswordResetModal} on:wheel|preventDefault|self on:touchmove|preventDefault|self role="presentation" on:keydown={(e) => e.key === 'Escape' && closePasswordResetModal()}>
    <div class="password-reset-modal" on:click|stopPropagation role="dialog" aria-modal="true" aria-labelledby="password-reset-title" tabindex="0">
      <button class="modal-close-btn" on:click={closePasswordResetModal} aria-label={$t('common.close')} title={$t('common.close')}>
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>

      <h3 id="password-reset-title" class="modal-title">{$t('userManagement.resetPassword')}</h3>
      <p class="modal-description">
        {$t('userManagement.setNewPasswordFor')} <strong>{passwordResetUser.full_name || passwordResetUser.email}</strong>
      </p>

      <form on:submit|preventDefault={executePasswordReset} class="password-reset-form">
        <div class="form-group">
          <div class="password-header">
            <label for="reset-password">{$t('userManagement.newPassword')}</label>
            <button
              type="button"
              class="toggle-password"
              on:click={() => showResetPassword = !showResetPassword}
              tabindex="-1"
              aria-label={showResetPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            >
              {#if showResetPassword}
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
                  <circle cx="12" cy="12" r="3"/>
                </svg>
              {:else}
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="m15 18-.722-3.25"/>
                  <path d="m2 2 20 20"/>
                  <path d="m9 9-.637 3.181"/>
                  <path d="M12.5 5.5c2.13.13 4.16 1.11 5.5 3.5-.274.526-.568 1.016-.891 1.469"/>
                  <path d="M2 12s3-7 10-7c1.284 0 2.499.23 3.62.67"/>
                  <path d="m18.147 8.476.853 3.524"/>
                </svg>
              {/if}
            </button>
          </div>
          <input
            type={showResetPassword ? 'text' : 'password'}
            id="reset-password"
            class="form-control"
            bind:value={resetPassword}
            placeholder={$t('userManagement.enterNewPassword')}
            required
            minlength={passwordMinLength}
          />
          <small class="form-text">{$t('userManagement.minimumCharacters', { min: passwordMinLength })}</small>
        </div>

        <div class="form-group">
          <div class="password-header">
            <label for="confirm-reset-password">{$t('userManagement.confirmPassword')}</label>
            <button
              type="button"
              class="toggle-password"
              on:click={() => showConfirmResetPassword = !showConfirmResetPassword}
              tabindex="-1"
              aria-label={showConfirmResetPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            >
              {#if showConfirmResetPassword}
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
                  <circle cx="12" cy="12" r="3"/>
                </svg>
              {:else}
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="m15 18-.722-3.25"/>
                  <path d="m2 2 20 20"/>
                  <path d="m9 9-.637 3.181"/>
                  <path d="M12.5 5.5c2.13.13 4.16 1.11 5.5 3.5-.274.526-.568 1.016-.891 1.469"/>
                  <path d="M2 12s3-7 10-7c1.284 0 2.499.23 3.62.67"/>
                  <path d="m18.147 8.476.853 3.524"/>
                </svg>
              {/if}
            </button>
          </div>
          <input
            type={showConfirmResetPassword ? 'text' : 'password'}
            id="confirm-reset-password"
            class="form-control"
            bind:value={confirmResetPassword}
            placeholder={$t('userManagement.confirmNewPassword')}
            required
            minlength={passwordMinLength}
          />
        </div>

        <div class="modal-actions">
          <button
            type="button"
            class="btn-cancel"
            on:click={closePasswordResetModal}
            disabled={passwordResetLoading}
          >
            {$t('common.cancel')}
          </button>
          <button
            type="submit"
            class="btn-confirm"
            disabled={passwordResetLoading || !resetPassword || !confirmResetPassword}
          >
            {passwordResetLoading ? $t('userManagement.resetting') : $t('userManagement.resetPasswordButton')}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .user-management {
    width: 100%;
    margin-bottom: 2rem;
  }

  .table-controls {
    display: flex;
    justify-content: space-between;
    margin-bottom: 1rem;
  }

  .search-container {
    flex: 1;
    margin-right: 1rem;
  }

  .search-container input {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-size: 0.8125rem;
  }

  .add-button {
    background-color: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .add-button:hover:not(:disabled),
  .add-button:focus:not(:disabled) {
    background-color: #2563eb;
    color: white;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
    text-decoration: none;
  }

  .add-button:active:not(:disabled) {
    transform: scale(1);
  }

  .add-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }

  .add-user-form {
    background-color: var(--card-background);
    padding: 1rem;
    border-radius: 4px;
    margin-bottom: 1rem;
    border: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .add-user-form h3 {
    font-size: 1.125rem;
    margin-bottom: 1rem;
  }

  .form-group {
    margin-bottom: 0.5rem;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.25rem;
    font-size: 0.8125rem;
  }

  .form-group input, .form-group select {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-size: 0.8125rem;
  }

  .role-warning {
    display: block;
    margin-top: 0.375rem;
    padding: 0.5rem 0.625rem;
    border: 1px solid rgba(245, 158, 11, 0.4);
    border-radius: 6px;
    background-color: rgba(245, 158, 11, 0.12);
    color: var(--text-color);
    font-size: 0.75rem;
    line-height: 1.4;
  }

  .create-button {
    background-color: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    margin-top: 0.5rem;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .create-button:hover:not(:disabled),
  .create-button:focus:not(:disabled) {
    background-color: #2563eb;
    color: white;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
    text-decoration: none;
  }

  .create-button:active:not(:disabled) {
    transform: scale(1);
  }

  .create-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }

  .users-table {
    width: 100%;
    border-collapse: collapse;
    background-color: var(--card-background);
    border-radius: 6px;
    overflow: hidden;
    box-shadow: var(--card-shadow);
  }

  .users-table th, .users-table td {
    padding: 0.75rem;
    border-bottom: 1px solid var(--border-color);
    text-align: left;
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .users-table th {
    background-color: var(--table-header-bg);
    font-weight: bold;
  }

  .users-table tr:hover {
    background-color: var(--table-row-hover);
  }

  .table-actions {
    display: flex;
    gap: 0.375rem;
    align-items: center;
  }

  /* Base icon button styles */
  .icon-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    padding: 0;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .icon-button svg {
    flex-shrink: 0;
  }

  .icon-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  /* Delete button - red */
  .delete-button {
    background-color: rgba(239, 68, 68, 0.1);
    color: #ef4444;
  }

  .delete-button:hover:not(:disabled) {
    background-color: #ef4444;
    color: white;
    transform: scale(1.05);
  }

  .delete-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  /* Recover button - green */
  .recover-button {
    background-color: rgba(16, 185, 129, 0.1);
    color: #10b981;
  }

  .recover-button:hover:not(:disabled) {
    background-color: #10b981;
    color: white;
    transform: scale(1.05);
  }

  .recover-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  /* Fallback toggle button - amber/orange */
  .fallback-toggle-button {
    background-color: rgba(245, 158, 11, 0.1);
    color: #f59e0b;
  }

  .fallback-toggle-button:hover:not(:disabled) {
    background-color: #f59e0b;
    color: white;
    transform: scale(1.05);
  }

  .fallback-toggle-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  .fallback-toggle-button.active {
    background-color: #f59e0b;
    color: white;
  }

  .fallback-toggle-button.active:hover:not(:disabled) {
    background-color: #d97706;
  }

  /* Expiration button - blue */
  .expiration-button {
    background-color: rgba(59, 130, 246, 0.1);
    color: #3b82f6;
  }

  .expiration-button:hover:not(:disabled) {
    background-color: #3b82f6;
    color: white;
    transform: scale(1.05);
  }

  .expiration-button.active {
    background-color: #3b82f6;
    color: white;
  }

  .expiration-button.active:hover:not(:disabled) {
    background-color: #2563eb;
  }

  .expiration-editor {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .expiration-editor input[type='date'] {
    height: 32px;
    padding: 0 6px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 6px;
    background-color: var(--bg-secondary, white);
    color: var(--text-primary, #111827);
    font-size: 0.85rem;
  }

  .expiration-save-button {
    background-color: rgba(16, 185, 129, 0.1);
    color: #10b981;
  }

  .expiration-save-button:hover:not(:disabled) {
    background-color: #10b981;
    color: white;
  }

  .expiration-cancel-button {
    background-color: rgba(107, 114, 128, 0.1);
    color: #6b7280;
  }

  .expiration-cancel-button:hover:not(:disabled) {
    background-color: #6b7280;
    color: white;
  }

  /* Reset password button - purple/indigo */
  .reset-password-button {
    background-color: rgba(99, 102, 241, 0.1);
    color: #6366f1;
  }

  .reset-password-button:hover:not(:disabled) {
    background-color: #6366f1;
    color: white;
    transform: scale(1.05);
  }

  .reset-password-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  /* Lock account - orange */
  .lock-button {
    background-color: rgba(249, 115, 22, 0.1);
    color: #f97316;
  }

  .lock-button:hover:not(:disabled) {
    background-color: #f97316;
    color: white;
    transform: scale(1.05);
  }

  .lock-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  /* Clear login lockout - sky */
  .unlock-button {
    background-color: rgba(14, 165, 233, 0.1);
    color: #0ea5e9;
  }

  .unlock-button:hover:not(:disabled) {
    background-color: #0ea5e9;
    color: white;
    transform: scale(1.05);
  }

  .unlock-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  /* Force logout - slate */
  .force-logout-button {
    background-color: rgba(100, 116, 139, 0.15);
    color: #475569;
  }

  .force-logout-button:hover:not(:disabled) {
    background-color: #475569;
    color: white;
    transform: scale(1.05);
  }

  .force-logout-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  :global([data-theme='dark']) .force-logout-button {
    color: #cbd5e1;
  }

  :global([data-theme='dark']) .force-logout-button:hover:not(:disabled) {
    background-color: #64748b;
    color: white;
  }

  /* MFA reset - violet */
  .mfa-reset-button {
    background-color: rgba(168, 85, 247, 0.1);
    color: #a855f7;
  }

  .mfa-reset-button:hover:not(:disabled) {
    background-color: #a855f7;
    color: white;
    transform: scale(1.05);
  }

  .mfa-reset-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  /* Link external identity - teal */
  .link-identity-button {
    background-color: rgba(20, 184, 166, 0.1);
    color: #14b8a6;
  }

  .link-identity-button:hover:not(:disabled) {
    background-color: #14b8a6;
    color: white;
    transform: scale(1.05);
  }

  .link-identity-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  .status-badge {
    display: inline-block;
    margin-left: 0.375rem;
    padding: 0.0625rem 0.375rem;
    border-radius: 999px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    vertical-align: middle;
  }

  .status-badge.inactive,
  .status-badge.status-revoked {
    background-color: rgba(239, 68, 68, 0.15);
    color: rgb(220, 38, 38);
  }

  :global([data-theme='dark']) .status-badge.inactive,
  :global([data-theme='dark']) .status-badge.status-revoked {
    background-color: rgba(239, 68, 68, 0.2);
    color: rgb(248, 113, 113);
  }

  .status-badge.unverified,
  .status-badge.status-expired {
    background-color: rgba(245, 158, 11, 0.18);
    color: rgb(180, 83, 9);
  }

  :global([data-theme='dark']) .status-badge.unverified,
  :global([data-theme='dark']) .status-badge.status-expired {
    background-color: rgba(245, 158, 11, 0.22);
    color: rgb(252, 211, 77);
  }

  .status-badge.status-pending {
    background-color: rgba(14, 165, 233, 0.15);
    color: rgb(2, 132, 199);
  }

  :global([data-theme='dark']) .status-badge.status-pending {
    background-color: rgba(14, 165, 233, 0.22);
    color: rgb(125, 211, 252);
  }

  .status-badge.status-accepted {
    background-color: rgba(16, 185, 129, 0.15);
    color: rgb(4, 120, 87);
  }

  :global([data-theme='dark']) .status-badge.status-accepted {
    background-color: rgba(16, 185, 129, 0.22);
    color: rgb(110, 231, 183);
  }

  .form-hint {
    display: block;
    margin-top: 0.25rem;
    color: var(--text-secondary);
    font-size: 0.75rem;
    line-height: 1.4;
  }

  .external-note {
    margin: 0 0 0.5rem;
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-color);
  }

  .invitations-section {
    margin-top: 2rem;
  }

  .invitations-section h3 {
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-color);
    margin-bottom: 0.75rem;
  }

  .invite-name {
    display: block;
    color: var(--text-secondary);
    font-size: 0.75rem;
  }

  .current-role {
    font-weight: bold;
    text-transform: capitalize;
  }

  .self-user {
    font-style: italic;
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  .loading-state {
    padding: 2rem;
    text-align: center;
    background-color: var(--card-background);
    border-radius: 4px;
    margin-top: 1rem;
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  /* Password Reset Modal */
  .password-reset-modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: var(--modal-backdrop, rgba(0, 0, 0, 0.5));
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1300;
    animation: fadeIn 0.2s ease-out;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .password-reset-modal {
    position: relative;
    width: 90%;
    max-width: 420px;
    background-color: var(--surface-color, #fff);
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    padding: 1.5rem;
    animation: slideUp 0.3s ease-out;
  }

  .modal-close-btn {
    position: absolute;
    top: 0.75rem;
    right: 0.75rem;
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.5rem;
    color: var(--text-secondary);
    transition: color 0.2s ease;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .modal-close-btn:hover {
    color: var(--text-color);
    background: var(--button-hover, var(--background-color));
  }

  .modal-title {
    font-size: 1.125rem;
    font-weight: 600;
    margin: 0 0 0.5rem 0;
    color: var(--text-color);
    padding-right: 2rem;
  }

  .modal-description {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin: 0 0 1.25rem 0;
  }

  .modal-description strong {
    color: var(--text-color);
  }

  .password-reset-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .password-reset-form .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .password-reset-form label {
    font-weight: 500;
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .password-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .toggle-password {
    background: none;
    border: none;
    cursor: pointer;
    padding: 4px;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    border-radius: 4px;
    transition: background-color 0.2s;
  }

  .toggle-password:hover {
    background-color: var(--background-color);
    color: var(--text-color);
  }

  .password-reset-form .form-control {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .password-reset-form .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(var(--primary-color-rgb), 0.1);
  }

  .password-reset-form .form-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-top: 0.125rem;
  }

  .modal-actions {
    display: flex;
    gap: 0.75rem;
    justify-content: flex-end;
    margin-top: 0.5rem;
  }

  .btn-cancel {
    background-color: var(--card-background);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: var(--card-shadow);
  }

  .btn-cancel:hover:not(:disabled) {
    background-color: var(--button-hover);
    border-color: var(--border-color);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
  }

  .btn-cancel:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-cancel:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-confirm {
    background-color: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-confirm:hover:not(:disabled) {
    background-color: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-confirm:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-confirm:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .table-scroll-wrapper {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  @media (max-width: 768px) {
    .table-controls {
      flex-direction: column;
      gap: 0.5rem;
    }

    .search-container {
      margin-right: 0;
    }

    .search-container input {
      min-height: 44px;
      font-size: 1rem;
    }

    .add-button,
    .btn-cancel {
      width: 100%;
      min-height: 44px;
      text-align: center;
    }

    .add-user-form input,
    .add-user-form select {
      min-height: 44px;
      font-size: 1rem;
    }

    .create-button {
      width: 100%;
      min-height: 44px;
    }

    .users-table {
      min-width: 600px;
    }

    .users-table th,
    .users-table td {
      white-space: nowrap;
      padding: 0.5rem;
      font-size: 0.75rem;
    }

    .icon-button {
      width: 36px;
      height: 36px;
    }

    .modal-actions {
      flex-direction: column-reverse;
    }

    .modal-actions .btn-cancel,
    .modal-actions .btn-confirm {
      width: 100%;
      min-height: 44px;
      text-align: center;
    }
  }
</style>
