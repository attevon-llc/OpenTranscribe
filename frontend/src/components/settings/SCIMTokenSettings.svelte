<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { formatDateTime } from '$lib/utils/formatting';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount } from 'svelte';
  import { t, locale } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { ScimTokensApi, type ScimToken } from '$lib/api/scimTokens';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import Badge from '$components/ui/Badge.svelte';
  import CopyButton from '$components/ui/CopyButton.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import BaseModal from '$components/ui/BaseModal.svelte';

  /**
   * SCIM 2.0 provisioning tokens (`/api/admin/scim-tokens`). A directory
   * (Okta, Azure AD, …) authenticates its SCIM push with one of these
   * bearer tokens. The row stores only a SHA-256 digest, so the plaintext
   * is shown exactly once, immediately after `create`.
   */
  let tokens: ScimToken[] = [];
  let loading = false;

  let showCreate = false;
  let newTokenName = '';
  let newTokenExpiresAt = '';
  let creating = false;
  let justCreatedToken: string | null = null;

  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    variant: confirmVariant,
  } = confirmDialog;

  onMount(loadTokens);

  async function loadTokens() {
    loading = true;
    try {
      tokens = await ScimTokensApi.list();
    } catch (err: unknown) {
      tokens = [];
      toastStore.error(getErrorMessage(err, $t('settings.scimTokens.loadFailed')));
    } finally {
      loading = false;
    }
  }

  function openCreate() {
    newTokenName = '';
    newTokenExpiresAt = '';
    justCreatedToken = null;
    showCreate = true;
  }

  async function submitCreate() {
    if (!newTokenName.trim()) return;
    creating = true;
    try {
      const created = await ScimTokensApi.create({
        name: newTokenName.trim(),
        expires_at: newTokenExpiresAt ? new Date(newTokenExpiresAt).toISOString() : null
      });
      justCreatedToken = created.token;
      await loadTokens();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.scimTokens.toast.createFailed')));
    } finally {
      creating = false;
    }
  }

  function closeCreate() {
    showCreate = false;
    justCreatedToken = null;
  }

  function askRevoke(token: ScimToken) {
    confirmDialog.request(
      $t('settings.scimTokens.revokeTitle'),
      $t('settings.scimTokens.revokeMessage', { name: token.name }),
      () => revoke(token),
      { confirmText: $t('settings.scimTokens.revoke') }
    );
  }

  async function revoke(target: ScimToken) {
    try {
      await ScimTokensApi.revoke(target.uuid);
      toastStore.success($t('settings.scimTokens.toast.revoked'));
      await loadTokens();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.scimTokens.toast.revokeFailed')));
    }
  }

</script>

