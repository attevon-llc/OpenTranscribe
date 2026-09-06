<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let selectedWhisperModel: string | null = null;
  export let adminDefaultModel = 'large-v3-turbo';
  export let skipSummary = false;

  const dispatch = createEventDispatcher<{
    change: { selectedWhisperModel: string | null; skipSummary: boolean };
  }>();

  function emitChange() {
    dispatch('change', { selectedWhisperModel, skipSummary });
  }
</script>

<div class="step-model">
  <p class="step-hint">{$t('uploader.modelHint')}</p>

  <div class="field">
    <label for="whisper-model-select">
      {$t('uploader.whisperModel')}
      <span class="hint">{$t('uploader.whisperModelHint')}</span>
    </label>
    <select id="whisper-model-select" bind:value={selectedWhisperModel} on:change={emitChange} class="model-select">
      <option value={null}>
        {$t('uploader.highQuality')} ({adminDefaultModel})
      </option>
      <option value="base">
        {$t('uploader.fastProcessing')}
      </option>
    </select>
    {#if selectedWhisperModel === 'base'}
      <p class="model-note">{$t('uploader.fastProcessingHint')}</p>
    {/if}
  </div>

  <!-- AI Summary -->
  <div class="section">
    <h4 class="section-title">{$t('upload.aiSummary')}</h4>
    <label class="toggle-row">
      <span class="toggle-text">
        <span class="toggle-label">{$t('upload.skipSummary')}</span>
        <span class="hint">{$t('upload.skipSummaryHint')}</span>
      </span>
      <label class="toggle-switch">
        <input type="checkbox" bind:checked={skipSummary} on:change={emitChange} />
        <span class="toggle-slider"></span>
      </label>
    </label>
  </div>
</div>

<style>
  .step-model { display: flex; flex-direction: column; gap: 1rem; }
  .step-hint { font-size: 0.8125rem; color: var(--text-secondary); margin: 0; line-height: 1.5; }

  .field { display: flex; flex-direction: column; gap: 0.375rem; }
  .field label { font-size: 0.8125rem; font-weight: 500; color: var(--text-primary); display: flex; flex-direction: column; gap: 0.125rem; }
  .hint { font-size: 0.6875rem; font-weight: 400; color: var(--text-secondary); }

  .model-select {
    width: 100%; padding: 0.4375rem 0.625rem; border: 1px solid var(--border-color); border-radius: 6px;
    background: var(--input-bg, var(--surface-color)); color: var(--text-primary); font-size: 0.875rem; cursor: pointer;
  }
  .model-select:focus { outline: none; border-color: var(--primary-color); box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1); }

  .model-note { margin: 0; font-size: 0.75rem; color: var(--text-secondary); line-height: 1.3; }

  /* Section */
  .section {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
  }

  .section-title {
    margin: 0;
    padding: 0.5rem 0.75rem;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--text-secondary);
    background: var(--surface-color);
    border-bottom: 1px solid var(--border-color);
  }

  .toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.625rem 0.75rem;
    cursor: pointer;
  }

  .toggle-text {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
  }

  .toggle-label {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-primary);
  }

  /* Toggle switch */
  .toggle-switch {
    position: relative;
    display: inline-block;
    width: 36px;
    height: 20px;
    flex-shrink: 0;
    cursor: pointer;
  }

  .toggle-switch input { opacity: 0; width: 0; height: 0; }

  .toggle-slider {
    position: absolute;
    inset: 0;
    background: var(--border-color);
    border-radius: 20px;
    transition: background 0.2s ease;
  }

  .toggle-slider::before {
    content: '';
    position: absolute;
    width: 16px;
    height: 16px;
    left: 2px;
    bottom: 2px;
    background: white;
    border-radius: 50%;
    transition: transform 0.2s ease;
  }

  .toggle-switch input:checked + .toggle-slider { background: var(--primary-color, var(--primary-color)); }
  .toggle-switch input:checked + .toggle-slider::before { transform: translateX(16px); }
</style>
