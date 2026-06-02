<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { t } from '$stores/locale';
  import ProgressBar from '../ui/ProgressBar.svelte';

  let enabled = true;
  let confidenceThreshold = 0.75;
  let tagsEnabled = true;
  let collectionsEnabled = true;
  let bulkGroupingEnabled = true;

  let loading = false;
  let saving = false;
  let retroactiveRunning = false;
  let retroactiveProcessed = 0;
  let retroactiveTotal = 0;
  let retroactiveEtaSeconds: number | null = null;
  let retroactiveMessage = '';
  let hasChanges = false;

  // Original values for change tracking
  let originalEnabled = true;
  let originalThreshold = 0.75;
  let originalTagsEnabled = true;
  let originalCollectionsEnabled = true;
  let originalBulkGroupingEnabled = true;

  $: hasChanges = enabled !== originalEnabled ||
    confidenceThreshold !== originalThreshold ||
    tagsEnabled !== originalTagsEnabled ||
    collectionsEnabled !== originalCollectionsEnabled ||
    bulkGroupingEnabled !== originalBulkGroupingEnabled;

  // Integrate with settingsModalStore dirty state
  $: settingsModalStore.setDirty('auto-labeling', hasChanges);

  async function loadSettings() {
    loading = true;
    try {
      const [settingsRes, progressRes] = await Promise.all([
        axiosInstance.get('/user-settings/auto-label'),
        axiosInstance.get('/files/retroactive-auto-label/status'),
      ]);

      const data = settingsRes.data;
      enabled = data.enabled ?? true;
      confidenceThreshold = data.confidence_threshold ?? 0.75;
      tagsEnabled = data.tags_enabled ?? true;
      collectionsEnabled = data.collections_enabled ?? true;
      bulkGroupingEnabled = data.bulk_grouping_enabled ?? true;

      originalEnabled = enabled;
      originalThreshold = confidenceThreshold;
      originalTagsEnabled = tagsEnabled;
      originalCollectionsEnabled = collectionsEnabled;
      originalBulkGroupingEnabled = bulkGroupingEnabled;

      // Restore progress state if a retroactive run is active
      const progress = progressRes.data;
      if (progress.running) {
        retroactiveRunning = true;
        retroactiveTotal = progress.total || 0;
        retroactiveProcessed = progress.processed || 0;
      }
    } catch (err) {
      console.error('[AutoLabelSettings] Error loading settings:', err);
      toastStore.error($t('autoLabel.loadFailed'));
    } finally {
      loading = false;
    }
  }

  async function saveSettings() {
    saving = true;
    try {
      const response = await axiosInstance.put('/user-settings/auto-label', {
        enabled,
        confidence_threshold: confidenceThreshold,
        tags_enabled: tagsEnabled,
        collections_enabled: collectionsEnabled,
        bulk_grouping_enabled: bulkGroupingEnabled,
      });

      const data = response.data;
      originalEnabled = data.enabled;
      originalThreshold = data.confidence_threshold;
      originalTagsEnabled = data.tags_enabled;
      originalCollectionsEnabled = data.collections_enabled;
      originalBulkGroupingEnabled = data.bulk_grouping_enabled;

      toastStore.success($t('autoLabel.settingsSaved'));
    } catch (err) {
      console.error('[AutoLabelSettings] Error saving settings:', err);
      toastStore.error($t('autoLabel.settingsSaveFailed'));
    } finally {
      saving = false;
    }
  }

  async function triggerRetroactiveApply() {
    retroactiveRunning = true;
    retroactiveProcessed = 0;
    retroactiveTotal = 0;
    retroactiveEtaSeconds = null;
    retroactiveMessage = '';
    try {
      await axiosInstance.post('/files/retroactive-auto-label', {});
      toastStore.success($t('autoLabel.retroactiveStarted'));
      // UI state will be updated by WebSocket auto-label-status events
    } catch (err) {
      console.error('[AutoLabelSettings] Error triggering retroactive apply:', err);
      toastStore.error($t('autoLabel.retroactiveFailed'));
      retroactiveRunning = false;
    }
  }

  function formatEta(seconds: number | null | undefined): string {
    if (seconds == null || seconds <= 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }

  // WebSocket event handler for auto-label status updates
  function handleAutoLabelStatus(event: CustomEvent) {
    const detail = event.detail;
    if (!detail) return;

    const status = detail.status;
    const message = detail.message || '';

    if (status === 'processing') {
      retroactiveRunning = true;
      retroactiveMessage = message;
      retroactiveProcessed = detail.processed ?? retroactiveProcessed;
      retroactiveTotal = detail.total ?? retroactiveTotal;
      retroactiveEtaSeconds = detail.eta_seconds ?? null;
    } else if (status === 'completed' || status === 'stopped') {
      retroactiveRunning = false;
      retroactiveProcessed = 0;
      retroactiveTotal = 0;
      retroactiveEtaSeconds = null;
      retroactiveMessage = '';
      if (status === 'completed') {
        toastStore.success(message);
      } else {
        toastStore.info(message || 'Auto-labeling stopped');
      }
    } else if (status === 'failed') {
      retroactiveRunning = false;
      retroactiveProcessed = 0;
      retroactiveTotal = 0;
      retroactiveEtaSeconds = null;
      retroactiveMessage = '';
      toastStore.error(message);
    }
  }

  onMount(() => {
    loadSettings();
    window.addEventListener('auto-label-status', handleAutoLabelStatus as EventListener);
  });

  onDestroy(() => {
    window.removeEventListener('auto-label-status', handleAutoLabelStatus as EventListener);
  });
</script>

<div class="auto-label-settings">
  {#if loading}
    <div class="skeleton-rows">
      <div class="skeleton-row">
        <div class="skeleton-text"></div>
        <div class="skeleton-toggle"></div>
      </div>
      <div class="skeleton-row">
        <div class="skeleton-text wide"></div>
        <div class="skeleton-toggle"></div>
      </div>
      <div class="skeleton-row">
        <div class="skeleton-text"></div>
        <div class="skeleton-toggle"></div>
      </div>
    </div>
  {:else}
    <div class="settings-group">
      <!-- Master Toggle -->
      <div class="setting-row">
        <div class="setting-info">
          <label for="auto-label-enabled" class="setting-label">{$t('autoLabel.enableLabel')}</label>
          <span class="setting-help">{$t('autoLabel.enableHelp')}</span>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="auto-label-enabled" bind:checked={enabled} />
          <span class="toggle-slider"></span>
        </label>
      </div>

      {#if enabled}
        <!-- Confidence Threshold -->
        <div class="setting-row">
          <div class="setting-info">
            <label for="confidence-threshold" class="setting-label">{$t('autoLabel.thresholdLabel')}</label>
            <span class="setting-help">{$t('autoLabel.thresholdHelp')}</span>
          </div>
          <div class="slider-container">
            <input
              type="range"
              id="confidence-threshold"
              min="0.5"
              max="1.0"
              step="0.05"
              bind:value={confidenceThreshold}
              class="threshold-slider"
            />
            <span class="threshold-value">{Math.round(confidenceThreshold * 100)}%</span>
          </div>
        </div>

        <!-- Tags Toggle -->
        <div class="setting-row">
          <div class="setting-info">
            <label for="auto-label-tags" class="setting-label">{$t('autoLabel.tagsLabel')}</label>
            <span class="setting-help">{$t('autoLabel.tagsHelp')}</span>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="auto-label-tags" bind:checked={tagsEnabled} />
            <span class="toggle-slider"></span>
          </label>
        </div>

        <!-- Collections Toggle -->
        <div class="setting-row">
          <div class="setting-info">
            <label for="auto-label-collections" class="setting-label">{$t('autoLabel.collectionsLabel')}</label>
            <span class="setting-help">{$t('autoLabel.collectionsHelp')}</span>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="auto-label-collections" bind:checked={collectionsEnabled} />
            <span class="toggle-slider"></span>
          </label>
        </div>

        <!-- Bulk Grouping Toggle -->
        <div class="setting-row">
          <div class="setting-info">
            <label for="auto-label-bulk" class="setting-label">{$t('autoLabel.bulkGroupingLabel')}</label>
            <span class="setting-help">{$t('autoLabel.bulkGroupingHelp')}</span>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="auto-label-bulk" bind:checked={bulkGroupingEnabled} />
            <span class="toggle-slider"></span>
          </label>
        </div>
      {/if}
    </div>

    <!-- Save Button -->
    {#if hasChanges}
      <div class="save-section">
        <button
          class="btn-save"
          on:click={saveSettings}
          disabled={saving}
        >
          {saving ? $t('autoLabel.saving') : $t('autoLabel.saveSettings')}
        </button>
      </div>
    {/if}

    <!-- Retroactive Apply Section -->
    <div class="retroactive-section">
      <div class="setting-info">
        <span class="setting-label">{$t('autoLabel.retroactiveLabel')}</span>
        <span class="setting-help">{$t('autoLabel.retroactiveHelp')}</span>
      </div>
      <button
        class="btn-retroactive"
        on:click={triggerRetroactiveApply}
        disabled={retroactiveRunning || !enabled}
      >
        {$t('autoLabel.retroactiveButton')}
      </button>

      {#if retroactiveRunning}
        <div class="progress-section">
          {#if retroactiveTotal === 0}
            <div class="queued-message">
              {retroactiveMessage || $t('autoLabel.retroactiveQueued')}
            </div>
            <ProgressBar percent={null} />
          {:else}
            <div class="progress-info">
              <span class="progress-label">
                {retroactiveProcessed} / {retroactiveTotal}
              </span>
              <span class="progress-percent">
                {Math.round((retroactiveProcessed / Math.max(retroactiveTotal, 1)) * 100)}%
                {#if formatEta(retroactiveEtaSeconds)}
                  ({formatEta(retroactiveEtaSeconds)} {$t('common.remaining')})
                {/if}
              </span>
            </div>
            <div class="progress-bar-container">
              <div
                class="progress-bar-fill"
                style="width: {(retroactiveProcessed / Math.max(retroactiveTotal, 1)) * 100}%"
              ></div>
            </div>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .auto-label-settings {
    padding: 1rem 0;
  }

  .settings-group {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .setting-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .setting-info {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    flex: 1;
    min-width: 0;
  }

  .setting-label {
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-primary);
  }

  .setting-help {
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.3;
  }

  /* Toggle Switch */
  .toggle-switch {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 24px;
    flex-shrink: 0;
    margin-left: 1rem;
  }

  .toggle-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: absolute;
    cursor: pointer;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: var(--border-color);
    transition: 0.3s;
    border-radius: 24px;
  }

  .toggle-slider:before {
    position: absolute;
    content: "";
    height: 18px;
    width: 18px;
    left: 3px;
    bottom: 3px;
    background-color: var(--toggle-button, white);
    transition: 0.3s;
    border-radius: 50%;
  }

  .toggle-switch input:focus-visible + .toggle-slider {
    outline: 2px solid var(--primary-color, #3b82f6);
    outline-offset: 2px;
  }

  .toggle-switch input:checked + .toggle-slider {
    background-color: #3b82f6;
  }

  .toggle-switch input:checked + .toggle-slider:before {
    transform: translateX(20px);
  }

  /* Slider */
  .slider-container {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-shrink: 0;
    margin-left: 1rem;
  }

  .threshold-slider {
    width: 120px;
    height: 4px;
    -webkit-appearance: none;
    appearance: none;
    border-radius: 2px;
    background: var(--border-color);
    outline: none;
  }

  .threshold-slider::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: #3b82f6;
    cursor: pointer;
  }

  .threshold-slider::-moz-range-thumb {
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: #3b82f6;
    cursor: pointer;
    border: none;
  }

  .threshold-value {
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--primary-color);
    min-width: 40px;
    text-align: right;
  }

  /* Save Section */
  .save-section {
    margin-top: 1rem;
    display: flex;
    justify-content: flex-end;
  }

  .btn-save {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    background: #3b82f6;
    color: white;
    font-weight: 500;
    font-size: 0.95rem;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-save:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-save:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-save:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Retroactive Section */
  .retroactive-section {
    margin-top: 1.5rem;
    padding: 1rem;
    border-radius: 8px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .btn-retroactive {
    margin-top: 0.75rem;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    background: #3b82f6;
    color: white;
    font-weight: 500;
    font-size: 0.9rem;
    cursor: pointer;
    transition: all 0.2s ease;
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-retroactive:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-retroactive:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-retroactive:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Progress Section */
  .progress-section {
    margin-top: 0.75rem;
  }

  .queued-message {
    font-size: 0.8rem;
    color: var(--text-secondary, #999);
    margin-bottom: 0.5rem;
  }

  .progress-info {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .progress-label {
    font-size: 0.8rem;
    color: var(--text-primary);
    font-weight: 500;
  }

  .progress-percent {
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .progress-bar-container {
    height: 8px;
    background: var(--border-color, #444);
    border-radius: 4px;
    overflow: hidden;
  }

  .progress-bar-fill {
    height: 100%;
    background: var(--primary-color, #4a9eff);
    border-radius: 4px;
    transition: width 0.3s ease;
  }

  /* Skeleton Loading */
  .skeleton-rows {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    padding: 0.5rem 0;
  }

  .skeleton-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .skeleton-text {
    height: 14px;
    width: 140px;
    border-radius: 4px;
    background: var(--border-color, #e5e7eb);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-text.wide {
    width: 200px;
  }

  .skeleton-toggle {
    height: 24px;
    width: 44px;
    border-radius: 12px;
    background: var(--border-color, #e5e7eb);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  @media (max-width: 768px) {
    .setting-row {
      flex-direction: column;
      align-items: flex-start;
      gap: 0.75rem;
    }

    .toggle-switch {
      margin-left: 0;
    }

    .slider-container {
      margin-left: 0;
      width: 100%;
    }

    .threshold-slider {
      flex: 1;
      width: auto;
    }

    .btn-save,
    .btn-retroactive {
      width: 100%;
      min-height: 44px;
    }
  }
</style>
