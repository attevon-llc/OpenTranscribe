<script lang="ts">
  import { onMount } from 'svelte';
  import { optionWithFlags } from '$lib/settings/asrCapabilities';
  import {
    loadPerFileFacts,
    optionName,
    perFileOptions,
    type PerFileFacts,
  } from '$lib/settings/perFileModel';
  import { t } from '$stores/locale';
  import type { TranscriptionSystemDefaults } from '$lib/api/transcriptionSettings';

  export let activeTab: 'file' | 'url' | 'record' = 'file';
  export let fileName = '';
  export let mediaUrl = '';
  export let selectedTags: string[] = [];
  export let selectedCollections: Array<{uuid: string; name: string}> = [];
  export let minSpeakers: number | null = null;
  export let maxSpeakers: number | null = null;
  export let numSpeakers: number | null = null;
  /** Why the speaker count is not used for this file, or null when it is. */
  export let speakerCountNote: 'off' | 'fast' | null = null;
  export let skipSummary = false;
  /** The uploader stated the speakers agreed to voice identification. */
  export let voiceprintConsent = false;
  export let selectedWhisperModel: string | null = null;
  export let adminDefaultModel = 'large-v3-turbo';
  export let transcriptionSystemDefaults: TranscriptionSystemDefaults | null = null;
  export let tagsSkipped = false;
  export let collectionsSkipped = false;
  export let extractionChoice: 'extract' | 'full' | null = null;

  function getMediaLabel(): string {
    if (activeTab === 'file') return fileName || '—';
    if (activeTab === 'url') {
      if (!mediaUrl) return '—';
      try {
        const url = new URL(mediaUrl);
        return url.hostname + url.pathname.slice(0, 30) + (url.pathname.length > 30 ? '...' : '');
      } catch { return mediaUrl.slice(0, 50); }
    }
    return $t('uploader.recording');
  }

  function getSpeakerLabel(): string {
    if (speakerCountNote === 'off') return $t('uploader.reviewSpeakersOff');
    if (speakerCountNote === 'fast') return $t('uploader.reviewSpeakersFast');
    if (numSpeakers !== null) return `${numSpeakers} (fixed)`;
    if (minSpeakers !== null || maxSpeakers !== null) {
      const min = minSpeakers ?? (transcriptionSystemDefaults?.min_speakers ?? 1);
      const max = maxSpeakers ?? (transcriptionSystemDefaults?.max_speakers ?? 20);
      return `${min} – ${max}`;
    }
    return $t('uploader.reviewDefaults');
  }

  // The same option wording as the Model step, so the review names what will really run (a saved
  // cloud provider, not this server's model) with the same flags.
  let facts: PerFileFacts | null = null;
  onMount(() => {
    void loadPerFileFacts().then((loaded) => (facts = loaded));
  });

  $: modelLabel = (() => {
    const options = perFileOptions(facts, adminDefaultModel);
    const chosen = options.find((option) => option.value === selectedWhisperModel) ?? options[0];
    return optionWithFlags(optionName(chosen, $t), chosen.capability, $t);
  })();
</script>

