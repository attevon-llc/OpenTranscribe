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
    gap: var(--space-2-5);
    padding: var(--space-3-5) 0;
    border-top: 1px solid var(--border-color);
  }
  .policy-row:first-child {
    border-top: none;
  }
  .row-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    flex-wrap: wrap;
  }
  .provider-name {
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }
  .credentials {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    font-size: var(--font-size-sm);
  }
  .credentials-label,
  .muted {
    color: var(--text-secondary);
  }
  .credential-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .credential {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1-5);
  }
  .credential-name {
    color: var(--text-color);
    font-weight: var(--font-weight-medium);
  }
  .chip {
    padding: 1px var(--space-2);
    border-radius: var(--radius-xl);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    background: rgba(var(--success-color-rgb), 0.14);
    color: var(--success-text);
    border: 1px solid rgba(var(--success-color-rgb), 0.35);
  }
  .add-shared {
    margin-inline-start: auto;
  }
  :global([data-theme='dark']) .chip {
    color: var(--success-text);
  }
</style>
