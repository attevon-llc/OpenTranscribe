<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { PolicyProvider } from '$lib/api/asrProviderPolicy';

  export let provider: PolicyProvider;
  export let disabled = false;

  const dispatch = createEventDispatcher<{ toggle: boolean; addshared: void }>();
</script>

<li class="policy-row" data-testid="asr-policy-row-{provider.id}">
  <div class="row-head">
    <span class="provider-name">{provider.display_name}</span>
    <label class="toggle-label">
      <input
        type="checkbox"
        class="toggle-input"
        checked={provider.allowed_for_users}
        {disabled}
        aria-label={$t('settings.asrPolicy.allowFor', { name: provider.display_name })}
        on:change={(e) => dispatch('toggle', e.currentTarget.checked)}
      />
      <span class="toggle-switch"></span>
      <span class="toggle-text">{$t('settings.asrPolicy.allowText')}</span>
    </label>
  </div>

  <div class="credentials">
    <span class="credentials-label">{$t('settings.asrPolicy.sharedCredentials')}</span>
    {#if provider.shared_credentials.length === 0}
      <span class="muted">{$t('settings.asrPolicy.noShared')}</span>
    {:else}
      <ul class="credential-list">
        {#each provider.shared_credentials as cred (cred.config_uuid)}
          <li class="credential">
            <span class="credential-name">{cred.name}</span>
            {#if cred.has_api_key}
              <span class="chip">{$t('settings.asrPolicy.apiKeySet')}</span>
            {/if}
            {#if cred.has_access_key_id}
              <span class="chip">{$t('settings.asrPolicy.accessKeySet')}</span>
            {/if}
            {#if cred.owner_name}
              <span class="muted">{$t('settings.asrProvider.sharedBy', { name: cred.owner_name })}</span>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    <button
      type="button"
      class="btn btn-secondary add-shared"
      {disabled}
      aria-label={$t('settings.asrPolicy.addSharedFor', { name: provider.display_name })}
      on:click={() => dispatch('addshared')}
    >
      {$t('settings.asrPolicy.addShared')}
    </button>
  </div>
</li>

<style>
  .policy-row {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 0.85rem 0;
    border-top: 1px solid var(--border-color);
  }
  .policy-row:first-child {
    border-top: none;
  }
  .row-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .provider-name {
    font-weight: var(--font-weight-semibold, 600);
    color: var(--text-color);
  }
  .credentials {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 0.75rem;
    font-size: 0.8rem;
  }
  .credentials-label,
  .muted {
    color: var(--text-secondary);
  }
  .credential-list {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .credential {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem;
  }
  .credential-name {
    color: var(--text-color);
    font-weight: var(--font-weight-medium, 500);
  }
  .chip {
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    background: rgba(var(--success-color-rgb, 34, 197, 94), 0.14);
    color: var(--success-color, #15803d);
    border: 1px solid rgba(var(--success-color-rgb, 34, 197, 94), 0.35);
  }
  .add-shared {
    margin-inline-start: auto;
  }
  :global([data-theme='dark']) .chip {
    color: var(--success-color, #4ade80);
  }
</style>
