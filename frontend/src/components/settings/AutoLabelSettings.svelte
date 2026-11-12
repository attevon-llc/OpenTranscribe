<script lang="ts">
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount, onDestroy } from 'svelte';
  import Toggle from '$components/ui/Toggle.svelte';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { llmStatusStore } from '$stores/llmStatus';
  import LiveProgressCard from '$components/ui/LiveProgressCard.svelte';
  import RangeSlider from '$components/ui/RangeSlider.svelte';

  let autoTopicExtraction = true;
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
  let hasChanges: boolean;

  // Original values for change tracking
  let originalAutoTopicExtraction = true;
  let originalEnabled = true;
  let originalThreshold = 0.75;
  let originalTagsEnabled = true;
  let originalCollectionsEnabled = true;
  let originalBulkGroupingEnabled = true;

  // Known only after the first status check; until then the switch is not disabled, so it
  // does not flash greyed-out for users who do have a provider.
  $: noProvider =
    !$llmStatusStore.checking && $llmStatusStore.lastChecked !== null && !$llmStatusStore.available;

  $: hasChanges = autoTopicExtraction !== originalAutoTopicExtraction ||
    enabled !== originalEnabled ||
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
      autoTopicExtraction = data.auto_topic_extraction ?? true;
      enabled = data.enabled ?? true;
      confidenceThreshold = data.confidence_threshold ?? 0.75;
      tagsEnabled = data.tags_enabled ?? true;
      collectionsEnabled = data.collections_enabled ?? true;
      bulkGroupingEnabled = data.bulk_grouping_enabled ?? true;

      originalAutoTopicExtraction = autoTopicExtraction;
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
        auto_topic_extraction: autoTopicExtraction,
        enabled,
        confidence_threshold: confidenceThreshold,
        tags_enabled: tagsEnabled,
        collections_enabled: collectionsEnabled,
        bulk_grouping_enabled: bulkGroupingEnabled,
      });

      const data = response.data;
      originalAutoTopicExtraction = data.auto_topic_extraction ?? autoTopicExtraction;
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
      toastStore.error(getErrorMessage(err, $t('autoLabel.retroactiveFailed')));
      retroactiveRunning = false;
    }
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
    void llmStatusStore.initialize();
    void loadSettings();
    window.addEventListener('auto-label-status', handleAutoLabelStatus as EventListener);
  });

  onDestroy(() => {
    window.removeEventListener('auto-label-status', handleAutoLabelStatus as EventListener);
  });
</script>

