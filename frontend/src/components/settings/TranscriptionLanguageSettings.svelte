<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { onMount, createEventDispatcher } from 'svelte';
  import {
    getTranscriptionSettings,
    updateTranscriptionSettings,
    resetTranscriptionSettings,
    getTranscriptionSystemDefaults,
    groupLanguages,
    type TranscriptionSettings,
    type LanguageOption
  } from '$lib/api/transcriptionSettings';
  import { ASRSettingsApi, type ASRModelCapabilities } from '$lib/api/asrSettings';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';
  import { localizeLanguages } from '$lib/i18n/backendLabels';
  import Spinner from '$components/ui/Spinner.svelte';
  import SettingsCard from './transcription/SettingsCard.svelte';
  import InfoTip from './transcription/InfoTip.svelte';
  import FormActions from './transcription/FormActions.svelte';
  import './transcription/formLayout.css';
  import './transcription/formControls.css';

  const dispatch = createEventDispatcher();

  let sourceLanguage = 'auto';
  let translateToEnglish = false;
  let llmOutputLanguage = 'en';
  let originalSourceLanguage = 'auto';
  let originalTranslateToEnglish = false;
  let originalLlmOutputLanguage = 'en';

  let systemDefaults: Awaited<ReturnType<typeof getTranscriptionSystemDefaults>> | null = null;
  // The backend sends code -> English name; the names are rendered in the UI language.
  $: sourceLanguageGroups = systemDefaults
    ? groupLanguages(
        localizeLanguages($t, $locale, systemDefaults.available_source_languages) ?? {},
        systemDefaults.common_languages
      )
    : { common: [] as LanguageOption[], other: [] as LanguageOption[] };
  $: llmLanguageOptions = Object.entries(
    localizeLanguages($t, $locale, systemDefaults?.available_llm_output_languages) ?? {}
  ).map(([code, name]): LanguageOption => ({ code, name }));
  let loading = true;
  let saving = false;
  let resetting = false;

  let asrCapabilities: ASRModelCapabilities | null = null;
  $: translationDisabled = asrCapabilities ? !asrCapabilities.supports_translation : false;
  $: isEnglishOptimized = asrCapabilities?.language_support === 'english_optimized';
  // Keyed off language_support first so local english_only models are covered even when the
  // catalog omits a numeric languages count; falls back to languages === 1 for cloud providers.
  $: isEnglishOnly =
    asrCapabilities?.language_support === 'english_only' || asrCapabilities?.languages === 1;
  $: isNonEnglishSelected = sourceLanguage !== 'auto' && sourceLanguage !== 'en';

  $: settingsChanged =
    sourceLanguage !== originalSourceLanguage ||
    translateToEnglish !== originalTranslateToEnglish ||
    llmOutputLanguage !== originalLlmOutputLanguage;

  $: dispatch('change', { hasChanges: settingsChanged });

  onMount(() => {
    void Promise.all([loadSettings(), loadSystemDefaults(), loadASRCapabilities()]);
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

  async function loadASRCapabilities() {
    try {
      const status = await ASRSettingsApi.getStatus();
      asrCapabilities = status.active_model_capabilities ?? null;
    } catch (err) {
      console.error('Failed to load ASR capabilities:', err);
    }
  }

  function applySettings(settings: TranscriptionSettings) {
    sourceLanguage = originalSourceLanguage = settings.source_language;
    translateToEnglish = originalTranslateToEnglish = settings.translate_to_english;
    llmOutputLanguage = originalLlmOutputLanguage = settings.llm_output_language;
  }

  async function saveSettings() {
    saving = true;
    try {
      applySettings(
        await updateTranscriptionSettings({
          source_language: sourceLanguage,
          translate_to_english: translateToEnglish,
          llm_output_language: llmOutputLanguage
        })
      );
      toastStore.success($t('settings.transcription.languageSaved'));
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
      const response = await resetTranscriptionSettings('language');
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

<div class="transcription-language-settings tx-form">
  {#if loading}
    <div class="loading-state">
      <Spinner size="large" />
      <p>{$t('settings.transcription.loading')}</p>
    </div>
  {:else}
    <div class="settings-form">
      <SettingsCard
        title={$t('settings.transcription.languageSettings')}
        description={$t('settings.transcription.languageSettingsDesc')}
        tooltip={$t('settings.transcription.languageSettingsTooltip')}
      >
        <FormField id="source-language" label={$t('settings.transcription.sourceLanguage')} labelSize="sm">
          <svelte:fragment slot="label-inline">
            <InfoTip inline>
              <strong>{$t('settings.transcription.sourceLanguageTooltipAuto')}</strong>
              {$t('settings.transcription.sourceLanguageTooltipAutoDesc')}<br /><br />
              <strong>{$t('settings.transcription.sourceLanguageTooltipSpecific')}</strong>
              {$t('settings.transcription.sourceLanguageTooltipSpecificDesc')}<br /><br />
              {$t('settings.transcription.sourceLanguageTooltipTimestamps')}
            </InfoTip>
          </svelte:fragment>
          <select id="source-language" class="form-select" bind:value={sourceLanguage}>
            <optgroup label={$t('settings.transcription.commonLanguages')}>
              {#each sourceLanguageGroups.common as lang (lang.code)}
                <option value={lang.code}>{lang.name}</option>
              {/each}
            </optgroup>
            <optgroup label={$t('settings.transcription.allLanguages')}>
              {#each sourceLanguageGroups.other as lang (lang.code)}
                <option value={lang.code}>{lang.name}</option>
              {/each}
            </optgroup>
          </select>
          {#if isEnglishOnly && isNonEnglishSelected}
            <p class="capability-warning">{$t('settings.transcription.englishOnlyWarning')}</p>
          {:else if isEnglishOptimized && isNonEnglishSelected}
            <p class="capability-warning warning-subtle">
              {$t('settings.transcription.englishOptimizedWarning')}
            </p>
          {/if}
        </FormField>

        <div class="form-field">
          <div class="setting-row">
            <div class="setting-controls">
              <label class="toggle-label" class:disabled-toggle={translationDisabled}>
                <input
                  type="checkbox"
                  bind:checked={translateToEnglish}
                  class="toggle-input"
                  disabled={translationDisabled}
                />
                <span class="toggle-switch"></span>
                <span class="toggle-text">{$t('settings.transcription.translateToEnglish')}</span>
              </label>
            </div>
          </div>
          <p class="input-hint">{$t('settings.transcription.translateToEnglishDesc')}</p>
          {#if translationDisabled && asrCapabilities}
            <p class="capability-warning">
              {#if asrCapabilities.provider === 'local'}
                {$t('settings.transcription.translationUnavailableLocal', {
                  model: asrCapabilities.model_id
                })}
              {:else}
                {$t('settings.transcription.translationUnavailableProvider', {
                  provider: ASRSettingsApi.getProviderDisplayName(asrCapabilities.provider)
                })}
              {/if}
            </p>
          {/if}
        </div>

        <div class="defaults-info">
          <span class="defaults-label">{$t('settings.transcription.defaults')}</span>
          <span class="defaults-value">{$t('settings.transcription.languageDefaultsValue', {
              source: $t('settings.transcription.defaultSourceAuto'),
              translate: $t('settings.transcription.defaultTranslateOff')
            })}</span>
        </div>
      </SettingsCard>

      <SettingsCard
        title={$t('settings.transcription.aiLanguageHeading')}
        description={$t('settings.transcription.aiLanguageDesc')}
      >
        <FormField id="llm-output-language" label={$t('settings.transcription.aiSummaryLanguage')} labelSize="sm" hint={$t('settings.transcription.aiSummaryLanguageHint')} let:describedby>
          <svelte:fragment slot="label-inline">
            <InfoTip inline>{$t('settings.transcription.aiSummaryLanguageTooltip')}</InfoTip>
          </svelte:fragment>
          <select id="llm-output-language" aria-describedby={describedby} class="form-select" bind:value={llmOutputLanguage}>
            {#each llmLanguageOptions as lang (lang.code)}
              <option value={lang.code}>{lang.name}</option>
            {/each}
          </select>
        </FormField>

        <div class="defaults-info">
          <span class="defaults-label">{$t('settings.transcription.defaults')}</span>
          <span class="defaults-value">{$t('settings.transcription.aiLanguageDefaultsValue', {
              language: $t('settings.transcription.defaultSummaryLanguage')
            })}</span>
        </div>
      </SettingsCard>

      <FormActions
        {saving}
        {resetting}
        dirty={settingsChanged}
        on:save={saveSettings}
        on:reset={resetToDefaults}
      />
    </div>
  {/if}
</div>
