<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import InfoIcon from '$components/ui/InfoIcon.svelte';
  import { onMount } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { getAudioExtractionSettings, updateAudioExtractionSettings } from '$lib/api/audioExtractionSettings';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  // Settings state
  let autoExtractEnabled = true;
  let extractionThresholdMb = 100;
  let rememberChoice = false;
  let showModal = true;
  let loading = false;
  let saving = false;

  // Original values for change tracking
  let originalAutoExtractEnabled = true;
  let originalExtractionThresholdMb = 100;
  let originalRememberChoice = false;
  let originalShowModal = true;

  // Track if settings have changed
  $: settingsChanged =
    autoExtractEnabled !== originalAutoExtractEnabled ||
    extractionThresholdMb !== originalExtractionThresholdMb ||
    rememberChoice !== originalRememberChoice ||
    showModal !== originalShowModal;

  onMount(async () => {
    await loadSettings();
  });

  async function loadSettings() {
    loading = true;
    try {
      const settings = await getAudioExtractionSettings();
      autoExtractEnabled = settings.auto_extract_enabled;
      extractionThresholdMb = settings.extraction_threshold_mb;
      rememberChoice = settings.remember_choice;
      showModal = settings.show_modal;

      // Store original values for change tracking
      originalAutoExtractEnabled = settings.auto_extract_enabled;
      originalExtractionThresholdMb = settings.extraction_threshold_mb;
      originalRememberChoice = settings.remember_choice;
      originalShowModal = settings.show_modal;
    } catch (err) {
      console.error('Failed to load audio extraction settings:', err);
      toastStore.error($t('settings.audioExtraction.loadFailed'));
    } finally {
      loading = false;
    }
  }

  async function saveSettings() {
    saving = true;
    try {
      await updateAudioExtractionSettings({
        auto_extract_enabled: autoExtractEnabled,
        extraction_threshold_mb: extractionThresholdMb,
        remember_choice: rememberChoice,
        show_modal: showModal,
      });

      // Update original values after successful save
      originalAutoExtractEnabled = autoExtractEnabled;
      originalExtractionThresholdMb = extractionThresholdMb;
      originalRememberChoice = rememberChoice;
      originalShowModal = showModal;

      toastStore.success($t('settings.audioExtraction.saved'));
    } catch (err) {
      console.error('Failed to save audio extraction settings:', err);
      toastStore.error($t('settings.audioExtraction.saveFailed'));
    } finally {
      saving = false;
    }
  }
</script>

