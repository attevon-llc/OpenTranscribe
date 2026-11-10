<script lang="ts">
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
        <label class="toggle-label">
          <input type="checkbox" bind:checked={retryLimitEnabled} class="toggle-input" />
          <span class="toggle-switch"></span>
          <span class="toggle-text">{$t('settings.retry.limitRetries')}</span>
        </label>
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

  .toggle-input {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-switch {
    position: relative;
    width: 36px;
    height: 20px;
    background-color: var(--border-color);
    border-radius: 10px;
    transition: background-color var(--duration-base) ease;
    flex-shrink: 0;
  }

  .toggle-switch::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    background-color: white;
    border-radius: 50%;
    transition: transform var(--duration-base) ease;
    box-shadow: var(--shadow-thumb);
  }

  .toggle-input:checked + .toggle-switch {
    background-color: var(--primary-color);
  }

  .toggle-input:checked + .toggle-switch::after {
    transform: translateX(16px);
  }

  .toggle-text {
    font-size: 0.875rem;
    color: var(--text-color);
  }

  .inline-input {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .input-label {
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .form-input {
    padding: var(--space-1-5) var(--space-2);
    font-size: 0.875rem;
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
    margin-right: auto;
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
      margin-right: 0;
    }

    .number-input {
      min-height: 44px;
      font-size: 1rem;
    }
  }
</style>
