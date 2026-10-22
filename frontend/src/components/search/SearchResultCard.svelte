<script lang="ts">
  /**
   * One transcript hit as a compact search-engine result: title, one muted meta line,
   * 1-2 snippets. Everything else is progressive disclosure: "Show N more matches", and
   * "Details", which pins this hit in the page's info panel (wide layout) or expands the
   * same info card inline (narrow layout). See docs/design/search_results_ux_audit.md.
   */
  import { createEventDispatcher } from 'svelte';
  import type { SearchHit, SearchOccurrence as SearchOccurrenceType } from '$stores/search';
  import { t } from '$stores/locale';
  import { prefetchFileDetails, cancelPrefetch } from '$lib/prefetch';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import { formatClock, formatDate } from '$lib/utils/formatting';
  import SearchOccurrence from './SearchOccurrence.svelte';
  import SearchPreviewButton, { type PreviewPlaybackState } from './SearchPreviewButton.svelte';
  import SearchResultInfoCard from './SearchResultInfoCard.svelte';

  export let hit: SearchHit;
  export let activePreview: { fileUuid: string; startTime: number; playing: boolean } | null = null;
  /** Wide layout: "Details" pins the page's info panel instead of expanding inline. */
  export let panelMode = false;
  /** This hit is pinned in the info panel. */
  export let selected = false;
  /** id of the page's info panel, for `aria-controls` in panel mode. */
  export let panelId: string | undefined = undefined;

  const dispatch = createEventDispatcher();

  let expanded = false;
  let detailsOpen = false;

  $: maxVisible = hit.has_both_match_types ? 2 : 1;
  $: visibleOccurrences = expanded ? hit.occurrences : hit.occurrences.slice(0, maxVisible);
  $: hiddenCount = hit.occurrences.length - maxVisible;
  $: occurrencesId = `search-result-occurrences-${hit.file_uuid}`;
  $: inlineDetailsId = `search-result-details-${hit.file_uuid}`;

  // `preview` is passed in rather than read from the prop so the template call below
  // lists it as a dependency: a legacy-mode `{#each}` re-evaluates a call only when one
  // of its arguments changes, and the page swaps `activePreview` with `hit` unchanged.
  function playbackState(
    occurrence: SearchOccurrenceType,
    preview: typeof activePreview
  ): PreviewPlaybackState {
    if (
      !preview ||
      preview.fileUuid !== hit.file_uuid ||
      preview.startTime !== occurrence.start_time
    ) {
      return 'idle';
    }
    return preview.playing ? 'playing' : 'paused';
  }

  function handlePreviewClick(occurrence: SearchOccurrenceType) {
    if (playbackState(occurrence, activePreview) !== 'idle') {
      dispatch('togglePlayback');
      return;
    }
    dispatch('preview', {
      fileUuid: hit.file_uuid,
      title: hit.title,
      startTime: occurrence.start_time,
      speaker: occurrence.speaker,
      contentType: hit.content_type || '',
    });
  }

  function handleDetailsClick() {
    if (panelMode) {
      dispatch('toggleDetails', hit.file_uuid);
    } else {
      detailsOpen = !detailsOpen;
    }
  }
</script>

<!-- Hover and keyboard focus both drive the info panel; neither clears it on leave, so
     moving the pointer across to the panel never makes it vanish. -->
<article
  class="result-card"
  class:selected={panelMode && selected}
  on:mouseenter={() => dispatch('inspect', hit.file_uuid)}
  on:focusin={() => dispatch('inspect', hit.file_uuid)}
