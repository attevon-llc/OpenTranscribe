<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { TranscriptionSettings, TranscriptionSystemDefaults } from '$lib/api/transcriptionSettings';

  export let minSpeakers: number | null = null;
  export let maxSpeakers: number | null = null;
  export let numSpeakers: number | null = null;
  export let transcriptionSettings: TranscriptionSettings | null = null;
  export let transcriptionSystemDefaults: TranscriptionSystemDefaults | null = null;

  const dispatch = createEventDispatcher<{
    settingsChange: { minSpeakers: number | null; maxSpeakers: number | null; numSpeakers: number | null };
  }>();

  $: if (minSpeakers !== null && minSpeakers < 1) minSpeakers = 1;
  $: if (maxSpeakers !== null && maxSpeakers < 1) maxSpeakers = 1;
  $: if (numSpeakers !== null && numSpeakers < 1) numSpeakers = 1;
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
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="12" y1="16" x2="12" y2="12"></line>
          <line x1="12" y1="8" x2="12.01" y2="8"></line>
        </svg>
        <span>{$t('uploader.usingSystemDefaults')}{#if transcriptionSystemDefaults} {$t('uploader.speakerDefaultsRange', { min: transcriptionSystemDefaults.min_speakers, max: transcriptionSystemDefaults.max_speakers })}{/if}</span>
      </div>
    {:else if transcriptionSettings.speaker_prompt_behavior === 'use_custom'}
      <div class="info-note">
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="12" y1="16" x2="12" y2="12"></line>
          <line x1="12" y1="8" x2="12.01" y2="8"></line>
        </svg>
        <span>{$t('uploader.usingSavedSettings')} {$t('uploader.speakerDefaultsRange', { min: transcriptionSettings.min_speakers, max: transcriptionSettings.max_speakers })}</span>
      </div>
    {/if}
  {/if}

  <div class="settings-row">
    <div class="field">
      <label for="min-speakers">
        {$t('uploader.minSpeakers')}
        <span class="hint">{$t('uploader.minSpeakersHint')}</span>
      </label>
      <input
        id="min-speakers"
        type="number"
        min="1"
        placeholder={transcriptionSystemDefaults ? `${$t('uploader.default')}: ${transcriptionSystemDefaults.min_speakers}` : $t('uploader.usesDefault')}
        bind:value={minSpeakers}
        on:change={emitChange}
        disabled={numSpeakers !== null}
      />
    </div>
    <div class="field">
      <label for="max-speakers">
        {$t('uploader.maxSpeakers')}
        <span class="hint">{$t('uploader.maxSpeakersHint')}</span>
      </label>
      <input
        id="max-speakers"
        type="number"
        min="1"
        placeholder={transcriptionSystemDefaults ? `${$t('uploader.default')}: ${transcriptionSystemDefaults.max_speakers}` : $t('uploader.usesDefault')}
        bind:value={maxSpeakers}
        on:change={emitChange}
        disabled={numSpeakers !== null}
      />
    </div>
  </div>

  <div class="field">
    <label for="num-speakers">
      {$t('uploader.fixedSpeakerCount')}
      <span class="hint">{$t('uploader.fixedSpeakerCountHint')}</span>
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
  .step-speakers { display: flex; flex-direction: column; gap: 0.75rem; }

  .step-hint { font-size: 0.8125rem; color: var(--text-secondary); margin: 0; line-height: 1.5; }

  .info-note {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.625rem 0.75rem; font-size: 0.8125rem;
    color: var(--text-secondary);
    background: rgba(59, 130, 246, 0.05);
    border: 1px solid rgba(59, 130, 246, 0.12);
    border-radius: 6px;
  }
  :global(.dark) .info-note { background: rgba(59, 130, 246, 0.1); border-color: rgba(59, 130, 246, 0.2); }
  .info-note svg { flex-shrink: 0; color: var(--primary-color, #3b82f6); }
  .info-note span { flex: 1; }

  .settings-row { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }

  .field { display: flex; flex-direction: column; gap: 0.375rem; }
  .field label { font-size: 0.8125rem; font-weight: 500; color: var(--text-primary); display: flex; flex-direction: column; gap: 0.125rem; }
  .hint { font-size: 0.6875rem; font-weight: 400; color: var(--text-secondary); }

  .field input[type="number"] {
    padding: 0.4375rem 0.5rem; border: 1px solid var(--border-color); border-radius: 6px;
    background: var(--input-background, var(--card-background)); color: var(--text-primary); font-size: 0.875rem;
    transition: all 0.15s ease;
  }
  .field input:focus { outline: none; border-color: var(--primary-color); box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1); }
  .field input:disabled { opacity: 0.5; cursor: not-allowed; background: var(--disabled-background, #f5f5f5); }
  :global(.dark) .field input:disabled { background: rgba(255, 255, 255, 0.05); }
  .field input::placeholder { color: var(--text-light); }

  .validation-error {
    display: flex; align-items: center; gap: 0.5rem; padding: 0.5rem;
    background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: 6px; color: #dc2626; font-size: 0.8125rem;
  }
  :global(.dark) .validation-error { background: rgba(239, 68, 68, 0.15); border-color: rgba(239, 68, 68, 0.4); color: #f87171; }

  @media (max-width: 480px) { .settings-row { grid-template-columns: 1fr; } }
</style>
