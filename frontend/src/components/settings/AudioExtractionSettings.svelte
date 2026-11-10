<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
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
      <div class="form-field">
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
              <FormField id="threshold" label={$t('settings.audioExtraction.threshold')} labelSize="md" hint={$t('settings.audioExtraction.thresholdInfo', { size: extractionThresholdMb })} let:describedby>
                <span class="label-hint" slot="label-inline">{$t('settings.audioExtraction.thresholdHint')}</span>
              <div class="threshold-input-group">
                <input
                  id="threshold"
                  aria-describedby={describedby}
                  type="number"
                  min="1"
                  max="10000"
                  bind:value={extractionThresholdMb}
                  class="form-input threshold-input"
                />
                <span class="threshold-unit" dir="ltr">MB</span>
              </div>
              </FormField>
            </div>
          {/if}
        </div>
      </div>

      {#if autoExtractEnabled}
        <!-- Show Modal -->
        <div class="form-field">
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
        <div class="form-field">
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
          class="btn btn-primary save-button"
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


  .form-actions {
    display: flex;
    justify-content: flex-end;
    padding-top: var(--space-2);
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

  }
</style>
