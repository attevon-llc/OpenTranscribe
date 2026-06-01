<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '../../stores/settingsModalStore';

  interface CacheConfig {
    retention_days: number;
    bucket: string;
    prefix: string;
    object_count: number;
    total_bytes: number;
  }

  const BASE = '/admin/settings/cache-config';

  let loading = true;
  let saving = false;
  let clearing = false;
  let confirmClear = false;

  let retentionDays = 7;
  let origDays = 7;
  let objectCount = 0;
  let totalBytes = 0;

  $: hasChanges = retentionDays !== origDays;

  onMount(loadConfig);
  onDestroy(() => settingsModalStore.clearDirty('cache'));

  $: settingsModalStore.setDirty('cache', hasChanges);

  function apply(cfg: CacheConfig) {
    retentionDays = cfg.retention_days;
    origDays = cfg.retention_days;
    objectCount = cfg.object_count;
    totalBytes = cfg.total_bytes;
  }

  async function loadConfig() {
    loading = true;
    try {
      const res = await axiosInstance.get<CacheConfig>(BASE);
      apply(res.data);
    } catch (err) {
      console.error('Error loading cache config:', err);
      toastStore.error($t('settings.cache.loadFailed'));
    } finally {
      loading = false;
    }
  }

  async function saveConfig() {
    saving = true;
    try {
      const res = await axiosInstance.put<CacheConfig>(BASE, { retention_days: retentionDays });
      apply(res.data);
      toastStore.success($t('settings.cache.saved'));
    } catch (err) {
      console.error('Error saving cache config:', err);
      toastStore.error($t('settings.cache.saveFailed'));
    } finally {
      saving = false;
    }
  }

  async function clearCache() {
    clearing = true;
    confirmClear = false;
    try {
      const res = await axiosInstance.post<{ deleted: number }>(`${BASE}/clear`);
      toastStore.success($t('settings.cache.clearedCount', { count: res.data.deleted }));
      await loadConfig();
    } catch (err) {
      console.error('Error clearing cache:', err);
      toastStore.error($t('settings.cache.clearFailed'));
    } finally {
      clearing = false;
    }
  }

  function formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }
</script>

<div class="cache-settings">
  <div class="title-row">
    <h3 class="section-title">{$t('settings.cache.title')}</h3>
  </div>
  <p class="section-desc">{$t('settings.cache.description')}</p>

  {#if loading}
    <div class="loading-state"><Spinner size="small" /></div>
  {:else}
    <!-- Current usage -->
    <div class="status-block">
      <div class="status-item">
        <span class="status-label">{$t('settings.cache.currentUsage')}:</span>
        <span class="status-value">
          {$t('settings.cache.usageValue', { count: objectCount, size: formatBytes(totalBytes) })}
        </span>
      </div>
    </div>

    <!-- Retention -->
    <div class="field-group">
      <label class="field-label" for="cache-retention-days">{$t('settings.cache.retentionDays')}</label>
      <div class="inline-input">
        <input
          id="cache-retention-days"
          type="number"
          bind:value={retentionDays}
          min="0"
          max="3650"
          class="form-input number-input"
        />
        <span class="input-suffix">{$t('settings.cache.daysUnit')}</span>
      </div>
      <p class="hint">{$t('settings.cache.retentionHint')}</p>
    </div>

    <!-- Clear now -->
    <div class="action-row">
      {#if !confirmClear}
        <button
          type="button"
          class="btn btn-danger-outline"
          on:click={() => (confirmClear = true)}
          disabled={clearing || objectCount === 0}
        >
          {$t('settings.cache.clearButton')}
        </button>
      {:else}
        <span class="run-confirm-text">{$t('settings.cache.clearConfirmBody')}</span>
        <button type="button" class="btn btn-secondary" on:click={() => (confirmClear = false)}>
          {$t('settings.cache.cancel')}
        </button>
        <button type="button" class="btn btn-danger" on:click={clearCache} disabled={clearing}>
          {#if clearing}<Spinner size="small" />{/if}
          {$t('settings.cache.clearConfirmButton')}
        </button>
      {/if}
    </div>

    <!-- Save / Reset -->
    <div class="button-row">
      <button type="button" class="btn btn-secondary" on:click={loadConfig} disabled={saving}>
        {$t('settings.cache.resetButton')}
      </button>
      <button type="button" class="btn btn-primary" on:click={saveConfig} disabled={saving || !hasChanges}>
        {saving ? $t('settings.cache.savingButton') : $t('settings.cache.saveButton')}
      </button>
    </div>
  {/if}
</div>

<style>
  .cache-settings {
    padding: 0.5rem 0;
  }

  .section-title {
    font-size: 0.95rem;
    font-weight: 600;
    margin: 0;
    color: var(--text-color);
  }

  .section-desc {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin: 0.25rem 0 1rem 0;
    line-height: 1.4;
  }

  .loading-state {
    display: flex;
    align-items: center;
    padding: 1rem;
  }

  .status-block {
    margin: 0 0 1rem 0;
    padding: 0.6rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-secondary, rgba(0, 0, 0, 0.03));
    font-size: 0.8rem;
  }

  .status-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }

  .status-label {
    color: var(--text-muted);
    font-weight: 500;
  }

  .status-value {
    color: var(--text-color);
  }

  .field-group {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    margin-bottom: 1rem;
  }

  .field-label {
    font-size: 0.78rem;
    color: var(--text-muted);
    font-weight: 500;
  }

  .inline-input {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .input-suffix {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .hint {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin: 0.25rem 0 0 0;
    line-height: 1.4;
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

  .number-input {
    width: 70px;
    text-align: center;
  }

  .action-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.75rem;
  }

  .run-confirm-text {
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .button-row {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
    margin-top: 0.5rem;
  }

  .button-row .btn-secondary {
    margin-right: auto;
  }

  .btn-danger-outline {
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-color);
    padding: 0.375rem 0.625rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.8rem;
    font-weight: 500;
    transition: all 0.2s ease;
  }

  .btn-danger-outline:hover:not(:disabled) {
    background-color: var(--button-hover);
  }

  @media (max-width: 768px) {
    .action-row {
      flex-direction: column;
      align-items: stretch;
    }

    .action-row .btn {
      width: 100%;
      min-height: 44px;
    }

    .button-row .btn {
      flex: 1;
      min-height: 44px;
    }
  }
</style>
