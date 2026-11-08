<script lang="ts">
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import { onMount } from 'svelte';
  import { AdminSettingsApi } from '$lib/api/adminSettings';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  /** The coded default (`DEFAULT_TRANSCRIPTION_FAIR_SHARE_THRESHOLD`). */
  const DEFAULT_THRESHOLD = 5;
  const MAX_THRESHOLD = 1000;

  let loading = true;
  let saving = false;
  let threshold = DEFAULT_THRESHOLD;
  let saved = DEFAULT_THRESHOLD;

  $: valid = Number.isInteger(threshold) && threshold >= 0 && threshold <= MAX_THRESHOLD;
  $: hasChanges = threshold !== saved;

  onMount(async () => {
    try {
      const config = await AdminSettingsApi.getRetryConfig();
      threshold = config.fair_share_threshold;
      saved = threshold;
    } catch (err: unknown) {
      console.error('Error loading fair-share setting:', err);
      toastStore.error($t('settings.fairShare.loadFailed'));
    } finally {
      loading = false;
    }
  });

  async function save() {
    saving = true;
    try {
      const config = await AdminSettingsApi.updateRetryConfig({ fair_share_threshold: threshold });
      threshold = config.fair_share_threshold;
      saved = threshold;
      toastStore.success($t('settings.fairShare.saved'));
    } catch (err: unknown) {
      console.error('Error saving fair-share setting:', err);
      toastStore.error(getErrorMessage(err, $t('settings.fairShare.saveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

<SettingsCard
  title={$t('settings.fairShare.title')}
  description={$t('settings.fairShare.desc')}
  tooltip={$t('settings.fairShare.tooltip')}
>
  {#if loading}
    <SettingsSkeleton variant="form" count={1} label={$t('common.loading')} />
  {:else}
    <div class="setting-row">
      <label class="inline-input" for="fair-share-threshold">
        <span class="input-label">{$t('settings.fairShare.label')}</span>
        <input
          id="fair-share-threshold"
          type="number"
          bind:value={threshold}
          min="0"
          max={MAX_THRESHOLD}
          class="form-input number-input"
          aria-describedby="fair-share-help"
        />
      </label>
      <div class="button-row">
        <button
          type="button"
          class="btn btn-secondary"
          on:click={() => (threshold = DEFAULT_THRESHOLD)}
          disabled={saving}
        >
          {$t('settings.retry.reset')}
        </button>
        <button
          type="button"
          class="btn btn-primary"
          on:click={save}
          disabled={saving || !hasChanges || !valid}
        >
          {saving ? $t('settings.retry.saving') : $t('settings.retry.save')}
        </button>
      </div>
    </div>
    <p id="fair-share-help" class="help-text">
      {threshold === 0 ? $t('settings.fairShare.offHint') : $t('settings.fairShare.help')}
    </p>
  {/if}
</SettingsCard>

<style>
  .setting-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    flex-wrap: wrap;
  }

  .inline-input {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .input-label {
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .form-input {
    padding: var(--space-1-5) var(--space-2);
    font-size: var(--font-size-md);
  }

  .number-input {
    width: 70px;
    text-align: center;
  }

  .button-row {
    display: flex;
    gap: var(--space-2);
    justify-content: flex-end;
  }

  .help-text {
    margin: var(--space-2) 0 0;
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  @media (max-width: 768px) {
    .button-row {
      width: 100%;
      flex-direction: column-reverse;
    }

    .button-row .btn {
      width: 100%;
      min-height: 44px;
    }

    .number-input {
      min-height: 44px;
      font-size: var(--font-size-base);
    }
  }
</style>
