<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import {
    getTranscriptionSettings,
    updateTranscriptionSettings,
    resetTranscriptionSettings,
    getTranscriptionSystemDefaults,
    type DiarizationSource,
    type SpeakerPromptBehavior,
    type TranscriptionSettings,
    type TranscriptionSettingsUpdate,
    type TranscriptionSystemDefaults
  } from '$lib/api/transcriptionSettings';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import Spinner from '../ui/Spinner.svelte';
  import SettingsCard from './transcription/SettingsCard.svelte';
  import InfoTip from './transcription/InfoTip.svelte';
  import FormActions from './transcription/FormActions.svelte';
  import './transcription/formLayout.css';
  import './transcription/formControls.css';

  const dispatch = createEventDispatcher();

  let minSpeakers = 1;
  let maxSpeakers = 20;
  let speakerBehavior: SpeakerPromptBehavior = 'always_prompt';
  let diarizationSource: DiarizationSource = 'provider';
  let originalMinSpeakers = 1;
  let originalMaxSpeakers = 20;
  let originalSpeakerBehavior: SpeakerPromptBehavior = 'always_prompt';
  let originalDiarizationSource: DiarizationSource = 'provider';

  // Deployment-owned: hidden here, and the server ignores writes to it.
  $: diarizationSourceEnabled = isCapabilityEnabled($capabilities, 'transcription.diarization_source');

  let systemDefaults: TranscriptionSystemDefaults | null = null;
  let loading = true;
  let saving = false;
  let resetting = false;
  let validationError = '';

  const speakerBehaviorOptions: { value: SpeakerPromptBehavior; label: string; desc: string }[] = [
    {
      value: 'always_prompt',
      label: 'settings.speakerIdentification.countMode.alwaysPrompt',
      desc: 'settings.speakerIdentification.countMode.alwaysPromptDesc'
    },
    {
      value: 'use_defaults',
      label: 'settings.speakerIdentification.countMode.useDefaults',
      desc: 'settings.speakerIdentification.countMode.useDefaultsDesc'
    },
    {
      value: 'use_custom',
      label: 'settings.speakerIdentification.countMode.useCustom',
      desc: 'settings.speakerIdentification.countMode.useCustomDesc'
    }
  ];
  const sourceOptions: { value: DiarizationSource; label: string; desc: string }[] = [
    {
      value: 'provider',
      label: 'settings.speakerIdentification.source.provider',
      desc: 'settings.speakerIdentification.source.providerDesc'
    },
    {
      value: 'local',
      label: 'settings.speakerIdentification.source.local',
      desc: 'settings.speakerIdentification.source.localDesc'
    },
    {
      value: 'pyannote',
      label: 'settings.speakerIdentification.source.pyannote',
      desc: 'settings.speakerIdentification.source.pyannoteDesc'
    },
    {
      value: 'off',
      label: 'settings.speakerIdentification.source.off',
      desc: 'settings.speakerIdentification.source.offDesc'
    }
  ];

  $: settingsChanged =
    minSpeakers !== originalMinSpeakers ||
    maxSpeakers !== originalMaxSpeakers ||
    speakerBehavior !== originalSpeakerBehavior ||
    diarizationSource !== originalDiarizationSource;

  $: dispatch('change', { hasChanges: settingsChanged });

  // UX-only range checks; the backend stays the authority.
  $: {
    if (minSpeakers > maxSpeakers) {
      validationError = $t('settings.speakerIdentification.validationMinMax');
    } else if (minSpeakers < 1 || minSpeakers > 50) {
      validationError = $t('settings.speakerIdentification.validationMinRange');
    } else if (maxSpeakers < 1 || maxSpeakers > 50) {
      validationError = $t('settings.speakerIdentification.validationMaxRange');
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
      toastStore.error($t('settings.speakerIdentification.loadFailed'));
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
    minSpeakers = originalMinSpeakers = settings.min_speakers;
    maxSpeakers = originalMaxSpeakers = settings.max_speakers;
    speakerBehavior = originalSpeakerBehavior = settings.speaker_prompt_behavior;
    // The reset response omits diarization_source, so fall back to the server default.
    diarizationSource = originalDiarizationSource =
      settings.diarization_source ?? systemDefaults?.diarization_source_default ?? 'provider';
  }

  async function saveSettings() {
    if (validationError) {
      toastStore.error(validationError);
      return;
    }
    saving = true;
    try {
      const update: TranscriptionSettingsUpdate = {
        min_speakers: minSpeakers,
        max_speakers: maxSpeakers,
        speaker_prompt_behavior: speakerBehavior
      };
      if (diarizationSourceEnabled) update.diarization_source = diarizationSource;
      applySettings(await updateTranscriptionSettings(update));
      toastStore.success($t('settings.speakerIdentification.saved'));
      dispatch('save');
    } catch (err) {
      console.error('Failed to save transcription settings:', err);
      toastStore.error($t('settings.speakerIdentification.saveFailed'));
    } finally {
      saving = false;
    }
  }

  async function resetToDefaults() {
    resetting = true;
    try {
      const response = await resetTranscriptionSettings('speakers');
      applySettings(response.default_settings);
      toastStore.success($t('settings.speakerIdentification.resetSuccess'));
      dispatch('reset');
    } catch (err) {
      console.error('Failed to reset transcription settings:', err);
      toastStore.error($t('settings.speakerIdentification.resetFailed'));
    } finally {
      resetting = false;
    }
  }
</script>

<div class="speaker-detection-settings tx-form">
  {#if loading}
    <div class="loading-state">
      <Spinner size="large" />
      <p>{$t('settings.transcription.loading')}</p>
    </div>
  {:else}
    <div class="settings-form">
      {#if diarizationSourceEnabled}
        <SettingsCard
          title={$t('settings.speakerIdentification.detectionHeading')}
          description={$t('settings.speakerIdentification.detectionDesc')}
        >
          <div class="form-group">
            <label for="diarization-source" class="form-label">
              {$t('settings.speakerIdentification.source.label')}
            </label>
            <select id="diarization-source" class="form-select" bind:value={diarizationSource}>
              {#each sourceOptions as source (source.value)}
                <option value={source.value}>{$t(source.label)}</option>
              {/each}
            </select>
            <p class="field-desc">
              {$t(sourceOptions.find((source) => source.value === diarizationSource)?.desc ?? '')}
            </p>
            {#if diarizationSource === 'off'}
              <p class="field-warning">{$t('settings.speakerIdentification.source.offWarning')}</p>
            {/if}
            {#if diarizationSource === 'pyannote'}
              <p class="field-desc hint-italic">
                {$t('settings.speakerIdentification.source.pyannoteHint')}
              </p>
            {/if}
          </div>
        </SettingsCard>
      {/if}

      <SettingsCard
        title={$t('settings.speakerIdentification.countHeading')}
        description={$t('settings.speakerIdentification.countDesc')}
      >
        <div class="form-group">
          <label for="speaker-behavior" class="form-label">
            {$t('settings.speakerIdentification.countMode.label')}
            <InfoTip inline>
              {#each speakerBehaviorOptions as option, i (option.value)}
                <strong>{$t(option.label)}:</strong>
                {$t(option.desc)}{#if i < speakerBehaviorOptions.length - 1}<br /><br />{/if}
              {/each}
            </InfoTip>
          </label>
          <select id="speaker-behavior" class="form-select" bind:value={speakerBehavior}>
            {#each speakerBehaviorOptions as option (option.value)}
              <option value={option.value}>{$t(option.label)}</option>
            {/each}
          </select>
          <p class="input-hint">
            {$t(
              speakerBehaviorOptions.find((option) => option.value === speakerBehavior)?.desc ?? ''
            )}
          </p>
        </div>

        {#if speakerBehavior === 'use_custom' && diarizationSource !== 'off'}
          <div class="speaker-range-row">
            <div class="form-group compact">
              <label for="min-speakers" class="form-label">
                {$t('settings.speakerIdentification.minSpeakers')}
              </label>
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
              <label for="max-speakers" class="form-label">
                {$t('settings.speakerIdentification.maxSpeakers')}
              </label>
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
          <p class="field-desc dimmed">{$t('settings.speakerIdentification.source.offNote')}</p>
        {/if}

        {#if systemDefaults}
          <div class="defaults-info">
            <span class="defaults-label">{$t('settings.speakerIdentification.systemDefaults')}</span>
            <span class="defaults-value">
              {$t('settings.speakerIdentification.minMaxFormat', {
                min: systemDefaults.min_speakers,
                max: systemDefaults.max_speakers
              })}
            </span>
          </div>
        {/if}
      </SettingsCard>

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