>
  <div class="result-header">
    <a
      href="/files/{hit.file_uuid}"
      class="result-title"
      on:mouseenter={() => prefetchFileDetails(hit.file_uuid)}
      on:mouseleave={cancelPrefetch}
    >
      {#if hit.content_type?.startsWith('video/')}
        <svg class="media-type-icon" aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="23 7 16 12 23 17 23 7"></polygon>
          <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
        </svg>
      {:else}
        <svg class="media-type-icon" aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
          <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
        </svg>
      {/if}
      <span dir="auto"
        >{#if hit.title_highlighted}{@html sanitizeHighlightHtml(hit.title_highlighted)}{:else}{hit.title}{/if}</span
      >
    </a>
    <p class="result-meta">
      {#if hit.upload_time}<span>{formatDate(hit.upload_time)}</span>{/if}
      {#if hit.duration > 0}<span>{formatClock(hit.duration)}</span>{/if}
      {#if hit.relevance_percent > 0}
        <span
          class="relevance"
          class:dual={hit.has_both_match_types}
          title={hit.has_both_match_types ? $t('search.dualMatchTooltip') : undefined}
        >{$t('search.relevanceShort', { percent: hit.relevance_percent })}</span>
      {/if}
    </p>
  </div>

  <ul class="occurrence-list" id={occurrencesId}>
    {#each visibleOccurrences as occurrence (occurrence.chunk_index)}
      <li class="occurrence-row">
        <SearchPreviewButton
          state={playbackState(occurrence, activePreview)}
          on:click={() => handlePreviewClick(occurrence)}
        />
        <div class="occurrence-body">
          {#if hit.has_both_match_types}
            <span class="occurrence-type-label" class:exact={occurrence.has_keyword_match}>
              {occurrence.has_keyword_match ? $t('search.exactLabel') : $t('search.relatedLabel')}
            </span>
          {/if}
          <SearchOccurrence {occurrence} fileUuid={hit.file_uuid} />
        </div>
      </li>
    {/each}
  </ul>

  <div class="result-actions">
    {#if hiddenCount > 0}
      <button
        type="button"
        class="text-btn show-more"
        aria-expanded={expanded}
        aria-controls={occurrencesId}
        on:click={() => (expanded = !expanded)}
      >
        {expanded ? $t('search.showLess') : $t('search.showMore', { count: hiddenCount })}
      </button>
    {/if}
    <button
      type="button"
      class="text-btn details-btn"
      aria-pressed={panelMode ? selected : undefined}
      aria-expanded={panelMode ? undefined : detailsOpen}
      aria-controls={panelMode ? panelId : inlineDetailsId}
      on:click={handleDetailsClick}
    >
      {$t('search.showDetails')}
    </button>
    <button
      type="button"
      class="text-btn view-transcript-btn"
      title={$t('search.viewFullTranscript')}
      on:click={() =>
        dispatch('viewTranscript', {
          fileUuid: hit.file_uuid,
          title: hit.title,
          occurrences: hit.occurrences,
        })}
    >
      {$t('search.viewTranscript')}
    </button>
  </div>

  {#if !panelMode && detailsOpen}
    <SearchResultInfoCard {hit} variant="inline" id={inlineDetailsId} />
  {/if}
</article>

<style>
  .result-card {
    padding: 0.875rem 1rem;
    margin-bottom: 0.5rem;
    border: 1px solid transparent;
    border-radius: 10px;
    transition:
      border-color 0.15s,
      background-color 0.15s;
  }

  .result-card:hover,
  .result-card:focus-within {
    background: var(--hover-color);
  }

  .result-card.selected {
    border-color: var(--primary-color);
    background: rgba(var(--primary-color-rgb), 0.06);
  }

  /* Search-engine convention: one run of link-coloured text, not a flex row — as
     inline-flex each highlighted <mark> became a flex item and split words. */
  .result-title {
    display: block;
    min-width: 0;
    font-size: var(--font-size-lg);
    font-weight: var(--font-weight-medium);
    color: var(--primary-on-surface);
    text-decoration: none;
    line-height: var(--line-height-tight);
    overflow-wrap: break-word;
  }

  .result-title:hover {
    text-decoration: underline;
  }

  .media-type-icon {
    vertical-align: -2px;
    margin-inline-end: 0.375rem;
    opacity: 0.7;
  }

  .result-meta {
    display: flex;
    flex-wrap: wrap;
    margin: 0.25rem 0 0.5rem;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .result-meta > span + span::before {
    content: '·';
    padding-inline: 0.375rem;
  }

  .relevance.dual {
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-medium);
  }

  .occurrence-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .occurrence-row {
    display: flex;
    align-items: flex-start;
    gap: 0.625rem;
  }

  .occurrence-body {
    flex: 1;
    min-width: 0;
  }

  .occurrence-type-label {
    display: inline-block;
    margin-bottom: 0.125rem;
    padding: 0 0.3125rem;
    border-radius: 3px;
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-medium);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    background: rgba(245, 158, 11, 0.1);
    color: #b45309;
  }

  .occurrence-type-label.exact {
    background: rgba(250, 204, 21, 0.15);
    color: #a16207;
  }

  :global([data-theme='dark']) .occurrence-type-label {
    background: rgba(251, 191, 36, 0.12);
    color: #fcd34d;
  }

  :global([data-theme='dark']) .occurrence-type-label.exact {
    color: #fbbf24;
  }

  .result-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.25rem 1rem;
    margin-top: 0.5rem;
    padding-inline-start: calc(28px + 0.625rem);
  }

  .text-btn {
    padding: 0.125rem 0;
    background: none;
    border: none;
    /* form-elements.css styles every bare <button> with a drop shadow and a hover
       scale + fill; a text link-button wants none of them. */
    box-shadow: none;
    color: var(--primary-on-surface);
    font-size: var(--font-size-sm);
    cursor: pointer;
  }

  .text-btn:hover {
    text-decoration: underline;
    background: none;
    box-shadow: none;
    transform: none;
  }

  .text-btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 2px;
  }

  .details-btn[aria-pressed='true'],
  .details-btn[aria-expanded='true'] {
    font-weight: var(--font-weight-medium);
  }

  @media (max-width: 768px) {
    .result-card {
      padding: 0.75rem 0.5rem;
    }

    .result-title {
      font-size: var(--font-size-body);
    }

    .result-actions {
      padding-inline-start: 0;
    }
  }
</style>
