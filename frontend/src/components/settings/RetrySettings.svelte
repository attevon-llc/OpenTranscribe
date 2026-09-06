<script lang="ts">
  import { onMount } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import { AdminSettingsApi, type RetryConfig } from '../../lib/api/adminSettings';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  // State
  let loading = true;
  let saving = false;
  let config: RetryConfig | null = null;

  // Form state
  let retryLimitEnabled = true;
  let maxRetries = 3;
  let hasChanges = false;

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
      hasChanges = false;
    } catch (err: unknown) {
      console.error('Error loading retry config:', err);
      toastStore.error($t('settings.retry.loadFailed'));
    } finally {
      loading = false;
    }
  }

  function checkForChanges() {
    hasChanges = retryLimitEnabled !== originalRetryLimitEnabled || maxRetries !== originalMaxRetries;
  }

  $: {
    // Reactive change detection
    retryLimitEnabled;
    maxRetries;
    checkForChanges();
  }

  async function saveConfig() {
    saving = true;
    try {
      config = await AdminSettingsApi.updateRetryConfig({
        retry_limit_enabled: retryLimitEnabled,
        max_retries: maxRetries
      });
      originalRetryLimitEnabled = config.retry_limit_enabled;
      originalMaxRetries = config.max_retries;
      hasChanges = false;
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

<div class="retry-settings">
  <div class="title-row">
    <h3 class="section-title">{$t('settings.retry.title')}</h3>
    <span class="info-icon">
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="12" y1="16" x2="12" y2="12"></line>
        <line x1="12" y1="8" x2="12.01" y2="8"></line>
      </svg>
      <span class="tooltip">{$t('settings.retry.tooltip')}</span>
    </span>
  </div>
  <p class="section-desc">{$t('settings.retry.desc')}</p>

  {#if loading}
    <div class="loading-state">
      <Spinner size="small" />
    </div>
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
</div>

<style>
  .retry-settings {
    padding: 0.5rem 0;
  }

  .title-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .section-title {
    font-size: 0.95rem;
    font-weight: 600;
    margin: 0;
    color: var(--text-color);
  }

  .info-icon {
    position: relative;
    color: var(--text-muted);
    cursor: help;
    display: flex;
    align-items: center;
  }

  .info-icon:hover {
    color: var(--text-color);
  }

  .tooltip {
    visibility: hidden;
    opacity: 0;
    position: absolute;
    left: 50%;
    top: calc(100% + 8px);
    transform: translateX(-50%);
    background-color: var(--surface-color, #333);
    color: var(--text-color);
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    font-size: 0.75rem;
    line-height: 1.4;
    width: 260px;
    z-index: 100;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
    border: 1px solid var(--border-color);
    transition: opacity 0.1s ease;
  }

  .info-icon:hover .tooltip {
    visibility: visible;
    opacity: 1;
  }

  .section-desc {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin: 0 0 1rem 0;
  }

  .loading-state {
    display: flex;
    align-items: center;
    padding: 1rem;
  }

  .setting-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
  }

  .setting-controls {
    display: flex;
    align-items: center;
    gap: 1.5rem;
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
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
    transition: background-color 0.2s ease;
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
    transition: transform 0.2s ease;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
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
    gap: 0.5rem;
  }

  .input-label {
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .form-input {
    padding: 0.375rem 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background-color: var(--background-color);
    color: var(--text-color);
    font-size: 0.875rem;
  }

  .form-input:focus {
    outline: none;
    border-color: var(--primary-color);
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
    gap: 0.5rem;
    justify-content: flex-end;
  }

  .button-row .btn-secondary {
    margin-right: auto;
  }

  @media (max-width: 768px) {
    .setting-controls {
      flex-direction: column;
      align-items: flex-start;
      gap: 0.75rem;
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