<div class="step-review">
  <div class="review-card">
    <div class="review-row">
      <span class="review-label">{$t('uploader.reviewMedia')}</span>
      <span class="review-value" title={activeTab === 'url' ? mediaUrl : fileName}>{getMediaLabel()}</span>
    </div>

    {#if extractionChoice}
      <div class="review-row">
        <span class="review-label">{$t('common.processing')}</span>
        <span class="review-value">
          {#if extractionChoice === 'extract'}
            <span class="review-chip extract">{$t('uploader.reviewExtractAudioOnly')}</span>
          {:else}
            {$t('extraction.uploadFullVideo')}
          {/if}
        </span>
      </div>
    {/if}

    <div class="review-row">
      <span class="review-label">{$t('uploader.reviewTags')}</span>
      <span class="review-value">
        {#if tagsSkipped}
          <em>{$t('uploader.reviewSkipped')}</em>
        {:else if selectedTags.length > 0}
          <span class="review-chips">
            {#each selectedTags as tag (tag)}
              <span class="review-chip tag">{tag}</span>
            {/each}
          </span>
        {:else}
          <em>{$t('uploader.reviewNone')}</em>
        {/if}
      </span>
    </div>

    <div class="review-row">
      <span class="review-label">{$t('uploader.reviewCollections')}</span>
      <span class="review-value">
        {#if collectionsSkipped}
          <em>{$t('uploader.reviewSkipped')}</em>
        {:else if selectedCollections.length > 0}
          <span class="review-chips">
            {#each selectedCollections as col (col.uuid)}
              <span class="review-chip collection">{col.name}</span>
            {/each}
          </span>
        {:else}
          <em>{$t('uploader.reviewNone')}</em>
        {/if}
      </span>
    </div>

    <div class="review-row">
      <span class="review-label">{$t('uploader.reviewSpeakers')}</span>
      <span class="review-value">{getSpeakerLabel()}</span>
    </div>

    {#if speakerCountNote === null}
      <div class="review-row" data-testid="review-voiceprint-consent">
        <span class="review-label">{$t('uploader.voiceprintConsent.reviewLabel')}</span>
        <span class="review-value">
          {voiceprintConsent
            ? $t('uploader.voiceprintConsent.reviewStated')
            : $t('uploader.voiceprintConsent.reviewNone')}
        </span>
      </div>
    {/if}

    <div class="review-row">
      <span class="review-label">{$t('uploader.reviewModel')}</span>
      <span class="review-value">{modelLabel}</span>
    </div>

    <div class="review-row">
      <span class="review-label">{$t('upload.aiSummary')}</span>
      <span class="review-value">{skipSummary ? $t('common.no') : $t('common.yes')}</span>
    </div>
  </div>

</div>

<style>
  .step-review { display: flex; flex-direction: column; gap: var(--space-3); }

  .review-card {
    display: flex; flex-direction: column; gap: 0;
    border: 1px solid var(--border-color); border-radius: 8px;
    background: var(--surface-color); overflow: hidden;
  }

  .review-row {
    display: flex; align-items: flex-start; gap: var(--space-3);
    padding: var(--space-2-5) var(--space-3-5);
    border-bottom: 1px solid var(--border-color);
  }

  .review-row:last-child { border-bottom: none; }

  .review-label {
    flex-shrink: 0; width: 90px;
    font-size: 0.75rem; font-weight: var(--font-weight-medium); color: var(--text-secondary);
    padding-top: var(--space-0-5);
  }

  .review-value {
    flex: 1; font-size: 0.8125rem; color: var(--text-primary);
    word-break: break-word;
  }

  .review-value em { color: var(--text-tertiary); font-style: italic; }

  .review-chips { display: flex; flex-wrap: wrap; gap: var(--space-1); }

  .review-chip {
    display: inline-block; padding: var(--space-0-5) 0.4375rem;
    border-radius: 999px; font-size: 0.6875rem; font-weight: 500;
  }

  .review-chip.tag {
    background: var(--tag-bg); color: var(--tag-color);
    border: 1px solid var(--tag-border);
  }

  :global([data-theme='dark']) .review-chip.tag { background: rgba(22, 163, 74, 0.15); border-color: rgba(22, 163, 74, 0.3); }

  .review-chip.collection {
    background: var(--primary-bg); color: var(--primary-on-surface);
    border: 1px solid var(--primary-border);
  }

  :global([data-theme='dark']) .review-chip.collection { background: rgba(var(--accent-rgb), 0.15); border-color: rgba(var(--accent-rgb), 0.3); }

  .review-chip.extract {
    background: rgba(16, 185, 129, 0.12); color: #10b981;
    border: 1px solid rgba(16, 185, 129, 0.25);
  }
</style>
