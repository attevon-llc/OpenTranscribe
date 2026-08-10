<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { ScimTokensApi, type ScimToken } from '$lib/api/scimTokens';
  import EmptyState from '../ui/EmptyState.svelte';
  import Badge from '../ui/Badge.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import BaseModal from '../ui/BaseModal.svelte';

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

  let showRevokeConfirm = false;
  let pendingRevoke: ScimToken | null = null;

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
    pendingRevoke = token;
    showRevokeConfirm = true;
  }

  async function confirmRevoke() {
    const target = pendingRevoke;
    pendingRevoke = null;
    if (!target) return;
    try {
      await ScimTokensApi.revoke(target.uuid);
      toastStore.success($t('settings.scimTokens.toast.revoked'));
      await loadTokens();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.scimTokens.toast.revokeFailed')));
    }
  }

  function formatDate(value: string | null): string {
    if (!value) return '—';
    return new Date(value).toLocaleString();
  }
</script>

<div class="settings-panel">
  <div class="info-box">
    <strong>{$t('settings.scimTokens.infoTitle')}</strong>
    <p>{$t('settings.scimTokens.infoDescription')}</p>
  </div>

  <div class="toolbar">
    <button type="button" class="btn btn-primary" on:click={openCreate}>
      {$t('settings.scimTokens.addToken')}
    </button>
  </div>

  {#if loading}
    <div class="loading">{$t('common.loading')}</div>
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
              <td>{formatDate(token.created_at)}</td>
              <td>{token.expires_at ? formatDate(token.expires_at) : $t('settings.scimTokens.neverExpires')}</td>
              <td>{token.last_used_at ? formatDate(token.last_used_at) : $t('settings.scimTokens.neverUsed')}</td>
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
  maxWidth="420px"
  onClose={closeCreate}
>
  {#if justCreatedToken}
    <div class="warning-box">{$t('settings.scimTokens.createdWarning')}</div>
    <div class="token-reveal">
      <code>{justCreatedToken}</code>
      <CopyButton text={justCreatedToken} />
    </div>
  {:else}
    <div class="form-group">
      <label for="scim-token-name">{$t('settings.scimTokens.name')}</label>
      <input
        id="scim-token-name"
        type="text"
        bind:value={newTokenName}
        placeholder={$t('settings.scimTokens.namePlaceholder')}
      />
    </div>
    <div class="form-group">
      <label for="scim-token-expiry">{$t('settings.scimTokens.expiresOptional')}</label>
      <input id="scim-token-expiry" type="date" bind:value={newTokenExpiresAt} />
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
  bind:isOpen={showRevokeConfirm}
  title={$t('settings.scimTokens.revokeTitle')}
  message={$t('settings.scimTokens.revokeMessage', { name: pendingRevoke?.name ?? '' })}
  confirmText={$t('settings.scimTokens.revoke')}
  on:confirm={confirmRevoke}
  on:cancel={() => (pendingRevoke = null)}
/>

<style>
  .settings-panel {
    max-width: 900px;
  }

  .info-box {
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1.5rem;
  }

  .info-box strong {
    color: var(--color-text);
  }

  .info-box p {
    margin: 0.5rem 0 0 0;
    font-size: 0.875rem;
    color: var(--color-text-secondary);
    line-height: 1.5;
  }

  .toolbar {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 1rem;
  }

  .loading {
    text-align: center;
    padding: 2rem;
    color: var(--color-text-secondary);
  }

  .table-wrap {
    overflow-x: auto;
  }

  .token-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .token-table th {
    text-align: left;
    padding: 0.5rem 0.6rem;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--color-text-secondary);
    border-bottom: 1px solid var(--color-border);
    white-space: nowrap;
  }

  .token-table td {
    padding: 0.6rem;
    border-bottom: 1px solid var(--color-border);
    color: var(--color-text);
    vertical-align: top;
  }

  .actions-col {
    text-align: right;
    white-space: nowrap;
  }

  .btn-row {
    padding: 0.3rem 0.65rem;
    font-size: 0.75rem;
  }

  .form-group {
    margin-bottom: 1rem;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.35rem;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--color-text);
  }

  .form-group input[type='text'],
  .form-group input[type='date'] {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: var(--color-bg);
    color: var(--color-text);
  }

  .warning-box {
    background: var(--color-warning-bg, rgba(217, 119, 6, 0.1));
    border: 1px solid var(--color-warning-border, rgba(217, 119, 6, 0.3));
    border-radius: 6px;
    padding: 0.75rem;
    font-size: 0.8125rem;
    margin-bottom: 1rem;
  }

  .token-reveal {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: var(--color-bg-secondary);
    border: 1px solid var(--color-border);
    border-radius: 6px;
    padding: 0.6rem;
    margin-bottom: 1rem;
  }

  .token-reveal code {
    flex: 1;
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: 0.75rem;
    word-break: break-all;
  }
</style>
