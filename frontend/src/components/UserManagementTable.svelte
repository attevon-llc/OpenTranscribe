<script>
  import { formatDateTime } from '$lib/utils/formatting';
  import { onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { AdminApi } from '$lib/api/admin';
  import {
    AUTH_TYPES,
    INVITE_EXPIRY_MIN_HOURS,
    INVITE_EXPIRY_MAX_HOURS
  } from '$lib/api/invitations';
  import { user } from '$stores/auth';
  import { toastStore } from '$stores/toast';
  import ConfirmationModal from './ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import LinkIdentityModal from './LinkIdentityModal.svelte';
  import PasswordResetModal from '$components/users/PasswordResetModal.svelte';
  import { t, locale } from '$stores/locale';
  import EmptyState from './ui/EmptyState.svelte';
  import { getErrorMessage } from '$lib/utils/apiError';
  import AuthMailNotice from './ui/AuthMailNotice.svelte';
  import {
    canAssignElevatedRoles,
    isSuperAdmin as viewerIsSuperAdmin,
    permittedRowActions
  } from '$lib/users/rowPermissions';
  import { filterUsers } from '$lib/users/userSearch';
  import { toDateInputValue } from '$lib/users/accountExpiry';
  import { createAccountActions } from '$lib/users/accountActions';
  import { createInvitationsController } from '$lib/users/invitationsController';
  import Badge from '$components/ui/Badge.svelte';
  import { badgeVariantForStatus } from '$lib/fileStatus/statusVariant';

  /**
   * @typedef {Object} User
   * @property {string} uuid
   * @property {string} email
   * @property {string} role
   * @property {string} created_at
   * @property {string|null} [last_login]
   * @property {boolean} [is_active]
   * @property {string} [full_name]
   * @property {string} [auth_type] - local, ldap, oidc, pki, ...
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

  /** @type {boolean} */
  let showInviteForm = false;

  // Confirmation modal state
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
  } = confirmDialog;

  const accountActions = createAccountActions(confirmDialog, () => onRefresh());
  /** UUID of the row with an account action in flight; its buttons are disabled. */
  const { pendingUuid: pendingActionUuid } = accountActions;

  const invites = createInvitationsController(confirmDialog);
  const {
    form: inviteForm,
    invitations,
    loading: invitationsLoading,
    submitting: inviteSubmitting,
    revokingUuid: pendingInviteUuid
  } = invites;

  // Server-owned password policy (GET /auth/password-policy). Hardcoding a
  // client-side minimum drifts from the backend's — it said 8 while the policy
  // required 12, so every "valid" password bounced off the API.
  /** @type {number} */
  let passwordMinLength = 12;

  onMount(() => {
    void (async () => {
      try {
        const { data } = await axiosInstance.get('/auth/password-policy');
        if (typeof data?.min_length === 'number' && data.min_length > 0) {
          passwordMinLength = data.min_length;
        }
      } catch (err) {
        console.warn('Could not load password policy; using default minimum', err);
      }
      await invites.refresh();
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

  /** The account whose password-reset dialog is open, or null. @type {User|null} */
  let passwordResetUser = null;

  /** @type {boolean} */
  let showAddUserForm = false;

  /** @type {string} */
  let searchTerm = '';

  $: currentUserId = $user ? $user.uuid : null;

  $: isSuperAdmin = viewerIsSuperAdmin($user);

  // Minting an admin or super_admin account, directly or by invitation, is
  // super_admin-only server-side.
  $: canElevate = canAssignElevatedRoles($user);

  /** @type {Array<User>} */
  $: filteredUsers = filterUsers(users, searchTerm);

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
      confirmDialog.request(
        $t('userManagement.confirmSuperAdminTitle'),
        $t('userManagement.confirmSuperAdminCreate', { name: newUsername || newEmail }),
        () => executeCreateUser(),
        { confirmText: $t('userManagement.confirmSuperAdminButton') }
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
      toastStore.error(getErrorMessage(err, $t('userManagement.createUserFailed')));
    }
  }

  function sendInvitation() {
    return invites.send(() => {
      showInviteForm = false;
    });
  }

  /**
   * Toggle the invite form, resetting it on open.
   */
  function toggleInviteForm() {
    showInviteForm = !showInviteForm;
    if (showInviteForm) {
      showAddUserForm = false;
      invites.resetForm();
    }
  }

  /**
   * Hand a role `<select>` change to the account actions, putting the select back
   * if a confirmation is backed out of.
   * @param {string} userId
   * @param {Event} e
   */
  function handleUserRoleChange(userId, e) {
    const select = /** @type {HTMLSelectElement} */ (e.currentTarget);
    if (!select) return;

    const target = users.find(u => u.uuid === userId);
    const previousRole = target?.role ?? 'user';
    accountActions.requestRoleChange(userId, target, select.value, () => {
      select.value = previousRole;
    });
  }

  /**
   * Open password reset modal for a user
   * @param {User} userToReset
   */
  function openPasswordResetModal(userToReset) {
    passwordResetUser = userToReset;
  }

  function closePasswordResetModal() {
    passwordResetUser = null;
  }

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
    expirationEditValue = toDateInputValue(targetUser.account_expires_at);
  }

  function closeExpirationEditor() {
    expirationEditUuid = null;
    expirationEditValue = '';
  }

  /**
   * Save (or clear, if the date field was emptied) the row's account_expires_at.
   * @param {User} targetUser
   */
  function saveExpiration(targetUser) {
    return accountActions.saveExpiration(targetUser, expirationEditValue, closeExpirationEditor);
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

    <div class="table-actions">
      <button
        type="button"
        on:click={toggleInviteForm}
        class="btn"
        class:btn-primary={!showInviteForm}
        class:btn-secondary={showInviteForm}
        title={showInviteForm ? $t('userManagement.cancelInvite') : $t('userManagement.inviteUserTitle')}
      >
        {showInviteForm ? $t('common.cancel') : $t('userManagement.inviteUser')}
      </button>

      <button
        type="button"
        on:click={toggleAddUserForm}
        class="btn btn-secondary"
        title={showAddUserForm ? $t('userManagement.cancelAddUser') : $t('userManagement.createNewUser')}
      >
        {showAddUserForm ? $t('common.cancel') : $t('userManagement.addUser')}
      </button>
    </div>
  </div>

  {#if showInviteForm}
    <div class="add-user-form">
      <h3>{$t('userManagement.inviteUser')}</h3>
      <p class="form-hint">{$t('userManagement.inviteDescription')}</p>
      <!-- The readiness read is super_admin-only like the designation it comes from, so a
           plain admin's request would only be refused; they get the 503 message instead. -->
      {#if isSuperAdmin}
        <AuthMailNotice context="invite" />
      {/if}

      <div class="form-group">
        <label for="invite-email">{$t('userManagement.email')}</label>
        <input
          type="email"
          id="invite-email"
          bind:value={$inviteForm.email}
          placeholder={$t('userManagement.email')}
          disabled={$inviteSubmitting}
          required
        />
      </div>

      <div class="form-group">
        <label for="invite-full-name">{$t('userManagement.fullName')}</label>
        <input
          type="text"
          id="invite-full-name"
          bind:value={$inviteForm.full_name}
          placeholder={$t('userManagement.fullName')}
          disabled={$inviteSubmitting}
        />
      </div>

      <div class="form-group">
        <label for="invite-role">{$t('userManagement.role')}</label>
        <select id="invite-role" bind:value={$inviteForm.role} disabled={$inviteSubmitting}>
          <option value="user">{$t('userManagement.roleUser')}</option>
          {#if canElevate}
            <!-- An invitation is a deferred account creation, so it inherits the
                 same gate as POST /admin/users: only a super_admin may mint
                 elevated accounts (the backend 403s otherwise). -->
            <option value="admin">{$t('userManagement.roleAdmin')}</option>
            <option value="super_admin">{$t('userManagement.roleSuperAdmin')}</option>
          {/if}
        </select>
        {#if canElevate && $inviteForm.role === 'super_admin'}
          <span class="role-warning">{$t('userManagement.superAdminWarning')}</span>
        {/if}
      </div>

      <div class="form-group">
        <label for="invite-auth-type">{$t('userManagement.authType')}</label>
        <select id="invite-auth-type" bind:value={$inviteForm.auth_type} disabled={$inviteSubmitting}>
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
          bind:value={$inviteForm.expires_in_hours}
          min={INVITE_EXPIRY_MIN_HOURS}
          max={INVITE_EXPIRY_MAX_HOURS}
          disabled={$inviteSubmitting}
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
        disabled={$inviteSubmitting}
        title={$t('userManagement.inviteUserTitle')}
      >
        {$inviteSubmitting ? $t('userManagement.inviteSending') : $t('userManagement.sendInvite')}
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
          {#if canElevate}
            <!-- Only a super_admin may mint elevated accounts (backend enforces this) -->
            <option value="admin">{$t('userManagement.roleAdmin')}</option>
            <option value="super_admin">{$t('userManagement.roleSuperAdmin')}</option>
          {/if}
        </select>
        {#if canElevate && newRole === 'super_admin'}
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
          <!-- What this viewer may do on this row: $lib/users/rowPermissions. -->
          {@const can = permittedRowActions($user, currentUser)}
          <tr>
            <td>
              {currentUser.full_name || $t('userManagement.notAvailable')}
              {#if currentUser.is_active === false}
                <Badge variant="error" class="status-badge">{$t('userManagement.inactiveBadge')}</Badge>
              {/if}
              <!-- Admission state (v381). `approved` is the overwhelming majority
                   and the backend's default, so only the two held states are
                   badged — chipping every row "Approved" would be noise. -->
              {#if currentUser.approval_status === 'pending'}
                <Badge variant="info" class="status-badge">{$t('userManagement.pendingBadge')}</Badge>
              {:else if currentUser.approval_status === 'rejected'}
                <Badge variant="error" class="status-badge">{$t('userManagement.rejectedBadge')}</Badge>
              {/if}
            </td>
            <td>
              {currentUser.email}
              <!-- Only meaningful for local accounts: an external identity's
                   address is asserted by its IdP (ExternalIdentity.email_verified,
                   a separate flag), so `email_verified` is false there by default
                   and badging it would libel every LDAP/OIDC user. -->
              {#if currentUser.auth_type === 'local' && currentUser.email_verified === false}
                <Badge variant="warning" class="status-badge">{$t('userManagement.unverifiedBadge')}</Badge>
              {/if}
            </td>
            <td>
              <!-- Role changes require super_admin server-side
                   (PUT /admin/users/{uuid}/role). A plain admin used to get a
                   select they could not act on: picking "Administrator" just
                   returned 403. A super_admin row also had no matching option,
                   so it rendered blank and any accidental change silently
                   demoted the account. Both cases now render as static text. -->
              {#if can.changeRole}
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
            <td>{formatDateTime(currentUser.created_at, $t('userManagement.notAvailable'), $locale)}</td>
            <td>
              <div class="table-actions">
                {#if currentUser.uuid !== currentUserId}
                  {#if can.toggleLocalFallback}
                  <button
                    class="icon-button fallback-toggle-button"
                    class:active={currentUser.allow_local_fallback}
                    on:click={() => accountActions.toggleLocalFallback(currentUser)}
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
                  {#if can.resetPassword}
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
                  {#if can.lock}
                  <button
                    class="icon-button lock-button"
                    disabled={$pendingActionUuid === currentUser.uuid}
                    on:click={() => accountActions.lockAccount(currentUser)}
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
                    disabled={$pendingActionUuid === currentUser.uuid}
                    on:click={() => accountActions.unlockAccount(currentUser)}
                    title={$t('userManagement.unlockAccountFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
                      <path d="M7 11V7a5 5 0 0 1 9.9-1"/>
                    </svg>
                  </button>
                  {#if can.forceLogout}
                  <button
                    class="icon-button force-logout-button"
                    disabled={$pendingActionUuid === currentUser.uuid}
                    on:click={() => accountActions.forceLogout(currentUser)}
                    title={$t('userManagement.forceLogoutFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
                      <polyline points="16 17 21 12 16 7"/>
                      <line x1="21" y1="12" x2="9" y2="12"/>
                    </svg>
                  </button>
                  {/if}
                  {#if can.resetMfa}
                  <button
                    class="icon-button mfa-reset-button"
                    disabled={$pendingActionUuid === currentUser.uuid}
                    on:click={() => accountActions.resetUserMFA(currentUser)}
                    title={$t('userManagement.resetMfaFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M20 13c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V6l8-3 8 3Z"/>
                      <line x1="9.5" y1="9.5" x2="14.5" y2="14.5"/>
                      <line x1="14.5" y1="9.5" x2="9.5" y2="14.5"/>
                    </svg>
                  </button>
                  {/if}
                  {#if can.linkIdentity}
                  <!-- The account_linking operator remedy (P1.3): sets the
                       provider's own identifier so a login that a source
                       like Authentik (hardcodes email_verified: false)
                       cannot link by email matches directly instead. Hidden
                       entirely for a super_admin target — the backend
                       refuses it unconditionally, that account is
                       local-only by design. -->
                  <button
                    class="icon-button link-identity-button"
                    disabled={$pendingActionUuid === currentUser.uuid}
                    on:click={() => openLinkIdentityModal(currentUser)}
                    title={$t('userManagement.linkIdentityFor', { name: currentUser.full_name || currentUser.email })}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
                      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
                    </svg>
                  </button>
                  {/if}
                  {#if !can.editExpiration}
                    <!-- No expiration editor: PUT /users/{uuid} refuses this target. -->
                  {:else if expirationEditUuid === currentUser.uuid}
                    <span class="expiration-editor">
                      <input
                        type="date"
                        bind:value={expirationEditValue}
                        aria-label={$t('userManagement.setExpirationFor', { name: currentUser.full_name || currentUser.email })}
                      />
                      <button
                        class="icon-button expiration-save-button"
                        disabled={$pendingActionUuid === currentUser.uuid}
                        on:click={() => saveExpiration(currentUser)}
                        title={$t('common.save')}
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <polyline points="20 6 9 17 4 12"/>
                        </svg>
                      </button>
                      <button
                        class="icon-button expiration-cancel-button"
                        disabled={$pendingActionUuid === currentUser.uuid}
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
                      ? $t('userManagement.editExpirationFor', { name: currentUser.full_name || currentUser.email, date: formatDateTime(currentUser.account_expires_at, $t('userManagement.notAvailable'), $locale) })
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
                  {#if can.delete}
                  <button
                    class="icon-button delete-button"
                    on:click={() => accountActions.deleteUser(currentUser.uuid)}
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
                  {/if}
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
    {#if $invitationsLoading}
      <div class="loading-state"><p>{$t('userManagement.loadingInvitations')}</p></div>
    {:else if $invitations.length === 0}
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
            {#each $invitations as invitation (invitation.uuid)}
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
                  <Badge variant={badgeVariantForStatus(invitation.status)} class="status-badge">
                    {invitationStatusLabels[invitation.status] || invitation.status}
                  </Badge>
                </td>
                <td>{formatDateTime(invitation.expires_at, $t('userManagement.notAvailable'), $locale)}</td>
                <td>
                  <div class="table-actions">
                    <button
                      class="icon-button delete-button"
                      disabled={$pendingInviteUuid === invitation.uuid}
                      on:click={() => invites.revoke(invitation)}
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
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmLabel || $t('common.delete')}
  cancelText={$t('common.cancel')}
  variant="danger"
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<!-- Link External Identity Modal (P1.3) -->
<LinkIdentityModal
  bind:isOpen={linkIdentityModalOpen}
  targetUser={linkIdentityTarget}
  on:linked={handleIdentityLinked}
  on:close={closeLinkIdentityModal}
/>

<!-- Password Reset Modal -->
{#if passwordResetUser}
  <PasswordResetModal
    target={passwordResetUser}
    {passwordMinLength}
    onClose={closePasswordResetModal}
  />
{/if}

<style>
  .user-management {
    width: 100%;
    margin-bottom: var(--space-8);
  }

  .table-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3) var(--space-4);
    margin-bottom: var(--space-4);
  }

  .search-container {
    flex: 1 1 14rem;
  }

  .search-container input {
    width: 100%;
    padding: var(--space-2);
    font-size: var(--font-size-sm);
  }

  .table-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1-5);
  }

  .add-user-form {
    background-color: var(--card-background);
    padding: var(--space-4);
    border-radius: var(--radius-sm);
    margin-bottom: var(--space-4);
    border: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .add-user-form h3 {
    font-size: 1.125rem;
    margin-bottom: var(--space-4);
  }

  .form-group {
    margin-bottom: var(--space-2);
  }

  .form-group label {
    display: block;
    margin-bottom: var(--space-1);
    font-size: var(--font-size-sm);
  }

  .form-group input, .form-group select {
    width: 100%;
    padding: var(--space-2);
    font-size: var(--font-size-sm);
  }

  .role-warning {
    display: block;
    margin-top: var(--space-1-5);
    padding: var(--space-2) var(--space-2-5);
    border: 1px solid rgba(245, 158, 11, 0.4);
    border-radius: var(--radius-md);
    background-color: rgba(245, 158, 11, 0.12);
    color: var(--text-color);
    font-size: var(--font-size-xs);
    line-height: 1.4;
  }

  .create-button {
    background-color: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: var(--radius-xl);
    cursor: pointer;
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    margin-top: var(--space-2);
    transition: all var(--duration-base) ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .create-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }

  .create-button:hover:not(:disabled),
  .create-button:focus:not(:disabled) {
    background-color: var(--primary-color);
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
    text-decoration: none;
  }

  .create-button:active:not(:disabled) {
    transform: scale(1);
  }

  .users-table {
    width: 100%;
    border-collapse: collapse;
    background-color: var(--card-background);
    border-radius: var(--radius-md);
    overflow: hidden;
    box-shadow: var(--card-shadow);
  }

  .users-table th, .users-table td {
    padding: var(--space-3);
    border-bottom: 1px solid var(--border-color);
    text-align: left;
    color: var(--text-color);
    font-size: var(--font-size-sm);
  }

  .users-table th {
    background-color: var(--table-header-bg);
    font-weight: var(--font-weight-semibold);
  }

  .users-table tr:hover {
    background-color: var(--table-row-hover);
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
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--duration-base) ease;
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
    color: var(--text-on-solid);
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
    color: var(--text-on-solid);
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
    color: var(--text-on-solid);
    transform: scale(1.05);
  }

  .fallback-toggle-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  .fallback-toggle-button.active {
    background-color: #f59e0b;
    color: var(--text-on-solid);
  }

  .fallback-toggle-button.active:hover:not(:disabled) {
    background-color: #d97706;
  }

  /* Expiration button - blue */
  .expiration-button {
    background-color: rgba(var(--accent-rgb), 0.1);
    color: var(--primary-on-surface);
  }

  .expiration-button:hover:not(:disabled) {
    background-color: var(--primary-color);
    color: var(--text-on-solid);
    transform: scale(1.05);
  }

  .expiration-button.active {
    background-color: var(--primary-color);
    color: var(--text-on-solid);
  }

  .expiration-button.active:hover:not(:disabled) {
    background-color: var(--primary-color);
  }

  .expiration-editor {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }

  .expiration-editor input[type='date'] {
    height: 32px;
    padding: 0 var(--space-1-5);
    font-size: 0.85rem;
  }

  .expiration-save-button {
    background-color: rgba(16, 185, 129, 0.1);
    color: #10b981;
  }

  .expiration-save-button:hover:not(:disabled) {
    background-color: #10b981;
    color: var(--text-on-solid);
  }

  .expiration-cancel-button {
    background-color: rgba(107, 114, 128, 0.1);
    color: #6b7280;
  }

  .expiration-cancel-button:hover:not(:disabled) {
    background-color: #6b7280;
    color: var(--text-on-solid);
  }

  /* Reset password button - brand blue */
  .reset-password-button {
    background-color: rgba(var(--primary-color-rgb), 0.1);
    color: var(--primary-on-surface);
  }

  .reset-password-button:hover:not(:disabled) {
    background-color: var(--primary-color);
    color: var(--text-on-solid);
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
    color: var(--text-on-solid);
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
    color: var(--text-on-solid);
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

  :global([data-theme='dark']) .force-logout-button {
    color: #cbd5e1;
  }

  .force-logout-button:hover:not(:disabled) {
    background-color: #475569;
    color: var(--text-on-solid);
    transform: scale(1.05);
  }

  .force-logout-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  :global([data-theme='dark']) .force-logout-button:hover:not(:disabled) {
    background-color: #64748b;
    color: var(--text-on-solid);
  }

  /* MFA reset - teal */
  .mfa-reset-button {
    background-color: rgba(13, 148, 136, 0.1);
    color: #0f766e;
  }

  .mfa-reset-button:hover:not(:disabled) {
    background-color: #0f766e;
    color: var(--text-on-solid);
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
    color: var(--text-on-solid);
    transform: scale(1.05);
  }

  .link-identity-button:active:not(:disabled) {
    transform: scale(0.95);
  }

  .form-hint {
    display: block;
    margin-top: var(--space-1);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    line-height: 1.4;
  }

  .external-note {
    margin: 0 0 var(--space-2);
    padding: var(--space-2) var(--space-2-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background-color: var(--background-color);
  }

  .invitations-section {
    margin-top: var(--space-8);
  }

  .invitations-section h3 {
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin-bottom: var(--space-3);
  }

  .invite-name {
    display: block;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .current-role {
    font-weight: var(--font-weight-semibold);
    text-transform: capitalize;
  }

  .self-user {
    font-style: italic;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .loading-state {
    padding: var(--space-8);
    text-align: center;
    background-color: var(--card-background);
    border-radius: var(--radius-sm);
    margin-top: var(--space-4);
    color: var(--text-color);
    font-size: var(--font-size-sm);
  }

  .table-scroll-wrapper {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  @media (max-width: 768px) {
    .table-controls {
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-2);
    }

    .search-container input {
      min-height: 44px;
      font-size: var(--font-size-base);
    }

    .table-actions {
      width: 100%;
    }

    .table-actions .btn {
      flex: 1 1 0;
      justify-content: center;
      min-height: 44px;
    }

    .add-user-form input,
    .add-user-form select {
      min-height: 44px;
      font-size: var(--font-size-base);
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
      padding: var(--space-2);
      font-size: var(--font-size-xs);
    }

    .icon-button {
      width: 36px;
      height: 36px;
    }

  }
</style>
