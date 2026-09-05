<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { SearchHit, SearchOccurrence as SearchOccurrenceType } from '$stores/search';
  import { t } from '$stores/locale';
  import { prefetchFileDetails, cancelPrefetch } from '$lib/prefetch';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import SearchOccurrence from './SearchOccurrence.svelte';

  export let hit: SearchHit;
  export let activePreview: { fileUuid: string; startTime: number } | null = null;

  const dispatch = createEventDispatcher();

  let expanded = false;

  // Show more snippets for files with both keyword and semantic matches
  $: maxVisible = hit.has_both_match_types ? 3 : 2;

  $: visibleOccurrences = expanded ? hit.occurrences : hit.occurrences.slice(0, maxVisible);
  $: hiddenCount = hit.occurrences.length - maxVisible;

  function formatDate(dateStr: string): string {
    try {
      return new Date(dateStr).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return dateStr;
    }
  }

  function formatDuration(seconds: number): string {
    if (!seconds || seconds <= 0) return '';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function formatFileSize(bytes: number): string {
    if (!bytes || bytes <= 0) return '';
    if (bytes >= 1_073_741_824) return `${(bytes / 1_073_741_824).toFixed(1)} GB`;
    if (bytes >= 1_048_576) return `${(bytes / 1_048_576).toFixed(1)} MB`;
    return `${Math.round(bytes / 1024)} KB`;
  }

  function handlePreviewClick(occurrence: SearchOccurrenceType) {
    const isActive = activePreview
      && activePreview.fileUuid === hit.file_uuid
      && activePreview.startTime === occurrence.start_time;

    if (isActive) {
      dispatch('preview', null);
    } else {
      dispatch('preview', {
        fileUuid: hit.file_uuid,
        title: hit.title,
        startTime: occurrence.start_time,
        speaker: occurrence.speaker,
        contentType: hit.content_type || '',
      });
    }
  }

  function isOccurrenceActive(occurrence: SearchOccurrenceType): boolean {
    return !!(activePreview
      && activePreview.fileUuid === hit.file_uuid
      && activePreview.startTime === occurrence.start_time);
  }

</script>

<article class="result-card">
  <div class="result-header">
    <div class="result-header-top">
      <a href="/files/{hit.file_uuid}" class="result-title"
        on:mouseenter={() => prefetchFileDetails(hit.file_uuid)}
        on:mouseleave={cancelPrefetch}
      >
        {#if hit.content_type?.startsWith('video/')}
          <svg class="media-type-icon" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="23 7 16 12 23 17 23 7"></polygon>
            <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
          </svg>
        {:else}
          <svg class="media-type-icon" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
            <line x1="12" y1="19" x2="12" y2="23"></line>
            <line x1="8" y1="23" x2="16" y2="23"></line>
          </svg>
        {/if}
        {#if hit.title_highlighted}
          {@html sanitizeHighlightHtml(hit.title_highlighted)}
        {:else}
          {hit.title}
        {/if}
      </a>
      <button
        class="view-transcript-btn"
        on:click={() => dispatch('viewTranscript', {
          fileUuid: hit.file_uuid,
          title: hit.title,
          occurrences: hit.occurrences
        })}
        title={$t('search.viewFullTranscript')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
          <polyline points="14,2 14,8 20,8"></polyline>
          <line x1="16" y1="13" x2="8" y2="13"></line>
          <line x1="16" y1="17" x2="8" y2="17"></line>
        </svg>
        {$t('search.viewTranscript')}
      </button>
    </div>
    <div class="result-meta">
      {#if hit.speakers?.length > 0}
        <span class="meta-item speakers">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
            <circle cx="12" cy="7" r="4"></circle>
          </svg>
          {hit.speakers.join(', ')}
        </span>
      {/if}
      {#if hit.upload_time}
        <span class="meta-item date">
          {formatDate(hit.upload_time)}
        </span>
      {/if}
      {#if hit.duration > 0}
        <span class="meta-item meta-chip">
          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="10"></circle>
            <polyline points="12 6 12 12 16 14"></polyline>
          </svg>
          {formatDuration(hit.duration)}
        </span>
      {/if}
      {#if hit.file_size > 0}
        <span class="meta-item meta-chip">
          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path>
            <polyline points="13 2 13 9 20 9"></polyline>
          </svg>
          {formatFileSize(hit.file_size)}
        </span>
      {/if}
      {#if hit.tags?.length > 0}
        <span class="meta-item tags">
          {#each hit.tags as tag}
            <span class="tag">{tag}</span>
          {/each}
        </span>
      {/if}
      <span class="meta-item occurrences">
        <span class="occurrence-counts">
          {#if hit.keyword_occurrences > 0}
            <span class="match-badge keyword-count-badge">{hit.keyword_occurrences} {$t('search.keywordLabel')}</span>
          {/if}
          {#if hit.semantic_occurrences > 0}
            <span class="match-badge semantic-count-badge">{hit.semantic_occurrences} {$t('search.semanticLabel')}</span>
          {/if}
          {#if hit.has_both_match_types}
            <span class="match-badge dual-match-badge" title={$t('search.dualMatchTooltip')}>{hit.relevance_percent}%</span>
          {:else if hit.relevance_percent > 0}
            <span class="match-badge relevance-badge">{hit.relevance_percent}%</span>
          {/if}
        </span>
        {#if hit.total_occurrences > hit.occurrences.length}
          <span class="showing-of-total">
            {$t('search.showingOf', { shown: hit.occurrences.length, total: hit.total_occurrences })}
          </span>
        {/if}
        {#if hit.match_sources && hit.match_sources.length > 0}
          <span class="match-sources">
            {#each hit.match_sources.filter(s => s !== 'semantic') as source}
              <span class="match-badge source-{source}">{
                source === 'content' ? $t('search.contentMatch') :
                source === 'title' ? $t('search.titleMatch') :
                source === 'speaker' ? $t('search.speakerMatch') :
                source === 'metadata_speaker' ? $t('search.metadataSpeakerMatch') : source
              }</span>
            {/each}
          </span>
        {/if}
      </span>
    </div>
  </div>

  <div class="result-body">
    {#each visibleOccurrences as occurrence (occurrence.chunk_index)}
      <div class="occurrence-row">
        {#if hit.has_both_match_types}
          <span class="occurrence-type-label" class:exact={occurrence.has_keyword_match} class:related={!occurrence.has_keyword_match}>
            {occurrence.has_keyword_match ? $t('search.exactLabel') : $t('search.relatedLabel')}
          </span>
        {/if}
        <SearchOccurrence {occurrence} fileUuid={hit.file_uuid} />
        <button
          class="preview-btn"
          class:active={isOccurrenceActive(occurrence)}
          on:click={() => handlePreviewClick(occurrence)}
          title={isOccurrenceActive(occurrence) ? $t('search.stopPreview') : $t('search.previewClip')}
        >
          {#if isOccurrenceActive(occurrence)}
            <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="currentColor" stroke="none">
              <rect x="6" y="5" width="4" height="14" rx="1"></rect>
              <rect x="14" y="5" width="4" height="14" rx="1"></rect>
            </svg>
          {:else}
            <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="currentColor" stroke="none">
              <path d="M8 5.14v13.72a1 1 0 0 0 1.5.86l11.04-6.86a1 1 0 0 0 0-1.72L9.5 4.28a1 1 0 0 0-1.5.86z"></path>
            </svg>
          {/if}
        </button>
      </div>
    {/each}

    {#if hiddenCount > 0 && !expanded}
      <button class="show-more" on:click={() => (expanded = true)}>
        {$t('search.showMore', { count: hiddenCount })}
      </button>
    {:else if expanded && hit.occurrences.length > maxVisible}
      <button class="show-more" on:click={() => (expanded = false)}>
        {$t('search.showLess')}
      </button>
    {/if}

  </div>
</article>

<style>
  .result-card {
    background: var(--surface-color, #fff);
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin-bottom: 0.75rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .result-card:hover {
    border-color: var(--primary-color, #4f46e5);
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
  }

  .result-header {
    margin-bottom: 0.5rem;
  }

  .result-header-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .result-title {
    font-size: 1.0625rem;
    font-weight: 600;
    color: var(--primary-color, #4f46e5);
    text-decoration: none;
    line-height: 1.3;
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
  }

  .media-type-icon {
    flex-shrink: 0;
    opacity: 0.7;
  }

  .result-title:hover {
    text-decoration: underline;
  }

  .result-meta {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.375rem;
    font-size: 0.8125rem;
    color: var(--text-secondary, #6b7280);
  }

  .meta-item {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }

  .meta-item.speakers svg {
    color: var(--text-secondary, #9ca3af);
  }

  .tag {
    display: inline-block;
    background: var(--surface-color, #f3f4f6);
    border: 1px solid var(--border-color, #e5e7eb);
    padding: 0.0625rem 0.375rem;
    border-radius: 4px;
    font-size: 0.6875rem;
  }

  .meta-item.occurrences {
    color: var(--text-secondary, #9ca3af);
    font-size: 0.75rem;
  }

  .occurrence-counts {
    display: inline-flex;
    gap: 4px;
    align-items: center;
  }

  .keyword-count-badge {
    background: rgba(250, 204, 21, 0.15);
    color: #a16207;
  }

  :global([data-theme='dark']) .keyword-count-badge {
    background: rgba(250, 204, 21, 0.12);
    color: #fbbf24;
  }

  .semantic-count-badge {
    background: rgba(245, 158, 11, 0.1);
    color: #d97706;
  }

  :global([data-theme='dark']) .semantic-count-badge {
    background: rgba(251, 191, 36, 0.12);
    color: #fcd34d;
  }

  .showing-of-total {
    font-size: 0.6875rem;
    color: var(--text-secondary, #9ca3af);
    margin-left: 4px;
  }

  .occurrence-row {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
  }

  .occurrence-row :global(.occurrence) {
    flex: 1;
  }

  .preview-btn {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    margin-top: 0.375rem;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: none;
    color: var(--primary-color, #4f46e5);
    cursor: pointer;
    transition: all 0.15s;
  }

  .preview-btn:hover {
    background: rgba(79, 70, 229, 0.1);
    transform: scale(1.1);
  }

  .preview-btn.active {
    color: var(--error-color, #ef4444);
  }

  .preview-btn.active:hover {
    background: rgba(239, 68, 68, 0.1);
  }

  .show-more {
    display: block;
    width: 100%;
    padding: 0.5rem;
    margin-top: 0.25rem;
    background: none;
    border: none;
    color: var(--primary-color, #4f46e5);
    font-size: 0.8125rem;
    cursor: pointer;
    text-align: center;
  }

  .show-more:hover {
    text-decoration: underline;
  }

  .match-sources {
    display: inline-flex;
    gap: 4px;
    margin-left: 6px;
    flex-wrap: wrap;
  }

  .match-badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 0.7rem;
    font-weight: 500;
    line-height: 1.4;
  }

  .source-content {
    background: var(--color-success-bg, #dcfce7);
    color: var(--color-success-text, #166534);
  }

  :global([data-theme='dark']) .source-content {
    background: rgba(34, 197, 94, 0.15);
    color: #4ade80;
  }

  .source-title {
    background: var(--color-info-bg, #dbeafe);
    color: var(--color-info-text, #1e40af);
  }

  :global([data-theme='dark']) .source-title {
    background: rgba(59, 130, 246, 0.15);
    color: #60a5fa;
  }

  .source-speaker {
    background: rgba(16, 185, 129, 0.1);
    color: #059669;
  }

  :global([data-theme='dark']) .source-speaker {
    background: rgba(16, 185, 129, 0.12);
    color: #34d399;
  }

  .semantic-badge {
    background: rgba(245, 158, 11, 0.1);
    color: #d97706;
  }

  :global([data-theme='dark']) .semantic-badge {
    background: rgba(251, 191, 36, 0.15);
    color: #fcd34d;
  }

  .source-metadata_speaker {
    background: #ccfbf1;
    color: #0d9488;
  }

  :global([data-theme='dark']) .source-metadata_speaker {
    background: rgba(13, 148, 136, 0.15);
    color: #5eead4;
  }

  .dual-match-badge {
    background: rgba(79, 70, 229, 0.12);
    color: #4f46e5;
    font-weight: 600;
  }

  :global([data-theme='dark']) .dual-match-badge {
    background: rgba(129, 140, 248, 0.15);
    color: #818cf8;
  }

  .relevance-badge {
    background: var(--hover-color, #f1f5f9);
    color: var(--text-secondary, #6b7280);
  }

  :global([data-theme='dark']) .relevance-badge {
    background: rgba(255, 255, 255, 0.08);
    color: #9ca3af;
  }

  .occurrence-type-label {
    display: inline-block;
    flex-shrink: 0;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    line-height: 1.4;
    margin-top: 0.5rem;
    align-self: flex-start;
  }

  .occurrence-type-label.exact {
    background: rgba(250, 204, 21, 0.15);
    color: #a16207;
  }

  :global([data-theme='dark']) .occurrence-type-label.exact {
    background: rgba(250, 204, 21, 0.12);
    color: #fbbf24;
  }

  .occurrence-type-label.related {
    background: rgba(245, 158, 11, 0.1);
    color: #d97706;
  }

  :global([data-theme='dark']) .occurrence-type-label.related {
    background: rgba(251, 191, 36, 0.12);
    color: #fcd34d;
  }

  /* Mobile responsive */
  @media (max-width: 768px) {
    .result-card {
      padding: 0.75rem;
      overflow-x: hidden;
    }

    .result-header-top {
      flex-direction: column;
      gap: 0.5rem;
    }

    .result-title {
      font-size: 0.9375rem;
      word-break: break-word;
      overflow-wrap: break-word;
    }

    .result-meta {
      gap: 0.375rem;
    }

    .meta-item.speakers {
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .meta-item.occurrences {
      flex-wrap: wrap;
    }

    .occurrence-counts {
      flex-wrap: wrap;
    }

    .match-sources {
      margin-left: 0;
    }

    .meta-item.tags {
      flex-wrap: wrap;
      max-width: 100%;
    }

    .tag {
      max-width: 120px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .result-meta {
      flex-wrap: wrap;
    }

    .occurrence-row {
      gap: 0.25rem;
    }

    .view-transcript-btn {
      align-self: flex-start;
      font-size: 0.75rem;
      padding: 0.25rem 0.5rem;
    }
  }

  .view-transcript-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    flex-shrink: 0;
    padding: 0.375rem 0.75rem;
    background: none;
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 6px;
    color: var(--primary-color, #4f46e5);
    font-size: 0.8125rem;
    cursor: pointer;
    transition: all 0.15s;
  }

  .view-transcript-btn:hover {
    background: rgba(79, 70, 229, 0.08);
    border-color: var(--primary-color, #4f46e5);
  }

  :global([data-theme='dark']) .view-transcript-btn:hover {
    background: rgba(129, 140, 248, 0.12);
  }

  .meta-chip {
    background: var(--hover-color, #f1f5f9);
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-secondary, #6b7280);
    gap: 0.25rem;
  }

  :global([data-theme='dark']) .meta-chip {
    background: rgba(255, 255, 255, 0.07);
    border-color: rgba(255, 255, 255, 0.12);
    color: #9ca3af;
  }
</style>
