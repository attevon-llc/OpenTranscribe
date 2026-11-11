<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
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
    void loadPasswordPolicy().then((policy) => (passwordPolicy = policy));
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
        <FormField id="email" label={$t('auth.email')} hint={$t('settings.profile.emailCannotChange')} let:describedby>
          <input dir="ltr"
            type="email"
            id="email" aria-describedby={describedby}
            class="form-control"
            value={email}
            disabled
          />
        </FormField>

        <FormField id="fullName" label={$t('settings.profile.fullName')}>
          <input
            type="text"
            id="fullName"
            class="form-control"
            bind:value={fullName}
            required
          />
        </FormField>

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
        <FormField id="currentPassword" label={$t('settings.profile.currentPassword')}>
          <svelte:fragment slot="label-after">
            <button
              type="button"
              class="settings-toggle-password"
              on:click={() => showCurrentPassword = !showCurrentPassword}
              tabindex="-1"
              aria-label={showCurrentPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            >
              <EyeIcon visible={showCurrentPassword} />
            </button>
          </svelte:fragment>
          <input
            type={showCurrentPassword ? 'text' : 'password'}
            id="currentPassword"
            class="form-control"
            bind:value={currentPassword}
          />
        </FormField>

        <FormField id="newPassword" label={$t('settings.profile.newPassword')}>
          <svelte:fragment slot="label-after">
            <button
              type="button"
              class="settings-toggle-password"
              on:click={() => showNewPassword = !showNewPassword}
              tabindex="-1"
              aria-label={showNewPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            >
              <EyeIcon visible={showNewPassword} />
            </button>
          </svelte:fragment>
          <input
            type={showNewPassword ? 'text' : 'password'}
            id="newPassword"
            class="form-control"
            bind:value={newPassword}
          />
          <small class="form-text" data-testid="password-requirements"
            >{passwordRequirements.map((r) => $t(r.key, r.params)).join(' · ')}</small
          >
        </FormField>

        <FormField id="confirmPassword" label={$t('settings.profile.confirmNewPassword')}>
          <svelte:fragment slot="label-after">
            <button
              type="button"
              class="settings-toggle-password"
              on:click={() => showConfirmPassword = !showConfirmPassword}
              tabindex="-1"
              aria-label={showConfirmPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            >
              <EyeIcon visible={showConfirmPassword} />
            </button>
          </svelte:fragment>
          <input
            type={showConfirmPassword ? 'text' : 'password'}
            id="confirmPassword"
            class="form-control"
            bind:value={confirmPassword}
          />
        </FormField>

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
    color: var(--error-text);
    margin: 0 0 var(--space-3) 0;
  }

  .section-title {
    font-size: 1.125rem;
    font-weight: var(--font-weight-semibold);
    margin: 0 0 var(--space-1) 0;
    color: var(--text-color);
  }

  .section-description {
    font-size: var(--font-size-sm);
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
    border-radius: var(--radius-xl);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .card-title {
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
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
    border-radius: var(--radius-xl);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .form-control {
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .form-text {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-top: var(--space-0-5);
  }


  .form-actions {
    display: flex;
    gap: var(--space-3);
    margin-top: var(--space-3);
    justify-content: flex-end;
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
