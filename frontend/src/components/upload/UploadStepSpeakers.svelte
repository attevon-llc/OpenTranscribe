<script lang="ts">
  import './upload-shared.css';
  import InfoIcon from '$components/ui/InfoIcon.svelte';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { TranscriptionSettings, TranscriptionSystemDefaults } from '$lib/api/transcriptionSettings';

  export let minSpeakers: number | null = null;
  export let maxSpeakers: number | null = null;
  export let numSpeakers: number | null = null;
  /** The uploader states the speakers agreed to voice identification (recorded, not verified). */
  export let voiceprintConsent = false;
  /** False while voiceprints are off for this person: there is nothing to consent to. */
  export let voiceprintsOn = true;
  export let transcriptionSettings: TranscriptionSettings | null = null;
  export let transcriptionSystemDefaults: TranscriptionSystemDefaults | null = null;

  const dispatch = createEventDispatcher<{
    settingsChange: { minSpeakers: number | null; maxSpeakers: number | null; numSpeakers: number | null };
  }>();

  $: if (minSpeakers !== null && minSpeakers < 1) minSpeakers = 1;
  $: if (maxSpeakers !== null && maxSpeakers < 1) maxSpeakers = 1;
  $: if (numSpeakers !== null && numSpeakers < 1) numSpeakers = 1;
  // What a blank field means on the server: the system range under "use system defaults",
  // otherwise the user's own saved range.
  $: useSystemRange = transcriptionSettings?.speaker_prompt_behavior === 'use_defaults';
  $: blankMin = useSystemRange ? transcriptionSystemDefaults?.min_speakers : transcriptionSettings?.min_speakers;
  $: blankMax = useSystemRange ? transcriptionSystemDefaults?.max_speakers : transcriptionSettings?.max_speakers;
  $: hasValidationError = minSpeakers !== null && maxSpeakers !== null && minSpeakers > maxSpeakers;

  function emitChange() {
    dispatch('settingsChange', { minSpeakers, maxSpeakers, numSpeakers });
  }
</script>

<div class="step-speakers">
  <p class="step-hint">{$t('uploader.speakersHint')}</p>

  {#if transcriptionSettings}
    {#if transcriptionSettings.speaker_prompt_behavior === 'use_defaults'}
      <div class="info-note">
        <InfoIcon size={14} />
        <span>{$t('uploader.usingSystemDefaults')}{#if transcriptionSystemDefaults} {$t('uploader.speakerDefaultsRange', { min: transcriptionSystemDefaults.min_speakers, max: transcriptionSystemDefaults.max_speakers })}{/if}</span>
      </div>
    {:else if transcriptionSettings.speaker_prompt_behavior === 'use_custom'}
      <div class="info-note">
        <InfoIcon size={14} />
        <span>{$t('uploader.usingSavedSettings')} {$t('uploader.speakerDefaultsRange', { min: transcriptionSettings.min_speakers, max: transcriptionSettings.max_speakers })}</span>
      </div>
    {/if}
  {/if}

  <div class="settings-row">
    <div class="stacked-field">
      <label for="min-speakers">
        {$t('uploader.minSpeakers')}
        <span class="stacked-field-hint">{$t('uploader.minSpeakersHint')}</span>
      </label>
      <input
        id="min-speakers"
        type="number"
        min="1"
        placeholder={blankMin != null ? `${$t('uploader.default')}: ${blankMin}` : $t('uploader.usesDefault')}
        bind:value={minSpeakers}
        on:change={emitChange}
        disabled={numSpeakers !== null}
      />
    </div>
    <div class="stacked-field">
      <label for="max-speakers">
        {$t('uploader.maxSpeakers')}
        <span class="stacked-field-hint">{$t('uploader.maxSpeakersHint')}</span>
      </label>
      <input
        id="max-speakers"
        type="number"
        min="1"
        placeholder={blankMax != null ? `${$t('uploader.default')}: ${blankMax}` : $t('uploader.usesDefault')}
        bind:value={maxSpeakers}
        on:change={emitChange}
        disabled={numSpeakers !== null}
      />
    </div>
  </div>

  <div class="stacked-field">
    <label for="num-speakers">
      {$t('uploader.fixedSpeakerCount')}
      <span class="stacked-field-hint">{$t('uploader.fixedSpeakerCountHint')}</span>
    </label>
    <input
      id="num-speakers"
      type="number"
      min="1"
      placeholder={$t('uploader.usesDefault')}
      bind:value={numSpeakers}
      on:change={emitChange}
    />
  </div>

  {#if voiceprintsOn}
    <label class="consent-row" for="voiceprint-consent">
      <input
        id="voiceprint-consent"
        type="checkbox"
        bind:checked={voiceprintConsent}
        data-testid="voiceprint-consent"
      />
      <span class="consent-text">
        {$t('uploader.voiceprintConsent.label')}
        <span class="stacked-field-hint">{$t('uploader.voiceprintConsent.hint')}</span>
      </span>
    </label>
  {/if}

  {#if hasValidationError}
    <div class="validation-error">
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="12" y1="8" x2="12" y2="12"></line>
        <line x1="12" y1="16" x2="12.01" y2="16"></line>
      </svg>
      {$t('uploader.minMaxValidationError')}
    </div>
  {/if}
</div>

<style>
  .step-speakers { display: flex; flex-direction: column; gap: var(--space-3); }


  .info-note {
    display: flex; align-items: center; gap: var(--space-2);
    padding: var(--space-2-5) var(--space-3); font-size: var(--font-size-sm);
    color: var(--text-secondary);
    background: rgba(var(--accent-rgb), 0.05);
    border: 1px solid rgba(var(--accent-rgb), 0.12);
    border-radius: var(--radius-md);
  }
  :global([data-theme='dark']) .info-note { background: rgba(var(--accent-rgb), 0.1); border-color: rgba(var(--accent-rgb), 0.2); }
  .info-note :global(svg) { flex-shrink: 0; color: var(--primary-on-surface); }
  .info-note span { flex: 1; }

  .settings-row { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4); }


  .stacked-field input[type="number"] {
    padding: var(--space-2); border: 1px solid var(--border-color); border-radius: var(--radius-md);
  }
  .stacked-field input:disabled { opacity: 0.5; cursor: not-allowed; background: var(--disabled-background); }
  .stacked-field input::placeholder { color: var(--text-light); }

  .consent-row {
    display: flex; align-items: flex-start; gap: var(--space-2-5);
    padding: var(--space-2-5) var(--space-3); cursor: pointer;
    border: 1px solid var(--border-color); border-radius: var(--radius-md);
    background: var(--surface-color);
  }
  .consent-row input[type='checkbox'] { margin-top: var(--space-1); flex-shrink: 0; }
  .consent-text { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--font-size-sm); font-weight: var(--font-weight-medium); color: var(--text-primary); }

  .validation-error {
    display: flex; align-items: center; gap: var(--space-2); padding: var(--space-2);
    background: var(--error-bg); border: 1px solid var(--error-border);
    border-radius: var(--radius-md); color: var(--error-text); font-size: var(--font-size-sm);
  }

  @media (max-width: 480px) { .settings-row { grid-template-columns: 1fr; } }
</style>
