<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import BaseModal from './ui/BaseModal.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { AdminApi } from '$lib/api/admin';

  export let isOpen = false;
  /** The account to link. `null` while the modal is closed/transitioning. */
  export let targetUser: { uuid: string; full_name?: string; email: string } | null = null;

  const dispatch = createEventDispatcher<{ linked: void; close: void }>();

  let provider: 'oidc' | 'ldap' | 'pki' = 'oidc';
  let identifier = '';
  let saving = false;

  // Re-seed whenever the modal is (re)opened, so linking one account and then
  // opening it again for another does not carry over the previous identifier.
  $: if (isOpen) {
    provider = 'oidc';
    identifier = '';
  }

  $: canSubmit = identifier.trim() !== '' && !saving && !!targetUser;
  $: targetName = targetUser?.full_name || targetUser?.email || '';

  function handleClose() {
    isOpen = false;
    dispatch('close');
  }

  async function handleSubmit() {
    if (!canSubmit || !targetUser) return;
    saving = true;
    try {
      await AdminApi.linkExternalIdentity(targetUser.uuid, provider, identifier.trim());
      toastStore.success($t('userManagement.linkIdentity.success', { name: targetName }));
      dispatch('linked');
      handleClose();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('userManagement.linkIdentity.failed')));
    } finally {
      saving = false;
    }
  }
</script>

<BaseModal
  {isOpen}
  title={$t('userManagement.linkIdentity.title')}
  maxWidth="480px"
  zIndex={1300}
  onClose={handleClose}
>
  <form id="link-identity-form" on:submit|preventDefault={handleSubmit}>
    <div class="modal-body">
      <p class="modal-intro">
        {$t('userManagement.linkIdentity.intro', { name: targetName })}
      </p>

      <div class="form-group">
        <label for="link-identity-provider">{$t('userManagement.linkIdentity.provider')}</label>
        <select id="link-identity-provider" class="form-control" bind:value={provider}>
          <option value="oidc">{$t('userManagement.linkIdentity.providerOidc')}</option>
          <option value="ldap">{$t('userManagement.linkIdentity.providerLdap')}</option>
          <option value="pki">{$t('userManagement.linkIdentity.providerPki')}</option>
        </select>
      </div>

      <div class="form-group">
        <label for="link-identity-identifier">
          {#if provider === 'oidc'}
            {$t('userManagement.linkIdentity.identifierOidc')}
          {:else if provider === 'ldap'}
            {$t('userManagement.linkIdentity.identifierLdap')}
          {:else}
            {$t('userManagement.linkIdentity.identifierPki')}
          {/if}
        </label>
        <input
          type="text"
          id="link-identity-identifier"
          class="form-control"
          bind:value={identifier}
          maxlength="512"
          required
        />
      </div>

      <p class="modal-note">{$t('userManagement.linkIdentity.note')}</p>
    </div>
  </form>

  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={handleClose} disabled={saving}>
      {$t('common.cancel')}
    </button>
    <button
      type="submit"
      form="link-identity-form"
      class="btn btn-primary"
      disabled={!canSubmit}
    >
      {saving ? $t('userManagement.linkIdentity.linking') : $t('userManagement.linkIdentity.linkButton')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .modal-body {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .modal-intro {
    margin: 0;
    font-size: 0.875rem;
    color: var(--color-text-secondary);
  }

  .modal-note {
    margin: 0;
    padding: 0.6rem 0.75rem;
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-radius: 6px;
    background: rgba(245, 158, 11, 0.12);
    color: var(--color-text);
    font-size: 0.75rem;
    line-height: 1.5;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .form-group label {
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--color-text);
  }

  .form-control {
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: 0.875rem;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--color-primary);
    box-shadow: 0 0 0 2px var(--color-primary-alpha);
  }
</style>