<div class="auto-label-settings">
  {#if loading}
    <SettingsSkeleton variant="rows" count={3} label={$t('common.loading')} />
  {:else}
    <div class="settings-group">
      <!-- Automatic AI suggestions after transcription (sends transcript text to the AI provider) -->
      <div class="setting-row">
        <div class="setting-info">
          <label for="auto-topic-extraction" class="setting-label">{$t('autoLabel.autoSuggestLabel')}</label>
          <span class="setting-help">{$t('autoLabel.autoSuggestHelp')}</span>
          {#if noProvider}
            <span class="setting-note" data-testid="auto-suggest-no-provider">
              {$t('autoLabel.autoSuggestNoProvider')}
            </span>
          {/if}
        </div>
        <Toggle id="auto-topic-extraction" checked={autoTopicExtraction} onchange={(next) => (autoTopicExtraction = next)} disabled={noProvider} />
      </div>

      <!-- Master Toggle -->
      <div class="setting-row">
        <div class="setting-info">
          <label for="auto-label-enabled" class="setting-label">{$t('autoLabel.enableLabel')}</label>
          <span class="setting-help">{$t('autoLabel.enableHelp')}</span>
        </div>
        <Toggle id="auto-label-enabled" checked={enabled} onchange={(next) => (enabled = next)} />
      </div>

      {#if enabled}
        <!-- Confidence Threshold -->
        <div class="setting-row setting-row--slider">
          <RangeSlider
            id="confidence-threshold"
            label={$t('autoLabel.thresholdLabel')}
            hint={$t('autoLabel.thresholdHelp')}
            min={0.5}
            max={1.0}
            step={0.05}
            format={(v) => `${Math.round(v * 100)}%`}
            bind:value={confidenceThreshold}
          />
        </div>

        <!-- Tags Toggle -->
        <div class="setting-row">
          <div class="setting-info">
            <label for="auto-label-tags" class="setting-label">{$t('autoLabel.tagsLabel')}</label>
            <span class="setting-help">{$t('autoLabel.tagsHelp')}</span>
          </div>
          <Toggle id="auto-label-tags" checked={tagsEnabled} onchange={(next) => (tagsEnabled = next)} />
        </div>

        <!-- Collections Toggle -->
        <div class="setting-row">
          <div class="setting-info">
            <label for="auto-label-collections" class="setting-label">{$t('autoLabel.collectionsLabel')}</label>
            <span class="setting-help">{$t('autoLabel.collectionsHelp')}</span>
          </div>
          <Toggle id="auto-label-collections" checked={collectionsEnabled} onchange={(next) => (collectionsEnabled = next)} />
        </div>

        <!-- Bulk Grouping Toggle -->
        <div class="setting-row">
          <div class="setting-info">
            <label for="auto-label-bulk" class="setting-label">{$t('autoLabel.bulkGroupingLabel')}</label>
            <span class="setting-help">{$t('autoLabel.bulkGroupingHelp')}</span>
          </div>
          <Toggle id="auto-label-bulk" checked={bulkGroupingEnabled} onchange={(next) => (bulkGroupingEnabled = next)} />
        </div>
      {/if}
    </div>

    <!-- Save Button -->
    {#if hasChanges}
      <div class="save-section">
        <button
          class="btn btn-primary btn-lg btn-save"
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
        class="btn btn-primary btn-lg btn-retroactive"
        on:click={triggerRetroactiveApply}
        disabled={retroactiveRunning || !enabled}
      >
        {$t('autoLabel.retroactiveButton')}
      </button>

      {#if retroactiveRunning}
        <div class="progress-section">
          <LiveProgressCard
            state={retroactiveTotal === 0 ? 'starting' : 'running'}
            done={retroactiveProcessed}
            total={retroactiveTotal}
            percent={retroactiveTotal === 0 ? null : (retroactiveProcessed / retroactiveTotal) * 100}
            etaSeconds={retroactiveEtaSeconds}
            remainingLabel={$t('common.remaining')}
            message={retroactiveTotal === 0 ? retroactiveMessage || $t('autoLabel.retroactiveQueued') : ''}
            barLabel={$t('autoLabel.progressBar')}
          />
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .auto-label-settings {
    padding: var(--space-4) 0;
  }

  .settings-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .setting-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: var(--space-3) var(--space-4);
    border-radius: var(--radius-lg);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .setting-info {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    flex: 1;
    min-width: 0;
  }

  .setting-label {
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .setting-help {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.3;
  }

  .setting-note {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.3;
  }

  .setting-row--slider {
    display: block;
  }

  /* Save Section */
  .save-section {
    margin-top: var(--space-4);
    display: flex;
    justify-content: flex-end;
  }

  /* Retroactive Section */
  .retroactive-section {
    margin-top: var(--space-6);
    padding: var(--space-4);
    border-radius: var(--radius-lg);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .btn-retroactive {
    margin-top: var(--space-3);
  }

  /* Progress Section */
  .progress-section {
    margin-top: var(--space-3);
  }

  /* Skeleton Loading */

  @media (max-width: 768px) {
    .setting-row {
      flex-direction: column;
      align-items: flex-start;
      gap: var(--space-3);
    }

    .btn-save,
    .btn-retroactive {
      width: 100%;
      min-height: 44px;
    }
  }
</style>
