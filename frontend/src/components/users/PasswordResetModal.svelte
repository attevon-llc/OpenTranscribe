<script lang="ts">
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import BaseModal from '$components/ui/BaseModal.svelte';

  /**
   * An admin setting a new local password on another account (`PUT /users/{uuid}`).
   * Mounted only while open, so every open starts with empty, hidden fields.
   */
  export let target: { uuid: string; email: string; full_name?: string };
  /** The server-owned minimum (`GET /auth/password-policy`), loaded by the table. */
  export let passwordMinLength: number;
  export let onClose: () => void;

  let resetPassword = '';
  let confirmResetPassword = '';
  let passwordResetLoading = false;
  let showResetPassword = false;
  let showConfirmResetPassword = false;

  async function executePasswordReset() {
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
      await axiosInstance.put(`/users/${target.uuid}`, {
        password: resetPassword
      });

      const userName = target.full_name || target.email;
      toastStore.success($t('userManagement.passwordResetSuccess', { userName }));
      onClose();
    } catch (err) {
      console.error('Error resetting password:', err);
      toastStore.error(getErrorMessage(err, $t('userManagement.passwordResetFailed')));
    } finally {
      passwordResetLoading = false;
    }
  }
</script>

<BaseModal
  isOpen={true}
  title={$t('userManagement.resetPassword')}
  size="sm"
  {onClose}
>
  <p class="modal-description">
    {$t('userManagement.setNewPasswordFor')} <strong>{target.full_name || target.email}</strong>
  </p>

  <form id="password-reset-form" on:submit|preventDefault={executePasswordReset} class="password-reset-form">
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
  </form>

  <svelte:fragment slot="footer">
    <button
      type="button"
      class="btn btn-secondary"
      on:click={onClose}
      disabled={passwordResetLoading}
    >
      {$t('common.cancel')}
    </button>
    <button
      type="submit"
      form="password-reset-form"
      class="btn btn-primary"
      disabled={passwordResetLoading || !resetPassword || !confirmResetPassword}
    >
      {passwordResetLoading ? $t('userManagement.resetting') : $t('userManagement.resetPasswordButton')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  /* Declarations and order carried over from UserManagementTable.svelte, on tokens of the
     same value. */
  .form-group {
    margin-block-end: var(--space-2);
  }

  .form-group label {
    display: block;
    margin-block-end: var(--space-1);
    font-size: var(--font-size-sm);
  }

  .form-group input {
    width: 100%;
    padding: var(--space-2);
    font-size: var(--font-size-sm);
  }

  .modal-description {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin: 0 0 var(--space-5) 0;
  }

  .modal-description strong {
    color: var(--text-color);
  }

  .password-reset-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .password-reset-form .form-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .password-reset-form label {
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    font-size: var(--font-size-sm);
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
    border-radius: var(--radius-sm);
    transition: background-color var(--duration-base);
  }

  .toggle-password:hover {
    background-color: var(--background-color);
    color: var(--text-color);
  }

  .password-reset-form .form-control {
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

  .password-reset-form .form-text {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-block-start: var(--space-0-5);
  }
</style>
