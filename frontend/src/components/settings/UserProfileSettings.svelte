<script lang="ts">
  import { onMount } from 'svelte';
  import { user as userStore, authStore, fetchUserInfo } from '$stores/auth';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { isCloudEdition } from '$lib/edition';
  import LanguageSettings from '$components/settings/LanguageSettings.svelte';
  import SecuritySettings from '$components/settings/SecuritySettings.svelte';
  import ActiveSessionsPanel from '$components/settings/ActiveSessionsPanel.svelte';

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

  // Password section
  let currentPassword = '';
  let newPassword = '';
  let confirmPassword = '';
  let passwordChanged = false;
  let passwordLoading = false;
  let showCurrentPassword = false;
  let showNewPassword = false;
  let showConfirmPassword = false;

  onMount(() => {
    if ($authStore.user) {
      fullName = $authStore.user.full_name || '';
      email = $authStore.user.email || '';
    }
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
          <input
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
              {#if showCurrentPassword}
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
              {#if showNewPassword}
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
            type={showNewPassword ? 'text' : 'password'}
            id="newPassword"
            class="form-control"
            bind:value={newPassword}
          />
          <small class="form-text">{$t('auth.passwordMinLength')}</small>
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
              {#if showConfirmPassword}
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
            type={showConfirmPassword ? 'text' : 'password'}
            id="confirmPassword"
            class="form-control"
            bind:value={confirmPassword}
          />
        </div>

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
  {/if}
</div>

<style>
  .section-title {
    font-size: 1.125rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
    color: var(--text-color);
  }

  .section-description {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin: 0 0 1.25rem 0;
  }

  .profile-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
  }

  @media (max-width: 700px) {
    .profile-grid {
      grid-template-columns: 1fr;
    }
  }

  .profile-card {
    padding: 1rem;
    border-radius: 10px;
    background: var(--surface-color, #333);
    border: 1px solid var(--border-color, #444);
  }

  .card-title {
    font-size: 0.9rem;
    font-weight: 600;
    margin: 0 0 0.75rem 0;
    color: var(--text-color);
  }

  .card-divider {
    border-top: 1px solid var(--border-color, #444);
    margin: 1rem 0;
  }

  .mfa-card {
    margin-top: 1rem;
    padding: 1rem;
    border-radius: 10px;
    background: var(--surface-color, #333);
    border: 1px solid var(--border-color, #444);
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .form-group label {
    font-weight: 500;
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .form-control {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light);
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    background-color: var(--background-color);
  }

  .form-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-top: 0.125rem;
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

  .form-actions {
    display: flex;
    gap: 0.75rem;
    margin-top: 0.75rem;
    justify-content: flex-end;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-primary {
    background-color: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-primary:hover:not(:disabled) {
    background-color: #2563eb;
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