<div class="settings-panel">
  <div class="settings-info-box">
    <strong>{$t('settings.scimTokens.infoTitle')}</strong>
    <p>{$t('settings.scimTokens.infoDescription')}</p>
  </div>

  <div class="toolbar">
    <button type="button" class="btn btn-primary" on:click={openCreate}>
      {$t('settings.scimTokens.addToken')}
    </button>
  </div>

  {#if loading}
    <SettingsSkeleton variant="form" count={2} label={$t('common.loading')} />
  {:else if tokens.length === 0}
    <EmptyState
      title={$t('settings.scimTokens.emptyTitle')}
      description={$t('settings.scimTokens.emptyDescription')}
      padding="32px 16px"
    >
      <svelte:fragment slot="icon">
        <svg
          width="40"
          height="40"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <rect x="3" y="11" width="18" height="10" rx="2" />
          <path d="M7 11V7a5 5 0 0 1 10 0v4" />
        </svg>
      </svelte:fragment>
    </EmptyState>
  {:else}
    <div class="table-wrap">
      <table class="token-table">
        <thead>
          <tr>
            <th>{$t('settings.scimTokens.name')}</th>
            <th>{$t('settings.scimTokens.created')}</th>
            <th>{$t('settings.scimTokens.expires')}</th>
            <th>{$t('settings.scimTokens.lastUsed')}</th>
            <th>{$t('settings.scimTokens.status')}</th>
            <th class="actions-col">{$t('common.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {#each tokens as token (token.uuid)}
            <tr>
              <td>{token.name}</td>
              <td>{formatDateTime(token.created_at, '—', $locale)}</td>
              <td>{token.expires_at ? formatDateTime(token.expires_at, '—', $locale) : $t('settings.scimTokens.neverExpires')}</td>
              <td>{token.last_used_at ? formatDateTime(token.last_used_at, '—', $locale) : $t('settings.scimTokens.neverUsed')}</td>
              <td>
                {#if token.revoked_at}
                  <Badge variant="error">{$t('settings.scimTokens.revoked')}</Badge>
                {:else}
                  <Badge variant="success">{$t('settings.scimTokens.active')}</Badge>
                {/if}
              </td>
              <td class="actions-col">
                {#if !token.revoked_at}
                  <button type="button" class="btn btn-danger btn-row" on:click={() => askRevoke(token)}>
                    {$t('settings.scimTokens.revoke')}
                  </button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<BaseModal
  isOpen={showCreate}
  title={justCreatedToken ? $t('settings.scimTokens.createdTitle') : $t('settings.scimTokens.addToken')}
  size="sm"
  onClose={closeCreate}
>
  {#if justCreatedToken}
    <div class="warning-box">{$t('settings.scimTokens.createdWarning')}</div>
    <div class="token-reveal">
      <code>{justCreatedToken}</code>
      <CopyButton text={justCreatedToken} />
    </div>
  {:else}
    <div class="form-stack">
    <FormField id="scim-token-name" label={$t('settings.scimTokens.name')} labelSize="sm">
      <input
        id="scim-token-name"
        type="text"
        bind:value={newTokenName}
        placeholder={$t('settings.scimTokens.namePlaceholder')}
      />
    </FormField>
    <FormField id="scim-token-expiry" label={$t('settings.scimTokens.expiresOptional')} labelSize="sm">
      <input id="scim-token-expiry" type="date" bind:value={newTokenExpiresAt} />
    </FormField>
    </div>
  {/if}
  <svelte:fragment slot="footer">
    {#if justCreatedToken}
      <button type="button" class="btn btn-primary" on:click={closeCreate}>
        {$t('common.close')}
      </button>
    {:else}
      <button type="button" class="btn btn-secondary" on:click={closeCreate}>
        {$t('common.cancel')}
      </button>
      <button
        type="button"
        class="btn btn-primary"
        disabled={!newTokenName.trim() || creating}
        on:click={submitCreate}
      >
        {$t('settings.scimTokens.create')}
      </button>
    {/if}
  </svelte:fragment>
</BaseModal>

<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmLabel}
  variant={$confirmVariant}
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .settings-panel {
    max-width: 900px;
  }

  .toolbar {
    display: flex;
    justify-content: flex-end;
    margin-bottom: var(--space-4);
  }

  .table-wrap {
    overflow-x: auto;
  }

  .token-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-sm);
  }

  .token-table th {
    text-align: start;
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--color-text-secondary);
    border-bottom: 1px solid var(--color-border);
    white-space: nowrap;
  }

  .token-table td {
    padding: var(--space-2-5);
    border-bottom: 1px solid var(--color-border);
    color: var(--color-text);
    vertical-align: top;
  }

  .actions-col {
    text-align: end;
    white-space: nowrap;
  }

  .btn-row {
    padding: var(--space-1) var(--space-2-5);
    font-size: var(--font-size-xs);
  }

  :global(.form-field) input[type='text'],
  :global(.form-field) input[type='date'] {
    width: 100%;
  }

  .warning-box {
    background: var(--color-warning-bg);
    border: 1px solid var(--color-warning-border);
    border-radius: var(--radius-md);
    padding: var(--space-3);
    font-size: var(--font-size-sm);
    margin-bottom: var(--space-4);
  }

  .token-reveal {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    background: var(--color-bg-secondary);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    padding: var(--space-2-5);
    margin-bottom: var(--space-4);
  }

  .token-reveal code {
    flex: 1;
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    word-break: break-all;
  }
</style>
