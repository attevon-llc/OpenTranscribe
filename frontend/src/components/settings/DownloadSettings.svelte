<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import { onMount, createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import Spinner from '$components/ui/Spinner.svelte';
  import {
    getDownloadSettings,
    updateDownloadSettings,
    resetDownloadSettings,
    getDownloadSystemDefaults,
    DEFAULT_DOWNLOAD_SETTINGS,
    type DownloadSettings,
    type DownloadSystemDefaults
  } from '$lib/api/downloadSettings';

  const dispatch = createEventDispatcher();

  // State
  let loading = true;
  let saving = false;
  let resetting = false;

  // Current values
  let videoQuality = DEFAULT_DOWNLOAD_SETTINGS.video_quality;
  let audioOnly = DEFAULT_DOWNLOAD_SETTINGS.audio_only;
  let audioQuality = DEFAULT_DOWNLOAD_SETTINGS.audio_quality;

  // Original values for change detection
  let originalVideoQuality = videoQuality;
  let originalAudioOnly = audioOnly;
  let originalAudioQuality = audioQuality;

  // System defaults
  let systemDefaults: DownloadSystemDefaults | null = null;

  // Change detection
  $: settingsChanged =
    videoQuality !== originalVideoQuality ||
    audioOnly !== originalAudioOnly ||
    audioQuality !== originalAudioQuality;

  $: settingsModalStore.setDirty('download', settingsChanged);

  function applySettings(settings: DownloadSettings) {
    videoQuality = settings.video_quality;
    audioOnly = settings.audio_only;
    audioQuality = settings.audio_quality;
    storeOriginalValues();
  }

  function storeOriginalValues() {
    originalVideoQuality = videoQuality;
    originalAudioOnly = audioOnly;
    originalAudioQuality = audioQuality;
  }

  onMount(async () => {
    try {
      const [settings, defaults] = await Promise.all([
        getDownloadSettings(),
        getDownloadSystemDefaults()
      ]);
      applySettings(settings);
      systemDefaults = defaults;
    } catch (err) {
      console.error('Failed to load download settings:', err);
      toastStore.error($t('settings.download.loadFailed'));
    } finally {
      loading = false;
    }
  });

  async function saveSettings() {
    saving = true;
    try {
      const updated = await updateDownloadSettings({
        video_quality: videoQuality,
        audio_only: audioOnly,
        audio_quality: audioQuality,
      });
      applySettings(updated);
      settingsModalStore.clearDirty('download');
      toastStore.success($t('settings.download.saved'));
      dispatch('save');
    } catch (err) {
      console.error('Failed to save download settings:', err);
      toastStore.error($t('settings.download.saveFailed'));
    } finally {
      saving = false;
    }
  }

  async function resetToDefaults() {
    resetting = true;
    try {
      const result = await resetDownloadSettings();
      applySettings(result.default_settings);
      settingsModalStore.clearDirty('download');
      toastStore.success($t('settings.download.resetSuccess'));
      dispatch('reset');
    } catch (err) {
      console.error('Failed to reset download settings:', err);
      toastStore.error($t('settings.download.resetFailed'));
    } finally {
      resetting = false;
    }
  }
</script>

{#if loading}
  <div class="loading-state">
    <Spinner size="small" />
    {$t('settings.download.loading')}
  </div>
{:else}
  <div class="settings-form">
    <!-- Video Quality -->
    <div class="form-group">
      <div class="form-group-header">
        <label for="video-quality">{$t('settings.download.videoQuality')}</label>
      </div>
      <select
        id="video-quality"
        bind:value={videoQuality}
        disabled={audioOnly}
        class="form-select"
      >
        {#if systemDefaults}
          {#each Object.entries(systemDefaults.available_video_qualities) as [value, label] (value)}
            <option {value}>{label}</option>
          {/each}
        {:else}
          <option value="best">{$t('settings.download.bestAvailable')}</option>
        {/if}
      </select>
      {#if audioOnly}
        <p class="form-hint">{$t('settings.download.videoQualityDisabledHint')}</p>
      {:else}
        <p class="form-hint">{$t('settings.download.videoQualityHint')}</p>
      {/if}
    </div>

    <!-- Audio Only Toggle -->
    <div class="form-group">
      <div class="toggle-row">
        <label for="audio-only" class="toggle-label">{$t('settings.download.audioOnly')}</label>
        <Toggle id="audio-only" checked={audioOnly} onchange={(next) => (audioOnly = next)} />
      </div>
      <p class="form-hint">{$t('settings.download.audioOnlyHint')}</p>
    </div>

    <!-- Audio Quality -->
    {#if audioOnly}
      <div class="form-group">
        <div class="form-group-header">
          <label for="audio-quality">{$t('settings.download.audioQuality')}</label>
        </div>
        <select
          id="audio-quality"
          bind:value={audioQuality}
          class="form-select"
        >
          {#if systemDefaults}
            {#each Object.entries(systemDefaults.available_audio_qualities) as [value, label] (value)}
              <option {value}>{label}</option>
            {/each}
          {:else}
            <option value="best">{$t('settings.download.bestAvailable')}</option>
          {/if}
        </select>
        <p class="form-hint">{$t('settings.download.audioQualityHint')}</p>
      </div>
    {/if}

    <!-- Action Buttons -->
    <div class="form-actions">
      <button
        class="btn btn-secondary"
        on:click={resetToDefaults}
        disabled={resetting || saving}
      >
        {resetting ? $t('settings.download.resetting') : $t('settings.download.resetToDefaults')}
      </button>
      <button
        class="btn btn-primary"
        on:click={saveSettings}
        disabled={!settingsChanged || saving}
      >
        {saving ? $t('settings.download.saving') : $t('settings.download.saveSettings')}
      </button>
    </div>
  </div>
{/if}

<style>
  .loading-state {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-4) 0;
    color: var(--text-secondary);
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .form-group-header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .form-group-header label,
  .toggle-label {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .form-select {
    width: 100%;
    padding-right: var(--space-10);
    cursor: pointer;
  }

  .form-select:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .form-hint {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin: 0;
  }

  .toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-color);
  }

  .form-actions .btn-secondary {
    margin-right: auto;
  }

  @media (max-width: 768px) {
    .form-actions {
      flex-direction: column-reverse;
    }

    .form-actions .btn-secondary {
      margin-right: 0;
    }

    .form-actions .btn {
      width: 100%;
      min-height: 44px;
      text-align: center;
    }
  }
</style>
