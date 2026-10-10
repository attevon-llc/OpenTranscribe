<script lang="ts">
  import type { TranscriptionSystemDefaults } from '$lib/api/transcriptionSettings';
  import { t } from '$stores/locale';
  import SettingsCard from './SettingsCard.svelte';
  import InfoTip from './InfoTip.svelte';

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
  <div class="form-group">
    <label for="vad-threshold" class="form-label">
      {$t('settings.transcription.vadThreshold')}
      <InfoTip inline>{$t('settings.transcription.vadThresholdTooltip')}</InfoTip>
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

  <div class="form-group">
    <label for="vad-min-silence" class="form-label">
      {$t('settings.transcription.vadMinSilence')}
      <InfoTip inline>{$t('settings.transcription.vadMinSilenceTooltip')}</InfoTip>
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

  <div class="form-group">
    <label for="vad-min-speech" class="form-label">
      {$t('settings.transcription.vadMinSpeech')}
      <InfoTip inline>{$t('settings.transcription.vadMinSpeechTooltip')}</InfoTip>
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

  <div class="form-group">
    <label for="vad-speech-pad" class="form-label">
      {$t('settings.transcription.vadSpeechPad')}
      <InfoTip inline>{$t('settings.transcription.vadSpeechPadTooltip')}</InfoTip>
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
</SettingsCard>

<SettingsCard
  title={$t('settings.transcription.accuracyTitle')}
  description={$t('settings.transcription.accuracyDesc')}
>
  <div class="form-group">
    <label for="hallucination-toggle" class="form-label">
      {$t('settings.transcription.hallucinationFilter')}
      <InfoTip inline>{$t('settings.transcription.hallucinationFilterTooltip')}</InfoTip>
    </label>
    <div class="setting-row">
      <div class="setting-controls">
        <label class="toggle-label">
          <input
            id="hallucination-toggle"
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
    <p class="input-hint">{$t('settings.transcription.hallucinationHint')}</p>
  </div>

  <div class="form-group">
    <label for="repetition-penalty" class="form-label">
      {$t('settings.transcription.repetitionPenalty')}
      <InfoTip inline>{$t('settings.transcription.repetitionPenaltyTooltip')}</InfoTip>
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

  {#if systemDefaults}
    <div class="defaults-info">
      <span class="defaults-label">{$t('settings.transcription.advancedDefaults')}</span>
      <span class="defaults-value">
        {$t('settings.transcription.advancedDefaultsValue', {
          vad: systemDefaults.vad_threshold,
          silence: systemDefaults.vad_min_silence_ms,
          speech: systemDefaults.vad_min_speech_ms,
          pad: systemDefaults.vad_speech_pad_ms,
          repetition: systemDefaults.repetition_penalty
        })}
      </span>
    </div>
  {/if}
</SettingsCard>
