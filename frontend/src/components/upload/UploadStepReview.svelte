<script lang="ts">
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
  export let skipSummary = false;
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
    if (numSpeakers !== null) return `${numSpeakers} (fixed)`;
    if (minSpeakers !== null || maxSpeakers !== null) {
      const min = minSpeakers ?? (transcriptionSystemDefaults?.min_speakers ?? 1);
      const max = maxSpeakers ?? (transcriptionSystemDefaults?.max_speakers ?? 20);
      return `${min} – ${max}`;
    }
    return $t('uploader.reviewDefaults');
  }

  function getModelLabel(): string {
    return selectedWhisperModel === 'base'
      ? $t('uploader.fastProcessing')
      : `${$t('uploader.highQuality')} (${adminDefaultModel})`;
  }
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
            {#each selectedTags as tag}
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
            {#each selectedCollections as col}
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

    <div class="review-row">
      <span class="review-label">{$t('uploader.reviewModel')}</span>
      <span class="review-value">{getModelLabel()}</span>
    </div>

    {#if skipSummary}
      <div class="review-row">
        <span class="review-label">{$t('upload.skipSummary')}</span>
        <span class="review-value">{$t('common.yes')}</span>
      </div>
    {/if}
  </div>

</div>

<style>
  .step-review { display: flex; flex-direction: column; gap: 0.75rem; }

  .review-card {
    display: flex; flex-direction: column; gap: 0;
    border: 1px solid var(--border-color); border-radius: 8px;
    background: var(--surface-color); overflow: hidden;
  }

  .review-row {
    display: flex; align-items: flex-start; gap: 0.75rem;
    padding: 0.625rem 0.875rem;
    border-bottom: 1px solid var(--border-color);
  }

  .review-row:last-child { border-bottom: none; }

  .review-label {
    flex-shrink: 0; width: 90px;
    font-size: 0.75rem; font-weight: 600; color: var(--text-secondary);
    text-transform: uppercase; letter-spacing: 0.02em;
    padding-top: 0.125rem;
  }

  .review-value {
    flex: 1; font-size: 0.8125rem; color: var(--text-primary);
    word-break: break-word;
  }

  .review-value em { color: var(--text-tertiary, #94a3b8); font-style: italic; }

  .review-chips { display: flex; flex-wrap: wrap; gap: 0.25rem; }

  .review-chip {
    display: inline-block; padding: 0.125rem 0.4375rem;
    border-radius: 999px; font-size: 0.6875rem; font-weight: 500;
  }

  .review-chip.tag {
    background: var(--tag-bg, #f0fdf4); color: var(--tag-color, #16a34a);
    border: 1px solid var(--tag-border, #bbf7d0);
  }

  :global(.dark) .review-chip.tag { background: rgba(22, 163, 74, 0.15); border-color: rgba(22, 163, 74, 0.3); }

  .review-chip.collection {
    background: var(--primary-bg, #eff6ff); color: var(--primary-color, #3b82f6);
    border: 1px solid var(--primary-border, #bfdbfe);
  }

  :global(.dark) .review-chip.collection { background: rgba(59, 130, 246, 0.15); border-color: rgba(59, 130, 246, 0.3); }

  .review-chip.extract {
    background: rgba(16, 185, 129, 0.12); color: #10b981;
    border: 1px solid rgba(16, 185, 129, 0.25);
  }
</style>
