<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import {
    getTranscriptionSettings,
    updateTranscriptionSettings,
    resetTranscriptionSettings,
    getTranscriptionSystemDefaults,
    type TranscriptionSettings,
    type TranscriptionSettingsUpdate,
    type TranscriptionSystemDefaults
  } from '$lib/api/transcriptionSettings';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import Spinner from '../ui/Spinner.svelte';
  import SettingsCard from './transcription/SettingsCard.svelte';
  import FormActions from './transcription/FormActions.svelte';
  import AccuracyAdvancedCards from './transcription/AccuracyAdvancedCards.svelte';
  import './transcription/formLayout.css';
  import './transcription/formControls.css';

  const dispatch = createEventDispatcher();

  let garbageCleanupEnabled = true;
  let garbageCleanupThreshold = 50;
  let vadThreshold = 0.5;
  let vadMinSilenceMs = 2000;
  let vadMinSpeechMs = 250;
  let vadSpeechPadMs = 400;
  let hallucinationEnabled = false;
  let hallucinationValue = 2.0;
  let repetitionPenalty = 1.0;

  let originalGarbageCleanupEnabled = true;
  let originalGarbageCleanupThreshold = 50;
  let originalVadThreshold = 0.5;
  let originalVadMinSilenceMs = 2000;
  let originalVadMinSpeechMs = 250;
  let originalVadSpeechPadMs = 400;
  let originalHallucinationEnabled = false;
  let originalHallucinationValue = 2.0;
  let originalRepetitionPenalty = 1.0;

  // Deployment-owned: hidden here, and the server ignores writes to them.
  $: advancedEnabled = isCapabilityEnabled($capabilities, 'transcription.advanced');

  let systemDefaults: TranscriptionSystemDefaults | null = null;
  let loading = true;
  let saving = false;
  let resetting = false;
  let validationError = '';

  $: hallucinationSilenceThreshold = hallucinationEnabled ? hallucinationValue : null;

  $: settingsChanged =
    garbageCleanupEnabled !== originalGarbageCleanupEnabled ||
    garbageCleanupThreshold !== originalGarbageCleanupThreshold ||
    vadThreshold !== originalVadThreshold ||
    vadMinSilenceMs !== originalVadMinSilenceMs ||
    vadMinSpeechMs !== originalVadMinSpeechMs ||
    vadSpeechPadMs !== originalVadSpeechPadMs ||
    hallucinationEnabled !== originalHallucinationEnabled ||
    (hallucinationEnabled && hallucinationValue !== originalHallucinationValue) ||
    repetitionPenalty !== originalRepetitionPenalty;

  $: dispatch('change', { hasChanges: settingsChanged });

  // UX-only range checks; the backend stays the authority.
  $: {
    if (garbageCleanupThreshold < 20 || garbageCleanupThreshold > 200) {
      validationError = $t('settings.transcription.validationThresholdRange');
    } else if (vadThreshold < 0.1 || vadThreshold > 0.95) {
      validationError = $t('settings.transcription.validationVadThreshold');
    } else if (vadMinSilenceMs < 100 || vadMinSilenceMs > 5000) {
      validationError = $t('settings.transcription.validationVadSilence');
    } else if (vadMinSpeechMs < 50 || vadMinSpeechMs > 5000) {
      validationError = $t('settings.transcription.validationVadSpeech');
    } else if (vadSpeechPadMs < 0 || vadSpeechPadMs > 2000) {
      validationError = $t('settings.transcription.validationVadPad');
    } else if (hallucinationEnabled && (hallucinationValue < 0.5 || hallucinationValue > 10.0)) {
      validationError = $t('settings.transcription.validationHallucination');
    } else if (repetitionPenalty < 1.0 || repetitionPenalty > 2.0) {
      validationError = $t('settings.transcription.validationRepetition');
    } else {
      validationError = '';
    }
  }

  onMount(() => {
    void Promise.all([loadSettings(), loadSystemDefaults()]);
  });

  async function loadSettings() {
    loading = true;
    try {
      applySettings(await getTranscriptionSettings());
    } catch (err) {
      console.error('Failed to load transcription settings:', err);
      toastStore.error($t('settings.transcription.loadFailed'));
    } finally {
      loading = false;
    }
  }

  async function loadSystemDefaults() {
    try {
      systemDefaults = await getTranscriptionSystemDefaults();
    } catch (err) {
      console.error('Failed to load system defaults:', err);
    }
  }

  function applySettings(settings: TranscriptionSettings) {
    garbageCleanupEnabled = originalGarbageCleanupEnabled = settings.garbage_cleanup_enabled;
    garbageCleanupThreshold = originalGarbageCleanupThreshold = settings.garbage_cleanup_threshold;
    vadThreshold = originalVadThreshold = settings.vad_threshold;
    vadMinSilenceMs = originalVadMinSilenceMs = settings.vad_min_silence_ms;
    vadMinSpeechMs = originalVadMinSpeechMs = settings.vad_min_speech_ms;
    vadSpeechPadMs = originalVadSpeechPadMs = settings.vad_speech_pad_ms;
    hallucinationEnabled = originalHallucinationEnabled =
      settings.hallucination_silence_threshold !== null;
    hallucinationValue = originalHallucinationValue =
      settings.hallucination_silence_threshold ?? 2.0;
    repetitionPenalty = originalRepetitionPenalty = settings.repetition_penalty;
  }

  async function saveSettings() {
    if (validationError) {
      toastStore.error(validationError);
      return;
    }
    saving = true;
    try {
      const update: TranscriptionSettingsUpdate = {
        garbage_cleanup_enabled: garbageCleanupEnabled,
        garbage_cleanup_threshold: garbageCleanupThreshold
      };
      if (advancedEnabled) {
        Object.assign(update, {
          vad_threshold: vadThreshold,
          vad_min_silence_ms: vadMinSilenceMs,
          vad_min_speech_ms: vadMinSpeechMs,
          vad_speech_pad_ms: vadSpeechPadMs,
          hallucination_silence_threshold: hallucinationSilenceThreshold,
          repetition_penalty: repetitionPenalty
        });
      }
      applySettings(await updateTranscriptionSettings(update));
      toastStore.success($t('settings.transcription.accuracySaved'));
      dispatch('save');
    } catch (err) {
      console.error('Failed to save transcription settings:', err);
      toastStore.error($t('settings.transcription.saveFailed'));
    } finally {
      saving = false;
    }
  }

  async function resetToDefaults() {
    resetting = true;
    try {
      const response = await resetTranscriptionSettings('accuracy');
      applySettings(response.default_settings);
      toastStore.success($t('settings.transcription.resetSuccess'));
      dispatch('reset');
    } catch (err) {
      console.error('Failed to reset transcription settings:', err);
      toastStore.error($t('settings.transcription.resetFailed'));
    } finally {
      resetting = false;
    }
  }
