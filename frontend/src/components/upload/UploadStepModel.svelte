<script lang="ts">
  import './upload-shared.css';
  import Toggle from '$components/ui/Toggle.svelte';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import PerFileModelSelect from '$components/PerFileModelSelect.svelte';

  export let selectedWhisperModel: string | null = null;
  export let adminDefaultModel = 'large-v3-turbo';
  /**
   * Wire-compatible name: `false` (the default) means a summary IS generated.
   * The UI presents this positively (`generateSummary`, derived below) so the
   * toggle reads as an affirmative choice rather than a double negative — see
   * #751 item 3. The prop itself is unchanged so the parent's localStorage
   * restore/save and the `skip_summary` wire field need no migration.
   */
  export let skipSummary = false;
  /** False when the deployment owns the model choice: render it read-only. */
  export let modelChoiceEnabled = true;

  $: generateSummary = !skipSummary;

  const dispatch = createEventDispatcher<{
    change: { selectedWhisperModel: string | null; skipSummary: boolean };
  }>();

  function emitChange() {
    dispatch('change', { selectedWhisperModel, skipSummary });
  }

  function toggleGenerateSummary(checked: boolean) {
    skipSummary = !checked;
    emitChange();
  }
</script>

<div class="step-model">
  <p class="step-hint">{$t('uploader.modelHint')}</p>

  {#if modelChoiceEnabled}
  <div class="stacked-field">
    <label for="whisper-model-select">
      {$t('uploader.whisperModel')}
      <span class="stacked-field-hint">{$t('uploader.whisperModelHint')}</span>
    </label>
    <PerFileModelSelect
      id="whisper-model-select"
      bind:value={selectedWhisperModel}
      adminModel={adminDefaultModel}
      on:change={emitChange}
    />
    {#if selectedWhisperModel === 'base'}
      <p class="model-note">{$t('uploader.fastProcessingHint')}</p>
    {/if}
  </div>
  {:else}
  <p class="model-managed" data-testid="model-managed">{$t('uploader.modelManaged')}</p>
  {/if}

  <!-- AI Summary -->
  <div class="section">
    <h4 class="section-title">{$t('upload.aiSummary')}</h4>
    <div class="toggle-row">
      <label class="toggle-text" for="generate-summary-toggle">
        <span class="toggle-label">{$t('upload.generateSummary')}</span>
        <span class="stacked-field-hint">{$t('upload.generateSummaryHint')}</span>
      </label>
      <Toggle
        id="generate-summary-toggle"
        checked={generateSummary}
        onchange={toggleGenerateSummary}
      />
    </div>
  </div>
</div>

<style>
  .step-model { display: flex; flex-direction: column; gap: var(--space-4); }


  .model-managed { margin: 0; font-size: var(--font-size-sm); color: var(--text-secondary); }
  .model-note { margin: 0; font-size: var(--font-size-xs); color: var(--text-secondary); line-height: 1.3; }

  /* Section */
  .section {
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  .section-title {
    margin: 0;
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    background: var(--surface-color);
    border-bottom: 1px solid var(--border-color);
  }

  .toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-2-5) var(--space-3);
  }

  .toggle-text {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
  }

  .toggle-label {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-primary);
  }

</style>
