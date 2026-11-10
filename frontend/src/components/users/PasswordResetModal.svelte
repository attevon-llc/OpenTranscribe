<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import EyeIcon from '$components/ui/EyeIcon.svelte';
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
    <div class="form-stack">
    <FormField id="reset-password" label={$t('userManagement.newPassword')} hint={$t('userManagement.minimumCharacters', { min: passwordMinLength })} let:describedby>
      <svelte:fragment slot="label-after">
        <button
          type="button"
          class="settings-toggle-password"
          on:click={() => showResetPassword = !showResetPassword}
          tabindex="-1"
          aria-label={showResetPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
        >
          <EyeIcon visible={showResetPassword} />
        </button>
      </svelte:fragment>
      <input
        type={showResetPassword ? 'text' : 'password'}
        id="reset-password" aria-describedby={describedby}
        class="form-control"
        bind:value={resetPassword}
        placeholder={$t('userManagement.enterNewPassword')}
        required
        minlength={passwordMinLength}
      />
    </FormField>

    <FormField id="confirm-reset-password" label={$t('userManagement.confirmPassword')}>
      <svelte:fragment slot="label-after">
        <button
          type="button"
          class="settings-toggle-password"
          on:click={() => showConfirmResetPassword = !showConfirmResetPassword}
          tabindex="-1"
          aria-label={showConfirmResetPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
        >
          <EyeIcon visible={showConfirmResetPassword} />
        </button>
      </svelte:fragment>
      <input
        type={showConfirmResetPassword ? 'text' : 'password'}
        id="confirm-reset-password"
        class="form-control"
        bind:value={confirmResetPassword}
        placeholder={$t('userManagement.confirmNewPassword')}
        required
        minlength={passwordMinLength}
      />
    </FormField>
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

  :global(.form-field) input {
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


  .password-reset-form .form-control {
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

</style>
