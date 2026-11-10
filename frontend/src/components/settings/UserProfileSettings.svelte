<script lang="ts">
  import EyeIcon from '$components/ui/EyeIcon.svelte';
  import { onMount } from 'svelte';
  import { user as userStore, authStore, fetchUserInfo } from '$stores/auth';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { isCloudEdition } from '$lib/edition';
  import {
    loadPasswordPolicy,
    buildPasswordRequirements,
    FALLBACK_PASSWORD_POLICY,
    type PasswordPolicy,
  } from '$lib/passwordPolicy';
  import LanguageSettings from '$components/settings/LanguageSettings.svelte';
  import SecuritySettings from '$components/settings/SecuritySettings.svelte';
  import ActiveSessionsPanel from '$components/settings/ActiveSessionsPanel.svelte';
  import AccountErasureCard from '$components/settings/AccountErasureCard.svelte';

  // Managed-edition users never have a local password; the password-change card
  // and the local MFA panel are both handled by the hosted IdP and replaced by a
  // link to its account portal. Community local users keep the inline flows.
  $: isLocalUser = !isCloudEdition && $userStore?.auth_type === 'local';

  // Open the hosted account/security portal (cloud edition).
  async function openHostedAccountPortal() {
    try {
      const { openAccountPortal } = await import('$lib/cloud');
      await openAccountPortal();
    } catch (err) {
      console.error('Failed to open the hosted account portal:', err);
    }
  }

  // User Profile section
  let fullName = '';
  let email = '';
  let profileChanged = false;
  let profileLoading = false;

  // Requirements come from the server's active policy profile, not a hardcoded minimum.
  let passwordPolicy: PasswordPolicy = FALLBACK_PASSWORD_POLICY;
  $: passwordRequirements = buildPasswordRequirements(passwordPolicy);
  // The toast truncates to one line, which hid the policy reason; show it in full inline.
  let passwordError = '';

  // Password section
  let currentPassword = '';
  let newPassword = '';
  let confirmPassword = '';
  let passwordChanged: boolean;
  let passwordLoading = false;
  let showCurrentPassword = false;
  let showNewPassword = false;
  let showConfirmPassword = false;

  onMount(() => {
    if ($authStore.user) {
      fullName = $authStore.user.full_name || '';
      email = $authStore.user.email || '';
    }
    loadPasswordPolicy().then((policy) => (passwordPolicy = policy));
  });

  // Reactive user data update when authStore changes
  $: if ($authStore.user) {
    if (!fullName) fullName = $authStore.user.full_name || '';
    if (!email) email = $authStore.user.email || '';
  }

  // Reactive profile change detection
  $: if ($authStore.user) {
    profileChanged = $authStore.user.full_name !== fullName;
  }

  // Reactive password change detection
  $: {
    passwordChanged = !!(currentPassword || newPassword || confirmPassword);
  }

  // Combined profile dirty state (profile changes OR password changes)
  $: {
    const isDirty = profileChanged || passwordChanged;
    settingsModalStore.setDirty('profile', isDirty);
  }

  // Profile functions
  async function updateProfile() {
    profileLoading = true;

    try {
      const response = await axiosInstance.put('/users/me', {
        full_name: fullName
      });

      authStore.setUser(response.data);

      toastStore.success($t('settings.toast.profileUpdated'));
      profileChanged = false;
      settingsModalStore.clearDirty('profile');

      await fetchUserInfo();
    } catch (err: unknown) {
      console.error('Error updating profile:', err);
      const message = getErrorMessage(err, $t('settings.toast.profileUpdateFailed'));
      toastStore.error(message);
    } finally {
      profileLoading = false;
    }
  }

  // Password functions
  async function updatePassword() {
    passwordLoading = true;
    passwordError = '';

    // Validation
    if (!currentPassword || !newPassword || !confirmPassword) {
      toastStore.error($t('settings.toast.passwordFieldsRequired'));
      passwordLoading = false;
      return;
    }

    if (newPassword !== confirmPassword) {
      toastStore.error($t('settings.toast.passwordsNotMatch'));
      passwordLoading = false;
      return;
    }

    if (newPassword.length < 8) {
      toastStore.error($t('settings.toast.passwordTooShort'));
      passwordLoading = false;
      return;
    }

    try {
      await axiosInstance.put('/users/me', {
        password: newPassword,
        current_password: currentPassword
      });

      toastStore.success($t('settings.toast.passwordUpdated'));

      // Clear password fields
      currentPassword = '';
      newPassword = '';
      confirmPassword = '';
      showCurrentPassword = false;
      showNewPassword = false;
      showConfirmPassword = false;
      passwordChanged = false;
      // Note: dirty state is managed reactively based on profileChanged || passwordChanged
    } catch (err: unknown) {
      console.error('Error updating password:', err);
      const message = getErrorMessage(err, $t('settings.toast.passwordUpdateFailed'));
      passwordError = message;
      toastStore.error(message);
    } finally {
      passwordLoading = false;
    }
  }
