<!--
  CloudProviderPolicy.svelte - which cloud transcription providers plain users may choose, and
  whether they may save keys of their own (issue #1246). Mounted under the saved-provider list
  on Transcription > Provider.

  `/admin/asr-providers` is super_admin only: `locked` (an admin) renders the explanation and
  makes NO request. Credentials are write-only here: only "key set" chips are shown. Entering
  one reuses the existing config editor; this panel just asks its parent to open it
  (`addshared`) and re-reads the policy through `reload()` once a shared config was saved.
-->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import CloudProviderPolicyRow from './CloudProviderPolicyRow.svelte';
  import {
    buildPolicyUpdate,
    getProviderPolicy,
    updateProviderPolicy,
    type PolicyProvider,
    type ProviderPolicy,
  } from '$lib/api/asrProviderPolicy';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import '$components/settings/transcription/formControls.css';

  /** An admin who is not a super administrator: read-only, and nothing is requested. */
  export let locked = false;

  const dispatch = createEventDispatcher<{ addshared: { providerId: string } }>();

  let loading = false;
  let saving = false;
  let loadFailed = false;
  let providers: PolicyProvider[] = [];
  let ownKeys = true;
  let saved: ProviderPolicy | null = null;

  $: dirty =
    saved !== null &&
    (ownKeys !== saved.users_may_add_own_keys ||
      providers.some((p, i) => p.allowed_for_users !== saved?.providers[i]?.allowed_for_users));

  function adopt(policy: ProviderPolicy) {
    saved = policy;
    providers = policy.providers.map((p) => ({ ...p }));
    ownKeys = policy.users_may_add_own_keys;
  }

  /** Re-reads the policy (shared credentials changed); unsaved toggles are replaced. */
  export async function reload() {
    if (locked) return;
    loading = saved === null;
    loadFailed = false;
    try {
      adopt(await getProviderPolicy());
    } catch {
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function save() {
    saving = true;
    try {
      adopt(await updateProviderPolicy(buildPolicyUpdate(providers, ownKeys)));
      toastStore.success($t('settings.asrPolicy.saved'));
    } catch {
      toastStore.error($t('settings.asrPolicy.saveFailed'));
    } finally {
      saving = false;
    }
  }

  function setAllowed(id: string, allowed: boolean) {
    providers = providers.map((p) => (p.id === id ? { ...p, allowed_for_users: allowed } : p));
  }

  onMount(() => {
    void reload();
  });
</script>

<div class="cloud-policy tx-form" data-testid="asr-policy">
  <SettingsCard
    title={$t('settings.asrPolicy.title')}
    description={$t('settings.asrPolicy.description')}
    tooltip={$t('settings.asrPolicy.tip')}
  >
    {#if locked}
      <p class="locked-hint" data-testid="asr-policy-locked">{$t('settings.asrPolicy.locked')}</p>
    {:else if loading}
      <div class="state"><Spinner size="medium" /></div>
    {:else if loadFailed}
      <p class="error-text" role="alert">{$t('settings.asrPolicy.loadFailed')}</p>
    {:else if providers.length === 0}
      <p class="muted">{$t('settings.asrPolicy.noProviders')}</p>
    {:else}
      <ul class="policy-list">
        {#each providers as provider (provider.id)}
          <CloudProviderPolicyRow
            {provider}
            disabled={saving}
            on:toggle={(e) => setAllowed(provider.id, e.detail)}
            on:addshared={() => dispatch('addshared', { providerId: provider.id })}
          />
        {/each}
      </ul>
      <p class="muted keys-note">{$t('settings.asrPolicy.keysNeverShown')}</p>

      <div class="own-keys">
        <label class="toggle-label">
          <input type="checkbox" class="toggle-input" bind:checked={ownKeys} disabled={saving} />
          <span class="toggle-switch"></span>
          <span class="toggle-text">{$t('settings.asrPolicy.ownKeys')}</span>
        </label>
        <p class="muted">{$t('settings.asrPolicy.ownKeysHint')}</p>
      </div>

      <div class="actions">
        <button type="button" class="btn btn-primary" disabled={saving || !dirty} on:click={save}>
          {saving ? $t('settings.asrPolicy.saving') : $t('settings.asrPolicy.save')}
        </button>
      </div>
    {/if}
  </SettingsCard>
</div>

<style>
  .cloud-policy {
    margin-top: 1.5rem;
  }
  .policy-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .muted,
  .locked-hint {
    color: var(--text-secondary);
    font-size: 0.8rem;
    margin: 0;
  }
  .locked-hint {
    padding: 0.75rem 1rem;
    border: 1px dashed var(--border-color);
    border-radius: 8px;
  }
  .keys-note {
    margin-top: 0.5rem;
  }
  .error-text {
    color: var(--error-color);
    margin: 0;
  }
  .state {
    display: flex;
    justify-content: center;
    padding: 1rem;
  }
  .own-keys {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    margin-top: 1rem;
  }
</style>
