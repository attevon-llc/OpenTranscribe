<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import type { TranscriptionSystemDefaults } from '$lib/api/transcriptionSettings';
  import { t } from '$stores/locale';
  import SettingsCard from './SettingsCard.svelte';
  import InfoTip from './InfoTip.svelte';
  import RangeSlider from '$components/ui/RangeSlider.svelte';

  export let vadThreshold: number;
  export let vadMinSilenceMs: number;
  export let vadMinSpeechMs: number;
  export let vadSpeechPadMs: number;
  export let hallucinationEnabled: boolean;
  export let hallucinationValue: number;
  export let repetitionPenalty: number;
  export let systemDefaults: TranscriptionSystemDefaults | null;
</script>

<SettingsCard
  title={$t('settings.transcription.vadTitle')}
  description={$t('settings.transcription.vadDesc')}
>
  <div class="form-field">
    <RangeSlider
      id="vad-threshold"
      label={$t('settings.transcription.vadThreshold')}
      hint={$t('settings.transcription.vadThresholdHint')}
      min={0.1}
      max={0.95}
      step={0.05}
      bind:value={vadThreshold}
    >
      <InfoTip slot="label-extra" inline>{$t('settings.transcription.vadThresholdTooltip')}</InfoTip>
    </RangeSlider>
  </div>

  <FormField id="vad-min-silence" label={$t('settings.transcription.vadMinSilence')} labelSize="sm" hint={$t('settings.transcription.vadMinSilenceHint')} let:describedby>
    <svelte:fragment slot="label-inline">
      <InfoTip inline>{$t('settings.transcription.vadMinSilenceTooltip')}</InfoTip>
    </svelte:fragment>
    <div class="inline-input">
      <input
        id="vad-min-silence" aria-describedby={describedby}
        type="number"
        min="100"
        max="5000"
        step="100"
        class="form-input number-input-wide"
        bind:value={vadMinSilenceMs}
      />
      <span class="input-suffix">ms</span>
    </div>
  </FormField>

  <FormField id="vad-min-speech" label={$t('settings.transcription.vadMinSpeech')} labelSize="sm" hint={$t('settings.transcription.vadMinSpeechHint')} let:describedby>
    <svelte:fragment slot="label-inline">
      <InfoTip inline>{$t('settings.transcription.vadMinSpeechTooltip')}</InfoTip>
    </svelte:fragment>
    <div class="inline-input">
      <input
        id="vad-min-speech" aria-describedby={describedby}
        type="number"
        min="50"
        max="5000"
        step="50"
        class="form-input number-input-wide"
        bind:value={vadMinSpeechMs}
      />
      <span class="input-suffix">ms</span>
    </div>
  </FormField>

  <FormField id="vad-speech-pad" label={$t('settings.transcription.vadSpeechPad')} labelSize="sm" hint={$t('settings.transcription.vadSpeechPadHint')} let:describedby>
    <svelte:fragment slot="label-inline">
      <InfoTip inline>{$t('settings.transcription.vadSpeechPadTooltip')}</InfoTip>
    </svelte:fragment>
    <div class="inline-input">
      <input
        id="vad-speech-pad" aria-describedby={describedby}
        type="number"
        min="0"
        max="2000"
        step="50"
        class="form-input number-input-wide"
        bind:value={vadSpeechPadMs}
      />
      <span class="input-suffix">ms</span>
    </div>
  </FormField>

  {#if systemDefaults}
    <div class="defaults-info">
      <span class="defaults-label">{$t('settings.transcription.advancedDefaults')}</span>
      <span class="defaults-value">
        {$t('settings.transcription.vadDefaultsValue', {
          vad: systemDefaults.vad_threshold,
          silence: systemDefaults.vad_min_silence_ms,
          speech: systemDefaults.vad_min_speech_ms,
          pad: systemDefaults.vad_speech_pad_ms
        })}
      </span>
    </div>
  {/if}
</SettingsCard>

<SettingsCard
  title={$t('settings.transcription.accuracyTitle')}
  description={$t('settings.transcription.accuracyDesc')}
>
  <FormField id="hallucination-toggle" label={$t('settings.transcription.hallucinationFilter')} labelSize="sm" hint={$t('settings.transcription.hallucinationHint')} let:describedby>
    <svelte:fragment slot="label-inline">
      <InfoTip inline>{$t('settings.transcription.hallucinationFilterTooltip')}</InfoTip>
    </svelte:fragment>
    <div class="setting-row">
      <div class="setting-controls">
        <label class="toggle-label">
          <input
            id="hallucination-toggle" aria-describedby={describedby}
            type="checkbox"
            bind:checked={hallucinationEnabled}
            class="toggle-input"
          />
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
  </FormField>

  <div class="form-field">
    <RangeSlider
      id="repetition-penalty"
      label={$t('settings.transcription.repetitionPenalty')}
      hint={$t('settings.transcription.repetitionPenaltyHint')}
      min={1.0}
      max={2.0}
      step={0.05}
      bind:value={repetitionPenalty}
    >
      <InfoTip slot="label-extra" inline>{$t('settings.transcription.repetitionPenaltyTooltip')}</InfoTip>
    </RangeSlider>
  </div>

  {#if systemDefaults}
    <div class="defaults-info">
      <span class="defaults-label">{$t('settings.transcription.advancedDefaults')}</span>
      <span class="defaults-value">
        {$t('settings.transcription.repetitionDefaultsValue', {
          repetition: systemDefaults.repetition_penalty
        })}
      </span>
    </div>
  {/if}
</SettingsCard>
