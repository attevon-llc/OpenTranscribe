<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import { onMount } from 'svelte';
  import { AdminSettingsApi, type RetryConfig } from '$lib/api/adminSettings';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  // State
  let loading = true;
  let saving = false;
  let config: RetryConfig | null = null;

  // Form state
  let retryLimitEnabled = true;
  let maxRetries = 3;

  // Track original values for change detection
  let originalRetryLimitEnabled = true;
  let originalMaxRetries = 3;

  onMount(async () => {
    await loadConfig();
  });

  async function loadConfig() {
    loading = true;
    try {
      config = await AdminSettingsApi.getRetryConfig();
      retryLimitEnabled = config.retry_limit_enabled;
      maxRetries = config.max_retries;
      originalRetryLimitEnabled = retryLimitEnabled;
      originalMaxRetries = maxRetries;
    } catch (err: unknown) {
      console.error('Error loading retry config:', err);
      toastStore.error($t('settings.retry.loadFailed'));
    } finally {
      loading = false;
    }
  }

  $: hasChanges = retryLimitEnabled !== originalRetryLimitEnabled || maxRetries !== originalMaxRetries;

  async function saveConfig() {
    saving = true;
    try {
      config = await AdminSettingsApi.updateRetryConfig({
        retry_limit_enabled: retryLimitEnabled,
        max_retries: maxRetries
      });
      originalRetryLimitEnabled = config.retry_limit_enabled;
      originalMaxRetries = config.max_retries;
      toastStore.success($t('settings.retry.saved'));
    } catch (err: unknown) {
      console.error('Error saving retry config:', err);
      const errorMsg = getErrorMessage(err, $t('settings.retry.saveFailed'));
      toastStore.error(errorMsg);
    } finally {
      saving = false;
    }
  }

  function resetToDefaults() {
    retryLimitEnabled = true;
    maxRetries = 3;
  }
</script>

<SettingsCard
  title={$t('settings.retry.title')}
  description={$t('settings.retry.desc')}
  tooltip={$t('settings.retry.tooltip')}
>
  {#if loading}
    <SettingsSkeleton variant="form" count={2} label={$t('common.loading')} />
  {:else}
    <div class="setting-row">
      <div class="setting-controls">
        <div class="toggle-label">
          <Toggle id="retrysettings-toggle-1" checked={retryLimitEnabled} onchange={(v) => (retryLimitEnabled = v)} />
          <label class="toggle-text" for="retrysettings-toggle-1">{$t('settings.retry.limitRetries')}</label>
        </div>
        <div class="inline-input">
          <span class="input-label">{$t('settings.retry.max')}</span>
          <input
            type="number"
            bind:value={maxRetries}
            min="1"
            max="10"
            class="form-input number-input"
            disabled={!retryLimitEnabled}
          />
        </div>
      </div>
      <div class="button-row">
        <button type="button" class="btn btn-secondary" on:click={resetToDefaults} disabled={saving}>
          {$t('settings.retry.reset')}
        </button>
        <button type="button" class="btn btn-primary" on:click={saveConfig} disabled={saving || !hasChanges}>
          {saving ? $t('settings.retry.saving') : $t('settings.retry.save')}
        </button>
      </div>
    </div>
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

  .setting-controls {
    display: flex;
    align-items: center;
    gap: var(--space-6);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    user-select: none;
  }

  .toggle-text {
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .inline-input {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .input-label {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .form-input {
    padding: var(--space-1-5) var(--space-2);
    font-size: var(--font-size-md);
  }

  .form-input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .number-input {
    width: 55px;
    text-align: center;
  }

  .button-row {
    display: flex;
    gap: var(--space-2);
    justify-content: flex-end;
  }

  .button-row .btn-secondary {
    margin-inline-end: auto;
  }

  @media (max-width: 768px) {
    .setting-controls {
      flex-direction: column;
      align-items: flex-start;
      gap: var(--space-3);
      width: 100%;
    }

    .button-row {
      width: 100%;
      flex-direction: column-reverse;
    }

    .button-row .btn {
      width: 100%;
      min-height: 44px;
      text-align: center;
    }

    .button-row .btn-secondary {
      margin-inline-end: 0;
    }

    .number-input {
      min-height: 44px;
      font-size: var(--font-size-base);
    }
  }
</style>