<div class="audio-extraction-settings">
  {#if loading}
    <div class="loading-state">
      <Spinner size="large" />
      <p>{$t('settings.audioExtraction.loading')}</p>
    </div>
  {:else}
    <div class="settings-form">
      <!-- Auto Extract Enabled -->
      <div class="form-group">
        <div class="toggle-group">
          <div class="toggle-header">
            <label for="auto-extract" class="toggle-label">
              <span class="label-text">{$t('settings.audioExtraction.enableExtraction')}</span>
              <span class="label-description">
                {$t('settings.audioExtraction.enableExtractionDesc')}
              </span>
            </label>
            <Toggle id="auto-extract" checked={autoExtractEnabled} onchange={(next) => (autoExtractEnabled = next)} />
          </div>
          {#if autoExtractEnabled}
            <!-- Depends on the switch above, so it sits directly beneath it (label, then control). -->
            <div class="dependent-field" data-testid="threshold-field">
              <label for="threshold" class="form-label">
                {$t('settings.audioExtraction.threshold')}
                <span class="label-hint">{$t('settings.audioExtraction.thresholdHint')}</span>
              </label>
              <div class="threshold-input-group">
                <input
                  id="threshold"
                  type="number"
                  min="1"
                  max="10000"
                  bind:value={extractionThresholdMb}
                  class="form-input threshold-input"
                />
                <span class="threshold-unit" dir="ltr">MB</span>
              </div>
              <p class="input-hint">
                {$t('settings.audioExtraction.thresholdInfo', { size: extractionThresholdMb })}
              </p>
            </div>
          {/if}
        </div>
      </div>

      {#if autoExtractEnabled}
        <!-- Show Modal -->
        <div class="form-group">
          <div class="toggle-group">
            <div class="toggle-header">
              <label for="show-modal" class="toggle-label">
                <span class="label-text">{$t('settings.audioExtraction.showDialog')}</span>
                <span class="label-description">
                  {$t('settings.audioExtraction.showDialogDesc')}
                </span>
              </label>
              <Toggle id="show-modal" checked={showModal} onchange={(next) => (showModal = next)} />
            </div>
          </div>
        </div>

        <!-- Remember Choice -->
        <div class="form-group">
          <div class="toggle-group">
            <div class="toggle-header">
              <label for="remember-choice" class="toggle-label">
                <span class="label-text">{$t('settings.audioExtraction.rememberChoice')}</span>
                <span class="label-description">
                  {$t('settings.audioExtraction.rememberChoiceDesc')}
                </span>
              </label>
              <Toggle id="remember-choice" checked={rememberChoice} onchange={(next) => (rememberChoice = next)} />
            </div>
          </div>
        </div>
      {/if}

      <!-- Save Button -->
      <div class="form-actions">
        <button
          class="save-button"
          on:click={saveSettings}
          disabled={saving || !settingsChanged}
        >
          {#if saving}
            <Spinner size="small" color="white" />
            {$t('settings.audioExtraction.saving')}
          {:else}
            {$t('settings.audioExtraction.saveSettings')}
          {/if}
        </button>
      </div>

      <!-- Info Box -->
      <div class="info-box">
        <div class="info-icon">
          <InfoIcon size={20} />
        </div>
        <div class="info-content">
          <p><strong>{$t('settings.audioExtraction.howItWorks')}</strong></p>
          <ul>
            <li>{$t('settings.audioExtraction.howItWorks1')}</li>
            <li>{$t('settings.audioExtraction.howItWorks2')}</li>
            <li>{$t('settings.audioExtraction.howItWorks3')}</li>
            <li>{$t('settings.audioExtraction.howItWorks4')}</li>
            <li>{$t('settings.audioExtraction.howItWorks5')}</li>
          </ul>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .audio-extraction-settings {
    max-width: 800px;
    padding: var(--space-6) 0;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-12);
    gap: var(--space-4);
    color: var(--text-secondary);
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .form-label {
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .label-hint {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-regular);
    color: var(--text-secondary);
  }

  .toggle-group {
    padding: var(--space-4);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    transition: all var(--duration-base) ease;
  }

  .toggle-group:hover {
    border-color: var(--primary-color);
  }

  .toggle-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-4);
  }

  .toggle-label {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    cursor: pointer;
  }

  .label-text {
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .label-description {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .dependent-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin-top: var(--space-4);
    padding-top: var(--space-4);
    padding-inline-start: var(--space-4);
    border-top: 1px solid var(--border-color);
  }

  .threshold-input-group {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .threshold-input {
    width: 120px;
  }

  .threshold-unit {
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .form-input {
    padding: var(--space-3);
    font-size: var(--font-size-sm);
  }

  .input-hint {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    padding-top: var(--space-2);
  }

  .save-button {
    padding: var(--space-3) var(--space-8);
    background: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    border-radius: var(--radius-xl);
    font-weight: var(--font-weight-medium);
    cursor: pointer;
    transition: all var(--duration-base) ease;
    display: flex;
    align-items: center;
    gap: var(--space-2);
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .save-button:hover:not(:disabled) {
    background: var(--primary-color);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.3);
  }

  .save-button:active:not(:disabled) {
    transform: scale(1);
  }

  .save-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .info-box {
    display: flex;
    gap: var(--space-4);
    padding: var(--space-4);
    background: rgba(var(--primary-color-rgb), 0.05);
    border: 1px solid rgba(var(--primary-color-rgb), 0.2);
    border-radius: var(--radius-lg);
    margin-top: var(--space-4);
  }

  .info-icon {
    flex-shrink: 0;
    color: var(--primary-on-surface);
  }

  .info-content {
    flex: 1;
  }

  .info-content p {
    margin: 0 0 var(--space-2) 0;
    color: var(--text-color);
  }

  .info-content ul {
    margin: 0;
    padding-left: var(--space-5);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.6;
  }

  .info-content li {
    margin-bottom: var(--space-1);
  }

  /* Responsive */
  @media (max-width: 640px) {
    .audio-extraction-settings {
      padding: var(--space-4) 0;
    }

    .toggle-header {
      flex-direction: column;
    }

    .threshold-input {
      width: 100%;
    }

    .info-box {
      flex-direction: column;
    }

    .save-button {
      width: 100%;
      min-height: 44px;
      justify-content: center;
    }
  }
</style>
