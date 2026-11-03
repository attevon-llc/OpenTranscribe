<!--
  SelfErasureAdminCard.svelte — the two instance settings of self-service account erasure
  (issue #1354, super administrator): the switch (off by default) and the grace period.

  Rendered on the Privacy & Redaction "policy" tab, which is already super_admin only. The server
  clamps the grace period to its maximum, so the input only mirrors that limit.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import PrivacySwitchCard from '$components/privacy/PrivacySwitchCard.svelte';
  import {
    getSelfErasureSettings,
    putSelfErasureSettings,
    type SelfErasureSettings,
  } from '$lib/api/selfErasure';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  let current: SelfErasureSettings | null = null;
  let loading = true;
  let loadFailed = false;
  let busy = false;
  let days = 14;

  async function load() {
    loading = true;
    loadFailed = false;
    try {
      current = await getSelfErasureSettings();
      days = current.grace_days;
    } catch (error) {
      console.error('Failed to read the self-service erasure settings:', error);
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function save(update: { enabled?: boolean; grace_days?: number }) {
    if (busy) return;
    busy = true;
    try {
      current = await putSelfErasureSettings(update);
      days = current.grace_days;
      toastStore.success($t('settings.selfErasureAdmin.saved'));
    } catch (error) {
      console.error('Failed to save the self-service erasure settings:', error);
      toastStore.error($t('settings.selfErasureAdmin.saveError'));
      void load();
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<div class="self-erasure-admin" data-testid="self-erasure-admin-card">
  {#if loading}
    <SettingsSkeleton variant="rows" count={1} label={$t('common.loading')} />
  {:else if loadFailed || !current}
    <p class="error" role="alert">{$t('settings.selfErasureAdmin.loadError')}</p>
  {:else}
    <PrivacySwitchCard
      id="instance-self-erasure"
      title={$t('settings.selfErasureAdmin.title')}
      description={$t('settings.selfErasureAdmin.description')}
      checked={current.enabled}
      {busy}
      onToggle={(next) => save({ enabled: next })}
    />
    <div class="grace">
      <label for="self-erasure-grace-days">{$t('settings.selfErasureAdmin.graceLabel')}</label>
      <input
        id="self-erasure-grace-days"
        type="number"
        class="form-control"
        min="0"
        max={current.max_grace_days}
        bind:value={days}
      />
      <button
        type="button"
        class="btn btn-secondary"
        disabled={busy || days === current.grace_days}
        on:click={() => save({ grace_days: days })}
      >
        {$t('settings.selfErasureAdmin.saveGrace')}
      </button>
      <span class="hint">
        {$t('settings.selfErasureAdmin.graceHint', { max: current.max_grace_days })}
      </span>
    </div>
  {/if}
</div>

<style>
  .self-erasure-admin {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-top: 1.5rem;
  }

  .grace {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8125rem;
    color: var(--text-color);
  }

  .grace input {
    width: 5rem;
    padding: 0.375rem 0.5rem;
  }

  .hint {
    color: var(--text-secondary);
  }

  .error {
    margin: 0;
    color: var(--error-color, #dc2626);
  }
</style>