</script>

<div class="content-section">
  <h3 class="section-title">{$t('settings.profile.title')}</h3>
  <p class="section-description">{$t('settings.profile.description')}</p>

  <div class="profile-grid">
    <!-- Left Column: Profile Info + Language -->
    <div class="profile-card">
      <h4 class="card-title">{$t('settings.profile.accountInfo')}</h4>
      <form on:submit|preventDefault={updateProfile} class="settings-form">
        <div class="form-group">
          <label for="email">{$t('auth.email')}</label>
          <input dir="ltr"
            type="email"
            id="email"
            class="form-control"
            value={email}
            disabled
          />
          <small class="form-text">{$t('settings.profile.emailCannotChange')}</small>
        </div>

        <div class="form-group">
          <label for="fullName">{$t('settings.profile.fullName')}</label>
          <input
            type="text"
            id="fullName"
            class="form-control"
            bind:value={fullName}
            required
          />
        </div>

        <div class="form-actions">
          <button
            type="submit"
            class="btn btn-primary"
            disabled={!profileChanged || profileLoading}
          >
            {profileLoading ? $t('common.saving') : $t('common.saveChanges')}
          </button>
        </div>
      </form>

      <div class="card-divider"></div>
      <LanguageSettings />
    </div>

    <!-- Right Column: hosted account portal link (cloud) OR local password change -->
    {#if isCloudEdition}
    <div class="profile-card">
      <h4 class="card-title">{$t('settings.profile.accountSecurity')}</h4>
      <p class="form-text">
        {$t('settings.profile.externalManaged')}
      </p>
      <div class="form-actions">
        <button type="button" class="btn btn-primary" on:click={openHostedAccountPortal}>
          {$t('settings.profile.manageAccount')}
        </button>
      </div>
    </div>
    {:else if isLocalUser}
    <div class="profile-card">
      <h4 class="card-title">{$t('settings.profile.changePassword')}</h4>
      <form on:submit|preventDefault={updatePassword} class="settings-form">
        <div class="form-group">
          <div class="password-header">
            <label for="currentPassword">{$t('settings.profile.currentPassword')}</label>
            <button
              type="button"
              class="toggle-password"
              on:click={() => showCurrentPassword = !showCurrentPassword}
              tabindex="-1"
              aria-label={showCurrentPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            >
              <EyeIcon visible={showCurrentPassword} />
            </button>
          </div>
          <input
            type={showCurrentPassword ? 'text' : 'password'}
            id="currentPassword"
            class="form-control"
            bind:value={currentPassword}
          />
        </div>

        <div class="form-group">
          <div class="password-header">
            <label for="newPassword">{$t('settings.profile.newPassword')}</label>
            <button
              type="button"
              class="toggle-password"
              on:click={() => showNewPassword = !showNewPassword}
              tabindex="-1"
              aria-label={showNewPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            >
              <EyeIcon visible={showNewPassword} />
            </button>
          </div>
          <input
            type={showNewPassword ? 'text' : 'password'}
            id="newPassword"
            class="form-control"
            bind:value={newPassword}
          />
          <small class="form-text" data-testid="password-requirements"
            >{passwordRequirements.map((r) => $t(r.key, r.params)).join(' · ')}</small
          >
        </div>

        <div class="form-group">
          <div class="password-header">
            <label for="confirmPassword">{$t('settings.profile.confirmNewPassword')}</label>
            <button
              type="button"
              class="toggle-password"
              on:click={() => showConfirmPassword = !showConfirmPassword}
              tabindex="-1"
              aria-label={showConfirmPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            >
              <EyeIcon visible={showConfirmPassword} />
            </button>
          </div>
          <input
            type={showConfirmPassword ? 'text' : 'password'}
            id="confirmPassword"
            class="form-control"
            bind:value={confirmPassword}
          />
        </div>

        {#if passwordError}
          <p class="form-text password-error" role="alert">{passwordError}</p>
        {/if}

        <div class="form-actions">
          <button
            type="submit"
            class="btn btn-primary"
            disabled={!passwordChanged || passwordLoading}
          >
            {passwordLoading ? $t('common.updating') : $t('settings.profile.updatePassword')}
          </button>
        </div>
      </form>
    </div>
    {/if}
  </div>

  <!-- Security / MFA Section — full width below. Managed-edition users handle
       MFA in the hosted account portal (linked above), so the local MFA panel
       is hidden. Community users keep the local UserMFA flow. -->
  {#if !isCloudEdition}
  <div class="mfa-card">
    <SecuritySettings />
  </div>

  <!-- Active sessions — same edition gate as the MFA card: in the managed
       edition the hosted IdP owns the session, and signing out everywhere is
       done from its account portal (linked above). -->
  <div class="mfa-card">
    <ActiveSessionsPanel />
  </div>

  <!-- Erase my account: renders nothing unless the deployment switched it on. -->
  <AccountErasureCard />
  {/if}
</div>

<style>
  .form-text.password-error {
    color: var(--error-color);
    margin: 0 0 var(--space-3) 0;
  }

  .section-title {
    font-size: 1.125rem;
    font-weight: 600;
    margin: 0 0 var(--space-1) 0;
    color: var(--text-color);
  }

  .section-description {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin: 0 0 var(--space-5) 0;
  }

  .profile-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-4);
  }

  @media (max-width: 700px) {
    .profile-grid {
      grid-template-columns: 1fr;
    }
  }

  .profile-card {
    padding: var(--space-4);
    border-radius: 10px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .card-title {
    font-size: 0.9rem;
    font-weight: 600;
    margin: 0 0 var(--space-3) 0;
    color: var(--text-color);
  }

  .card-divider {
    border-top: 1px solid var(--border-color);
    margin: var(--space-4) 0;
  }

  .mfa-card {
    margin-top: var(--space-4);
    padding: var(--space-4);
    border-radius: 10px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .form-group label {
    font-weight: 500;
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .form-control {
    padding: var(--space-2) var(--space-2-5);
    font-size: 0.8125rem;
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .form-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-top: var(--space-0-5);
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
    padding: var(--space-1);
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    border-radius: 4px;
    transition: background-color var(--duration-base);
  }

  .toggle-password:hover {
    background-color: var(--background-color);
    color: var(--text-color);
  }

  .form-actions {
    display: flex;
    gap: var(--space-3);
    margin-top: var(--space-3);
    justify-content: flex-end;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all var(--duration-base) ease;
  }

  .btn-primary {
    background-color: var(--primary-color);
    color: var(--text-on-solid);
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-primary:hover:not(:disabled) {
    background-color: var(--primary-color);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-primary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  @media (max-width: 768px) {
    .form-actions {
      flex-direction: column-reverse;
    }

    .form-actions .btn {
      width: 100%;
      min-height: 44px;
      text-align: center;
    }
  }
</style>
