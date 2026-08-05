<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import {
    getTranscriptionSettings,
    updateTranscriptionSettings,
    resetTranscriptionSettings,
    getTranscriptionSystemDefaults,
    getSpeakerBehaviorLabel,
    getSpeakerBehaviorDescription,
    groupLanguages,
    type TranscriptionSettings,
    type TranscriptionSystemDefaults,
    type SpeakerPromptBehavior,
    type LanguageOption
  } from '$lib/api/transcriptionSettings';
  import { ASRSettingsApi, type ASRModelCapabilities } from '$lib/api/asrSettings';
  import { toastStore } from '$stores/toast';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { t } from '$stores/locale';
  import Spinner from '../ui/Spinner.svelte';

  const dispatch = createEventDispatcher();

  // Settings state
  let minSpeakers = 1;
  let maxSpeakers = 20;
  let speakerBehavior: SpeakerPromptBehavior = 'always_prompt';
  let garbageCleanupEnabled = true;
  let garbageCleanupThreshold = 50;
  let sourceLanguage = 'auto';
  let translateToEnglish = false;
  let llmOutputLanguage = 'en';
  let vadThreshold = 0.5;
  let vadMinSilenceMs = 2000;
  let vadMinSpeechMs = 250;
  let vadSpeechPadMs = 400;
  let hallucinationSilenceThreshold: number | null = null;
  let hallucinationEnabled = false;
  let hallucinationValue = 2.0;
  let repetitionPenalty = 1.0;
  let diarizationSource: import('$lib/api/transcriptionSettings').DiarizationSource = 'provider';

  // Original values for change tracking
  let originalMinSpeakers = 1;
  let originalMaxSpeakers = 20;
  let originalSpeakerBehavior: SpeakerPromptBehavior = 'always_prompt';
  let originalGarbageCleanupEnabled = true;
  let originalGarbageCleanupThreshold = 50;
  let originalSourceLanguage = 'auto';
  let originalTranslateToEnglish = false;
  let originalLlmOutputLanguage = 'en';
  let originalVadThreshold = 0.5;
  let originalVadMinSilenceMs = 2000;
  let originalVadMinSpeechMs = 250;
  let originalVadSpeechPadMs = 400;
  let originalHallucinationEnabled = false;
  let originalHallucinationValue = 2.0;
  let originalRepetitionPenalty = 1.0;
  let originalDiarizationSource: import('$lib/api/transcriptionSettings').DiarizationSource = 'provider';

  // Advanced section collapsed state
  let advancedExpanded = false;

  // System defaults
  let systemDefaults: TranscriptionSystemDefaults | null = null;

  // Grouped languages for dropdowns
  let sourceLanguageGroups: { common: LanguageOption[]; other: LanguageOption[] } = { common: [], other: [] };
  let llmLanguageOptions: LanguageOption[] = [];
  // Loading states
  let loading = true;
  let saving = false;
  let resetting = false;

  // ASR model capabilities (for translation/language guards)
  let asrCapabilities: ASRModelCapabilities | null = null;
  $: translationDisabled = asrCapabilities ? !asrCapabilities.supports_translation : false;
  $: isEnglishOptimized = asrCapabilities?.language_support === 'english_optimized';
  // English-only models (e.g. CrisperWhisper, cloud single-language models). Keyed off
  // language_support first so local english_only models are covered even when the catalog
  // omits a numeric languages count; falls back to languages === 1 for cloud providers.
  $: isEnglishOnly =
    asrCapabilities?.language_support === 'english_only' || asrCapabilities?.languages === 1;
  $: isNonEnglishSelected = sourceLanguage !== 'auto' && sourceLanguage !== 'en';

  // Validation
  let validationError = '';

  // Sync hallucination toggle/value with the nullable threshold
  $: hallucinationSilenceThreshold = hallucinationEnabled ? hallucinationValue : null;

  // Track if settings have changed
  $: settingsChanged =
    minSpeakers !== originalMinSpeakers ||
    maxSpeakers !== originalMaxSpeakers ||
    speakerBehavior !== originalSpeakerBehavior ||
    garbageCleanupEnabled !== originalGarbageCleanupEnabled ||
    garbageCleanupThreshold !== originalGarbageCleanupThreshold ||
    sourceLanguage !== originalSourceLanguage ||
    translateToEnglish !== originalTranslateToEnglish ||
    llmOutputLanguage !== originalLlmOutputLanguage ||
    vadThreshold !== originalVadThreshold ||
    vadMinSilenceMs !== originalVadMinSilenceMs ||
    vadMinSpeechMs !== originalVadMinSpeechMs ||
    vadSpeechPadMs !== originalVadSpeechPadMs ||
    hallucinationEnabled !== originalHallucinationEnabled ||
    (hallucinationEnabled && hallucinationValue !== originalHallucinationValue) ||
    repetitionPenalty !== originalRepetitionPenalty ||
    diarizationSource !== originalDiarizationSource;

  // Update dirty state in store
  $: {
    settingsModalStore.setDirty('transcription', settingsChanged);
    dispatch('change', { hasChanges: settingsChanged });
  }

  // Validate all settings
  $: {
    if (minSpeakers > maxSpeakers) {
      validationError = $t('settings.transcription.validationMinMax');
    } else if (minSpeakers < 1 || minSpeakers > 50) {
      validationError = $t('settings.transcription.validationMinRange');
    } else if (maxSpeakers < 1 || maxSpeakers > 50) {
      validationError = $t('settings.transcription.validationMaxRange');
    } else if (garbageCleanupThreshold < 20 || garbageCleanupThreshold > 200) {
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

  const speakerBehaviorOptions: SpeakerPromptBehavior[] = [
    'always_prompt',
    'use_defaults',
    'use_custom'
  ];

  onMount(async () => {
    await Promise.all([loadSettings(), loadSystemDefaults(), loadASRCapabilities()]);
  });

  async function loadSettings() {
    loading = true;
    try {
      const settings = await getTranscriptionSettings();
      applySettings(settings);
      storeOriginalValues(settings);
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

      // Process language options for dropdowns
      if (systemDefaults) {
        sourceLanguageGroups = groupLanguages(
          systemDefaults.available_source_languages,
          systemDefaults.common_languages
        );

        // LLM output languages (flat list, no grouping needed)
        llmLanguageOptions = Object.entries(systemDefaults.available_llm_output_languages)
          .map(([code, name]) => ({ code, name }));

      }
    } catch (err) {
      console.error('Failed to load system defaults:', err);
      // Non-critical, don't show error
    }
  }

  async function loadASRCapabilities() {
    try {
      const status = await ASRSettingsApi.getStatus();
      asrCapabilities = status.active_model_capabilities ?? null;
    } catch (err) {
      console.error('Failed to load ASR capabilities:', err);
    }
  }

  function applySettings(settings: TranscriptionSettings) {
    minSpeakers = settings.min_speakers;
    maxSpeakers = settings.max_speakers;
    speakerBehavior = settings.speaker_prompt_behavior;
    garbageCleanupEnabled = settings.garbage_cleanup_enabled;
    garbageCleanupThreshold = settings.garbage_cleanup_threshold;
    sourceLanguage = settings.source_language;
    translateToEnglish = settings.translate_to_english;
    llmOutputLanguage = settings.llm_output_language;
    vadThreshold = settings.vad_threshold;
    vadMinSilenceMs = settings.vad_min_silence_ms;
    vadMinSpeechMs = settings.vad_min_speech_ms;
    vadSpeechPadMs = settings.vad_speech_pad_ms;
    hallucinationEnabled = settings.hallucination_silence_threshold !== null;
    hallucinationValue = settings.hallucination_silence_threshold ?? 2.0;
    repetitionPenalty = settings.repetition_penalty;
    diarizationSource = settings.diarization_source;
  }

  function storeOriginalValues(settings: TranscriptionSettings) {
    originalMinSpeakers = settings.min_speakers;
    originalMaxSpeakers = settings.max_speakers;
    originalSpeakerBehavior = settings.speaker_prompt_behavior;
    originalGarbageCleanupEnabled = settings.garbage_cleanup_enabled;
    originalGarbageCleanupThreshold = settings.garbage_cleanup_threshold;
    originalSourceLanguage = settings.source_language;
    originalTranslateToEnglish = settings.translate_to_english;
    originalLlmOutputLanguage = settings.llm_output_language;
    originalVadThreshold = settings.vad_threshold;
    originalVadMinSilenceMs = settings.vad_min_silence_ms;
    originalVadMinSpeechMs = settings.vad_min_speech_ms;
    originalVadSpeechPadMs = settings.vad_speech_pad_ms;
    originalHallucinationEnabled = settings.hallucination_silence_threshold !== null;
    originalHallucinationValue = settings.hallucination_silence_threshold ?? 2.0;
    originalRepetitionPenalty = settings.repetition_penalty;
    originalDiarizationSource = settings.diarization_source;
  }

  async function saveSettings() {
    if (validationError) {
      toastStore.error(validationError);
      return;
    }

    saving = true;
    try {
      const updatedSettings = await updateTranscriptionSettings({
        min_speakers: minSpeakers,
        max_speakers: maxSpeakers,
        speaker_prompt_behavior: speakerBehavior,
        garbage_cleanup_enabled: garbageCleanupEnabled,
        garbage_cleanup_threshold: garbageCleanupThreshold,
        source_language: sourceLanguage,
        translate_to_english: translateToEnglish,
        llm_output_language: llmOutputLanguage,
        vad_threshold: vadThreshold,
        vad_min_silence_ms: vadMinSilenceMs,
        vad_min_speech_ms: vadMinSpeechMs,
        vad_speech_pad_ms: vadSpeechPadMs,
        hallucination_silence_threshold: hallucinationSilenceThreshold,
        repetition_penalty: repetitionPenalty,
        diarization_source: diarizationSource
      });

      storeOriginalValues(updatedSettings);
      settingsModalStore.clearDirty('transcription');
      toastStore.success($t('settings.transcription.saved'));
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
      const response = await resetTranscriptionSettings();
      applySettings(response.default_settings);
      storeOriginalValues(response.default_settings);
      settingsModalStore.clearDirty('transcription');
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

<div class="transcription-settings">
  {#if loading}
    <div class="loading-state">
      <Spinner size="large" />
      <p>{$t('settings.transcription.loading')}</p>
    </div>
  {:else}
    <div class="settings-form">
      <!-- Speaker Settings Section -->
      <div class="settings-section">
        <div class="title-row">
          <h3 class="section-title">{$t('settings.transcription.speakerDetection')}</h3>
          <span class="info-icon">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="12" y1="16" x2="12" y2="12"></line>
              <line x1="12" y1="8" x2="12.01" y2="8"></line>
            </svg>
            <span class="tooltip">{$t('settings.transcription.speakerDetectionTooltip')}</span>
          </span>
        </div>
        <p class="section-desc">{$t('settings.transcription.speakerDetectionDesc')}</p>

        <!-- Diarization Source -->
        <div class="form-group">
          <label for="diarization-source" class="form-label">
            {$t('settings.transcription.diarizationSource')}
          </label>
          <select
            id="diarization-source"
            class="form-select"
            bind:value={diarizationSource}
          >
            <option value="provider">{$t('settings.transcription.diarizationProvider')}</option>
            <option value="local">{$t('settings.transcription.diarizationLocal')}</option>
            <option value="pyannote">{$t('settings.transcription.diarizationPyannote')}</option>
            <option value="off">{$t('settings.transcription.diarizationOff')}</option>
          </select>
          <p class="field-desc">
            {#if diarizationSource === 'provider'}
              {$t('settings.transcription.diarizationProviderDesc')}
            {:else if diarizationSource === 'local'}
              {$t('settings.transcription.diarizationLocalDesc')}
            {:else if diarizationSource === 'pyannote'}
              {$t('settings.transcription.diarizationPyannoteDesc')}
            {:else}
              {$t('settings.transcription.diarizationOffDesc')}
            {/if}
          </p>
          {#if diarizationSource === 'off'}
            <p class="field-warning">{$t('settings.transcription.disableDiarizationWarning')}</p>
          {/if}
          {#if diarizationSource === 'pyannote'}
            <p class="field-desc" style="margin-top: 0.5rem; font-style: italic;">
              {$t('settings.transcription.diarizationPyannoteConfigHint')}
            </p>
          {/if}
        </div>

        <!-- Speaker Behavior -->
        <div class="form-group">
          <label for="speaker-behavior" class="form-label">
            {$t('settings.transcription.behavior')}
            <span class="inline-info-icon">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="16" x2="12" y2="12"></line>
                <line x1="12" y1="8" x2="12.01" y2="8"></line>
              </svg>
              <span class="inline-tooltip">
                <strong>{$t('settings.transcription.behaviorTooltipAlwaysAsk')}</strong> {$t('settings.transcription.behaviorTooltipAlwaysAskDesc')}<br><br>
                <strong>{$t('settings.transcription.behaviorTooltipUseDefaults')}</strong> {$t('settings.transcription.behaviorTooltipUseDefaultsDesc')} ({$t('settings.transcription.minMaxFormat', { min: systemDefaults?.min_speakers ?? 1, max: systemDefaults?.max_speakers ?? 20 })}).<br><br>
                <strong>{$t('settings.transcription.behaviorTooltipUseCustom')}</strong> {$t('settings.transcription.behaviorTooltipUseCustomDesc')}
              </span>
            </span>
          </label>
          <select
            id="speaker-behavior"
            class="form-select"
            bind:value={speakerBehavior}
          >
            {#each speakerBehaviorOptions as option}
              <option value={option}>{getSpeakerBehaviorLabel(option)}</option>
            {/each}
          </select>
          <p class="input-hint">{getSpeakerBehaviorDescription(speakerBehavior)}</p>
        </div>

        <!-- Min/Max Speakers -->
        {#if speakerBehavior === 'use_custom' && diarizationSource !== 'off'}
          <div class="speaker-range-row">
            <div class="form-group compact">
              <label for="min-speakers" class="form-label">{$t('settings.transcription.minSpeakers')}</label>
              <input
                id="min-speakers"
                type="number"
                min="1"
                max="50"
                class="form-input number-input"
                bind:value={minSpeakers}
              />
            </div>
            <div class="form-group compact">
              <label for="max-speakers" class="form-label">{$t('settings.transcription.maxSpeakers')}</label>
              <input
                id="max-speakers"
                type="number"
                min="1"
                max="50"
                class="form-input number-input"
                bind:value={maxSpeakers}
              />
            </div>
          </div>
        {/if}
        {#if diarizationSource === 'off'}
          <p class="field-desc dimmed">{$t('settings.transcription.speakerDetectionDisabled')}</p>
        {/if}

        <!-- System Defaults Info -->
        {#if systemDefaults}
          <div class="defaults-info">
            <span class="defaults-label">{$t('settings.transcription.systemDefaults')}</span>
            <span class="defaults-value">{$t('settings.transcription.minMaxFormat', { min: systemDefaults.min_speakers, max: systemDefaults.max_speakers })}</span>
          </div>
        {/if}
      </div>

      <!-- Garbage Cleanup Section -->
      <div class="settings-section">
        <div class="title-row">
          <h3 class="section-title">{$t('settings.transcription.garbageCleanup')}</h3>
          <span class="info-icon">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="12" y1="16" x2="12" y2="12"></line>
              <line x1="12" y1="8" x2="12.01" y2="8"></line>
            </svg>
            <span class="tooltip">{$t('settings.transcription.garbageCleanupTooltip')}</span>
          </span>
        </div>
        <p class="section-desc">{$t('settings.transcription.garbageCleanupDesc')}</p>

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
            <span class="defaults-value">{systemDefaults.garbage_cleanup_threshold} {$t('settings.transcription.chars')}</span>
          </div>
        {/if}
      </div>

      <!-- Language Settings Section -->
      <div class="settings-section">
        <div class="title-row">
          <h3 class="section-title">{$t('settings.transcription.languageSettings')}</h3>
          <span class="info-icon">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="12" y1="16" x2="12" y2="12"></line>
              <line x1="12" y1="8" x2="12.01" y2="8"></line>
            </svg>
            <span class="tooltip">{$t('settings.transcription.languageSettingsTooltip')}</span>
          </span>
        </div>
        <p class="section-desc">{$t('settings.transcription.languageSettingsDesc')}</p>

        <!-- Source Language -->
        <div class="form-group">
          <label for="source-language" class="form-label">
            {$t('settings.transcription.sourceLanguage')}
            <span class="inline-info-icon">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="16" x2="12" y2="12"></line>
                <line x1="12" y1="8" x2="12.01" y2="8"></line>
              </svg>
              <span class="inline-tooltip">
                <strong>{$t('settings.transcription.sourceLanguageTooltipAuto')}</strong> {$t('settings.transcription.sourceLanguageTooltipAutoDesc')}<br><br>
                <strong>{$t('settings.transcription.sourceLanguageTooltipSpecific')}</strong> {$t('settings.transcription.sourceLanguageTooltipSpecificDesc')}<br><br>
                {$t('settings.transcription.sourceLanguageTooltipTimestamps')}
              </span>
            </span>
          </label>
          <select
            id="source-language"
            class="form-select"
            bind:value={sourceLanguage}
          >
            <optgroup label={$t('settings.transcription.commonLanguages')}>
              {#each sourceLanguageGroups.common as lang}
                <option value={lang.code}>
                  {lang.name}
                </option>
              {/each}
            </optgroup>
            <optgroup label={$t('settings.transcription.allLanguages')}>
              {#each sourceLanguageGroups.other as lang}
                <option value={lang.code}>
                  {lang.name}
                </option>
              {/each}
            </optgroup>
          </select>
          {#if isEnglishOnly && isNonEnglishSelected}
            <p class="capability-warning">
              {$t('settings.transcription.englishOnlyWarning')}
            </p>
          {:else if isEnglishOptimized && isNonEnglishSelected}
            <p class="capability-warning warning-subtle">
              {$t('settings.transcription.englishOptimizedWarning')}
            </p>
          {/if}
        </div>

        <!-- Translate to English -->
        <div class="form-group">
          <div class="setting-row">
            <div class="setting-controls">
              <label class="toggle-label" class:disabled-toggle={translationDisabled}>
                <input type="checkbox" bind:checked={translateToEnglish} class="toggle-input" disabled={translationDisabled} />
                <span class="toggle-switch"></span>
                <span class="toggle-text">{$t('settings.transcription.translateToEnglish')}</span>
              </label>
            </div>
          </div>
          <p class="input-hint">{$t('settings.transcription.translateToEnglishDesc')}</p>
          {#if translationDisabled && asrCapabilities}
            <p class="capability-warning">
              {#if asrCapabilities.provider === 'local'}
                {$t('settings.transcription.translationUnavailableLocal', { model: asrCapabilities.model_id })}
              {:else}
                {$t('settings.transcription.translationUnavailableProvider', { provider: ASRSettingsApi.getProviderDisplayName(asrCapabilities.provider) })}
              {/if}
            </p>
          {/if}
        </div>

        <!-- LLM Output Language -->
        <div class="form-group">
          <label for="llm-output-language" class="form-label">
            {$t('settings.transcription.aiSummaryLanguage')}
            <span class="inline-info-icon">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="16" x2="12" y2="12"></line>
                <line x1="12" y1="8" x2="12.01" y2="8"></line>
              </svg>
              <span class="inline-tooltip">
                {$t('settings.transcription.aiSummaryLanguageTooltip')}
              </span>
            </span>
          </label>
          <select
            id="llm-output-language"
            class="form-select"
            bind:value={llmOutputLanguage}
          >
            {#each llmLanguageOptions as lang}
              <option value={lang.code}>{lang.name}</option>
            {/each}
          </select>
          <p class="input-hint">{$t('settings.transcription.aiSummaryLanguageHint')}</p>
        </div>

        <div class="defaults-info">
          <span class="defaults-label">{$t('settings.transcription.defaults')}</span>
          <span class="defaults-value">{$t('settings.transcription.defaultsValue')}</span>
        </div>
      </div>

      <!-- Advanced Transcription Settings (collapsible) -->
      <div class="settings-section">
        <button
          type="button"
          class="collapsible-header"
          on:click={() => advancedExpanded = !advancedExpanded}
        >
          <div class="title-row">
            <h3 class="section-title">{$t('settings.transcription.advancedSettings')}</h3>
            <span class="info-icon">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="16" x2="12" y2="12"></line>
                <line x1="12" y1="8" x2="12.01" y2="8"></line>
              </svg>
              <span class="tooltip">{$t('settings.transcription.advancedSettingsTooltip')}</span>
            </span>
          </div>
          <svg
            class="chevron"
            class:expanded={advancedExpanded}
            xmlns="http://www.w3.org/2000/svg"
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <polyline points="6 9 12 15 18 9"></polyline>
          </svg>
        </button>
        <p class="section-desc">{$t('settings.transcription.advancedSettingsDesc')}</p>

        {#if advancedExpanded}
          <div class="advanced-content">
            <!-- Voice Activity Detection (VAD) -->
            <div class="subsection">
              <h4 class="subsection-title">{$t('settings.transcription.vadTitle')}</h4>
              <p class="subsection-desc">{$t('settings.transcription.vadDesc')}</p>

              <!-- VAD Threshold (slider) -->
              <div class="form-group">
                <label for="vad-threshold" class="form-label">
                  {$t('settings.transcription.vadThreshold')}
                  <span class="inline-info-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="16" x2="12" y2="12"></line>
                      <line x1="12" y1="8" x2="12.01" y2="8"></line>
                    </svg>
                    <span class="inline-tooltip">{$t('settings.transcription.vadThresholdTooltip')}</span>
                  </span>
                </label>
                <div class="slider-row">
                  <input
                    id="vad-threshold"
                    type="range"
                    min="0.1"
                    max="0.95"
                    step="0.05"
                    class="form-slider"
                    bind:value={vadThreshold}
                  />
                  <span class="slider-value">{vadThreshold.toFixed(2)}</span>
                </div>
                <p class="input-hint">{$t('settings.transcription.vadThresholdHint')}</p>
              </div>

              <!-- Min Silence Duration -->
              <div class="form-group">
                <label for="vad-min-silence" class="form-label">
                  {$t('settings.transcription.vadMinSilence')}
                  <span class="inline-info-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="16" x2="12" y2="12"></line>
                      <line x1="12" y1="8" x2="12.01" y2="8"></line>
                    </svg>
                    <span class="inline-tooltip">{$t('settings.transcription.vadMinSilenceTooltip')}</span>
                  </span>
                </label>
                <div class="inline-input">
                  <input
                    id="vad-min-silence"
                    type="number"
                    min="100"
                    max="5000"
                    step="100"
                    class="form-input number-input-wide"
                    bind:value={vadMinSilenceMs}
                  />
                  <span class="input-suffix">ms</span>
                </div>
                <p class="input-hint">{$t('settings.transcription.vadMinSilenceHint')}</p>
              </div>

              <!-- Min Speech Duration -->
              <div class="form-group">
                <label for="vad-min-speech" class="form-label">
                  {$t('settings.transcription.vadMinSpeech')}
                  <span class="inline-info-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="16" x2="12" y2="12"></line>
                      <line x1="12" y1="8" x2="12.01" y2="8"></line>
                    </svg>
                    <span class="inline-tooltip">{$t('settings.transcription.vadMinSpeechTooltip')}</span>
                  </span>
                </label>
                <div class="inline-input">
                  <input
                    id="vad-min-speech"
                    type="number"
                    min="50"
                    max="5000"
                    step="50"
                    class="form-input number-input-wide"
                    bind:value={vadMinSpeechMs}
                  />
                  <span class="input-suffix">ms</span>
                </div>
                <p class="input-hint">{$t('settings.transcription.vadMinSpeechHint')}</p>
              </div>

              <!-- Speech Padding -->
              <div class="form-group">
                <label for="vad-speech-pad" class="form-label">
                  {$t('settings.transcription.vadSpeechPad')}
                  <span class="inline-info-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="16" x2="12" y2="12"></line>
                      <line x1="12" y1="8" x2="12.01" y2="8"></line>
                    </svg>
                    <span class="inline-tooltip">{$t('settings.transcription.vadSpeechPadTooltip')}</span>
                  </span>
                </label>
                <div class="inline-input">
                  <input
                    id="vad-speech-pad"
                    type="number"
                    min="0"
                    max="2000"
                    step="50"
                    class="form-input number-input-wide"
                    bind:value={vadSpeechPadMs}
                  />
                  <span class="input-suffix">ms</span>
                </div>
                <p class="input-hint">{$t('settings.transcription.vadSpeechPadHint')}</p>
              </div>
            </div>

            <!-- Accuracy Settings -->
            <div class="subsection">
              <h4 class="subsection-title">{$t('settings.transcription.accuracyTitle')}</h4>
              <p class="subsection-desc">{$t('settings.transcription.accuracyDesc')}</p>

              <!-- Hallucination Filter -->
              <div class="form-group">
                <label for="hallucination-toggle" class="form-label">
                  {$t('settings.transcription.hallucinationFilter')}
                  <span class="inline-info-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="16" x2="12" y2="12"></line>
                      <line x1="12" y1="8" x2="12.01" y2="8"></line>
                    </svg>
                    <span class="inline-tooltip">{$t('settings.transcription.hallucinationFilterTooltip')}</span>
                  </span>
                </label>
                <div class="setting-row">
                  <div class="setting-controls">
                    <label class="toggle-label">
                      <input id="hallucination-toggle" type="checkbox" bind:checked={hallucinationEnabled} class="toggle-input" />
                      <span class="toggle-switch"></span>
                      <span class="toggle-text">{$t('settings.transcription.hallucinationEnable')}</span>
                    </label>
                    {#if hallucinationEnabled}
                      <div class="inline-input">
                        <span class="input-label">{$t('settings.transcription.hallucinationThresholdLabel')}</span>
                        <input
                          type="number"
                          min="0.5"
                          max="10"
                          step="0.5"
                          class="form-input number-input-wide"
                          bind:value={hallucinationValue}
                        />
                        <span class="input-suffix">s</span>
                      </div>
                    {/if}
                  </div>
                </div>
                <p class="input-hint">{$t('settings.transcription.hallucinationHint')}</p>
              </div>

              <!-- Repetition Penalty -->
              <div class="form-group">
                <label for="repetition-penalty" class="form-label">
                  {$t('settings.transcription.repetitionPenalty')}
                  <span class="inline-info-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="16" x2="12" y2="12"></line>
                      <line x1="12" y1="8" x2="12.01" y2="8"></line>
                    </svg>
                    <span class="inline-tooltip">{$t('settings.transcription.repetitionPenaltyTooltip')}</span>
                  </span>
                </label>
                <div class="slider-row">
                  <input
                    id="repetition-penalty"
                    type="range"
                    min="1.0"
                    max="2.0"
                    step="0.05"
                    class="form-slider"
                    bind:value={repetitionPenalty}
                  />
                  <span class="slider-value">{repetitionPenalty.toFixed(2)}</span>
                </div>
                <p class="input-hint">{$t('settings.transcription.repetitionPenaltyHint')}</p>
              </div>
            </div>

            <!-- System Defaults for Advanced -->
            {#if systemDefaults}
              <div class="defaults-info">
                <span class="defaults-label">{$t('settings.transcription.advancedDefaults')}</span>
                <span class="defaults-value">
                  {$t('settings.transcription.advancedDefaultsValue', {
                    vad: systemDefaults.vad_threshold,
                    silence: systemDefaults.vad_min_silence_ms,
                    speech: systemDefaults.vad_min_speech_ms,
                    pad: systemDefaults.vad_speech_pad_ms,
                    repetition: systemDefaults.repetition_penalty,
                  })}
                </span>
              </div>
            {/if}
          </div>
        {/if}
      </div>

      <!-- Validation Error -->
      {#if validationError}
        <div class="validation-error">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"></circle>
            <line x1="12" y1="8" x2="12" y2="12"></line>
            <line x1="12" y1="16" x2="12.01" y2="16"></line>
          </svg>
          <span>{validationError}</span>
        </div>
      {/if}

      <!-- Actions -->
      <div class="button-row">
        <button
          type="button"
          class="btn btn-secondary"
          on:click={resetToDefaults}
          disabled={saving || resetting}
        >
          {resetting ? $t('settings.transcription.resetting') : $t('settings.transcription.resetToDefaults')}
        </button>
        <button
          type="button"
          class="btn btn-primary"
          on:click={saveSettings}
          disabled={saving || resetting || !settingsChanged || !!validationError}
        >
          {saving ? $t('settings.transcription.saving') : $t('settings.transcription.saveSettings')}
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  .transcription-settings {
    padding: 0.5rem 0;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 3rem;
    gap: 1rem;
    color: var(--text-secondary);
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }

  .settings-section {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1.25rem;
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
    width: 280px;
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
    margin: 0.25rem 0 1rem 0;
  }

  .form-group {
    margin-bottom: 1rem;
  }

  .form-group.compact {
    margin-bottom: 0;
  }

  .form-label {
    display: block;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
    margin-bottom: 0.375rem;
  }

  .form-select {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-color);
    color: var(--text-color);
    font-size: 0.875rem;
    cursor: pointer;
    appearance: none;
    -webkit-appearance: none;
    padding-right: 2.5rem;
  }

  .form-select:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(var(--primary-color-rgb), 0.1);
  }

  .input-hint {
    margin: 0.375rem 0 0 0;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .inline-info-icon {
    position: relative;
    color: var(--text-muted);
    cursor: help;
    display: inline-flex;
    align-items: center;
    margin-left: 0.25rem;
    vertical-align: middle;
  }

  .inline-info-icon:hover {
    color: var(--primary-color);
  }

  .inline-tooltip {
    visibility: hidden;
    opacity: 0;
    position: absolute;
    left: 0;
    top: calc(100% + 8px);
    background-color: var(--surface-color, #1a1a2e);
    color: var(--text-color);
    padding: 0.75rem 1rem;
    border-radius: 8px;
    font-size: 0.75rem;
    font-weight: 400;
    line-height: 1.5;
    width: 320px;
    z-index: 1000;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
    border: 1px solid var(--border-color);
    transition: opacity 0.15s ease;
  }

  .inline-tooltip strong {
    color: var(--primary-color);
  }

  .inline-info-icon:hover .inline-tooltip {
    visibility: visible;
    opacity: 1;
  }

  .speaker-range-row {
    display: flex;
    gap: 1rem;
    margin-top: 1rem;
  }

  .form-input {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-color);
    color: var(--text-color);
    font-size: 0.875rem;
  }

  .form-input:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(var(--primary-color-rgb), 0.1);
  }

  .form-input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .number-input {
    width: 80px;
    text-align: center;
  }

  .defaults-info {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px dashed var(--border-color);
    font-size: 0.75rem;
  }

  .defaults-label {
    color: var(--text-muted);
  }

  .defaults-value {
    color: var(--text-secondary);
    font-family: 'Courier New', Courier, monospace;
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
    background-color: #3b82f6;
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

  .input-suffix {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .validation-error {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    background-color: rgba(239, 68, 68, 0.1);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: 6px;
    color: var(--error-color, #ef4444);
    font-size: 0.8125rem;
  }

  .button-row {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
    padding-top: 0.5rem;
  }

  .button-row .btn-secondary {
    margin-right: auto;
  }

  /* Collapsible header */
  .collapsible-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    color: var(--text-color);
  }

  .collapsible-header:hover {
    opacity: 0.8;
  }

  .chevron {
    color: var(--text-muted);
    transition: transform 0.2s ease;
    flex-shrink: 0;
  }

  .chevron.expanded {
    transform: rotate(180deg);
  }

  .advanced-content {
    margin-top: 1rem;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .subsection {
    padding: 1rem;
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
  }

  .subsection-title {
    font-size: 0.875rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
    color: var(--text-color);
  }

  .subsection-desc {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin: 0 0 1rem 0;
  }

  /* Slider */
  .slider-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .form-slider {
    flex: 1;
    -webkit-appearance: none;
    appearance: none;
    height: 6px;
    background: var(--border-color);
    border-radius: 3px;
    outline: none;
  }

  .form-slider::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: #3b82f6;
    cursor: pointer;
    border: 2px solid white;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
  }

  .form-slider::-moz-range-thumb {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: #3b82f6;
    cursor: pointer;
    border: 2px solid white;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
  }

  .slider-value {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
    font-family: 'Courier New', Courier, monospace;
    min-width: 3rem;
    text-align: right;
  }

  .number-input-wide {
    width: 100px;
    text-align: center;
  }

  /* Responsive */
  @media (max-width: 640px) {
    .setting-controls {
      flex-direction: column;
      align-items: flex-start;
      gap: 1rem;
    }

    .speaker-range-row {
      flex-direction: column;
    }

    .button-row {
      flex-direction: column;
    }

    .btn {
      width: 100%;
      min-height: 44px;
    }

    .number-input {
      min-height: 44px;
      font-size: 1rem;
    }
  }

  .capability-warning {
    font-size: 0.8rem;
    color: var(--warning-color, #b45309);
    margin-top: 0.25rem;
    padding: 0.4rem 0.6rem;
    background: var(--warning-bg, rgba(245, 158, 11, 0.1));
    border-radius: 4px;
    border-left: 3px solid var(--warning-color, #b45309);
  }

  .capability-warning.warning-subtle {
    color: var(--text-secondary);
    background: var(--surface-color);
    border-left-color: var(--text-secondary);
  }

  .disabled-toggle {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .disabled-toggle .toggle-input {
    pointer-events: none;
  }

  .field-desc {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin: 0.25rem 0 0 0;
  }

  .field-desc.dimmed {
    font-style: italic;
    opacity: 0.7;
  }

  .field-warning {
    font-size: 0.75rem;
    color: var(--warning-color, #b45309);
    margin: 0.375rem 0 0 0;
    padding: 0.25rem 0.5rem;
    background: var(--warning-bg, rgba(245, 158, 11, 0.1));
    border-radius: 4px;
  }
</style>