</script>

<div class="transcription-accuracy-settings tx-form">
  {#if loading}
    <div class="loading-state">
      <Spinner size="large" />
      <p>{$t('settings.transcription.loading')}</p>
    </div>
  {:else}
    <div class="settings-form">
      <p class="section-desc">{$t('settings.transcription.accuracyTabDesc')}</p>

      <SettingsCard
        title={$t('settings.transcription.garbageCleanup')}
        description={$t('settings.transcription.garbageCleanupDesc')}
        tooltip={$t('settings.transcription.garbageCleanupTooltip')}
      >
        <div class="setting-row">
          <div class="setting-controls">
            <label class="toggle-label">
              <input type="checkbox" bind:checked={garbageCleanupEnabled} class="toggle-input" />
              <span class="toggle-switch"></span>
              <span class="toggle-text">{$t('settings.transcription.enableCleanup')}</span>
            </label>
            <div class="inline-input">
              <span class="input-label">{$t('settings.transcription.threshold')}</span>
              <input
                type="number"
                bind:value={garbageCleanupThreshold}
                min="20"
                max="200"
                class="form-input number-input"
                disabled={!garbageCleanupEnabled}
              />
              <span class="input-suffix">{$t('settings.transcription.chars')}</span>
            </div>
          </div>
        </div>

        {#if systemDefaults}
          <div class="defaults-info">
            <span class="defaults-label">{$t('settings.transcription.systemDefaultThreshold')}</span>
            <span class="defaults-value">
              {systemDefaults.garbage_cleanup_threshold}
              {$t('settings.transcription.chars')}
            </span>
          </div>
        {/if}
      </SettingsCard>

      {#if advancedEnabled}
        <AccuracyAdvancedCards
          bind:vadThreshold
          bind:vadMinSilenceMs
          bind:vadMinSpeechMs
          bind:vadSpeechPadMs
          bind:hallucinationEnabled
          bind:hallucinationValue
          bind:repetitionPenalty
          {systemDefaults}
        />
      {/if}

      <FormActions
        {validationError}
        {saving}
        {resetting}
        dirty={settingsChanged}
        on:save={saveSettings}
        on:reset={resetToDefaults}
      />
    </div>
  {/if}
</div>
