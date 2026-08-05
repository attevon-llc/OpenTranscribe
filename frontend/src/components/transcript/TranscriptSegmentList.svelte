<script lang="ts">
  import type { GroupedSegmentView } from '$lib/types/media';
  import type { Segment, Speaker } from '$lib/types/speaker';
  import { createEventDispatcher, onMount, onDestroy, tick } from 'svelte';
  import ScrollbarIndicator from '$components/ScrollbarIndicator.svelte';
  import SegmentSpeakerDropdown from '$components/SegmentSpeakerDropdown.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { type TranscriptSegment } from '$lib/utils/scrollbarCalculations';
  import { highlightTextWithMatches, type SearchMatch } from '$lib/utils/searchHighlight';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';

  export let file: any = null;
  export let groupedTranscriptSegments: GroupedSegmentView[] = [];
  export let transcriptSegments: TranscriptSegment[] = [];
  export let speakerList: Speaker[] = [];
  export let currentTime: number = 0;
  export let diarizationDisabled: boolean = false;
  export let isEditingTranscript: boolean = false;
  export let editingSegmentId: string | number | null = null;
  export let editingSegmentText: string = '';
  export let savingTranscript: boolean = false;

  // Search props
  export let searchQuery: string = '';
  export let searchMatches: SearchMatch[] = [];
  export let currentMatchIndex: number = -1;

  // Pagination props
  export let totalSegments: number = 0;
  export let hasMoreSegments: boolean = false;
  export let loadingMoreSegments: boolean = false;

  const dispatch = createEventDispatcher();

  // Infinite scroll sentinel element
  let infiniteScrollSentinel: HTMLElement | null = null;
  let infiniteScrollObserver: IntersectionObserver | null = null;

  // Scroll progress tracking (reading progress bar)
  let scrollProgress: number = 0;
  let transcriptDisplayElement: HTMLElement | null = null;

  // Scrollbar indicator state
  let transcriptContainer: HTMLElement | null = null;
  let scrollbarIndicatorEnabled: boolean = true;

  // Calculate loaded segments info
  $: loadedSegments = file?.transcript_segments?.length || 0;

  // Reading progress is measured in SEGMENT INDEX, not scroll offset, so the bar
  // reflects position in the whole transcript rather than in the loaded page.
  //
  // This used to run from an unthrottled `on:scroll` handler that, on every single
  // scroll event, did a `querySelectorAll('[data-seg-index]')` across the entire
  // list and then walked it reading `offsetTop` — an O(n) DOM query plus a forced
  // synchronous layout per event, on a list that holds thousands of segments.
  // An IntersectionObserver gets the same answer with no scroll handler, no DOM
  // query and no layout read: the browser tells us which segments are on screen
  // and the smallest index among them is the top one.
  // Plain Set on purpose: nothing in the markup reads it, so making it reactive
  // would only add invalidation churn on every intersection callback.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  const visibleSegmentIndices = new Set<number>();
  let progressObserver: IntersectionObserver | null = null;

  function updateScrollProgress() {
    if (totalSegments === 0 || visibleSegmentIndices.size === 0) {
      scrollProgress = 0;
      return;
    }
    scrollProgress = Math.round((Math.min(...visibleSegmentIndices) / totalSegments) * 100);
  }

  function handleSegmentVisibility(entries: IntersectionObserverEntry[]) {
    for (const entry of entries) {
      const raw = (entry.target as HTMLElement).dataset.segIndex;
      if (raw === undefined) continue;
      const index = parseInt(raw, 10);
      if (Number.isNaN(index)) continue;
      if (entry.isIntersecting) {
        visibleSegmentIndices.add(index);
      } else {
        visibleSegmentIndices.delete(index);
      }
    }
    updateScrollProgress();
  }

  // Re-target the observer when the segment set changes (pagination appends rows).
  // O(n) once per data change instead of O(n) per scroll event.
  async function refreshProgressObserver() {
    if (typeof IntersectionObserver === 'undefined') return;
    await tick();
    if (!transcriptDisplayElement) return;

    if (progressObserver) {
      progressObserver.disconnect();
    } else {
      progressObserver = new IntersectionObserver(handleSegmentVisibility, {
        root: transcriptDisplayElement,
      });
    }
    visibleSegmentIndices.clear();
    transcriptDisplayElement
      .querySelectorAll('[data-seg-index]')
      .forEach((el) => progressObserver?.observe(el));
  }

  $: if (transcriptDisplayElement && groupedTranscriptSegments) {
    void refreshProgressObserver();
  }

  // A group renders as one row, so it needs a key that is stable across reorders
  // and appends. Overlap groups are identified by their group id, plain groups by
  // their single segment's uuid; the prefix keeps the two id spaces from colliding.
  function groupKey(group: {
    isOverlapGroup?: boolean;
    overlapGroupId?: string;
    segments?: { uuid?: string | number }[];
    startSegmentIndex?: number;
  }): string {
    if (group?.isOverlapGroup && group.overlapGroupId) return `g:${group.overlapGroupId}`;
    const uuid = group?.segments?.[0]?.uuid;
    return uuid != null ? `s:${uuid}` : `i:${group?.startSegmentIndex}`;
  }

  // Set up infinite scroll observer
  onMount(() => {
    if (typeof IntersectionObserver !== 'undefined') {
      infiniteScrollObserver = new IntersectionObserver(
        (entries) => {
          const entry = entries[0];
          if (entry?.isIntersecting && hasMoreSegments && !loadingMoreSegments) {
            dispatch('loadMore');
          }
        },
        { rootMargin: '200px' } // Trigger 200px before reaching the sentinel
      );
    }
  });

  onDestroy(() => {
    if (infiniteScrollObserver) {
      infiniteScrollObserver.disconnect();
      infiniteScrollObserver = null;
    }
    if (progressObserver) {
      progressObserver.disconnect();
      progressObserver = null;
    }
  });

  // Observe the sentinel element when it's available
  $: if (infiniteScrollSentinel && infiniteScrollObserver) {
    infiniteScrollObserver.observe(infiniteScrollSentinel);
  }

  // Map segment uuid -> its index in file.transcript_segments. Search matches are
  // keyed by that flat index; when segments come from the backend `grouped_segments`
  // payload they are DIFFERENT object refs than `transcript_segments`, so a plain
  // `indexOf` returns -1 and highlights silently vanish. Look up by uuid instead.
  $: segmentIndexByUuid = (() => {
    const map = new Map<string | number, number>();
    (file?.transcript_segments || []).forEach((s: Segment, i: number) => {
      if (s?.uuid != null) map.set(s.uuid, i);
    });
    return map;
  })();

  // Helper to get original segment index for search highlighting
  function getOriginalSegmentIndex(segment: Segment): number {
    if (segment?.uuid != null && segmentIndexByUuid.has(segment.uuid)) {
      return segmentIndexByUuid.get(segment.uuid) as number;
    }
    return file?.transcript_segments?.indexOf(segment) ?? -1;
  }

  // Handle scrollbar indicator click to seek to playhead
  function handleSeekToPlayhead(event: CustomEvent) {
    const { currentTime: seekTime, targetSegment } = event.detail;

    if (targetSegment) {
      // Scroll to the current segment
      const segmentElement = document.querySelector(`[data-segment-id="${targetSegment.uuid}"]`);
      if (segmentElement) {
        segmentElement.scrollIntoView({
          behavior: 'smooth',
          block: 'center',
          inline: 'nearest'
        });
      }
    }

    // Also dispatch to parent for potential video seeking
    dispatch('seekToPlayhead', { time: seekTime, segment: targetSegment });
  }

  // Check if scrollbar indicator should be enabled
  $: {
    scrollbarIndicatorEnabled = !!(
      transcriptSegments &&
      transcriptSegments.length > 10 && // Only show for transcripts with substantial content
      currentTime >= 0 &&
      !isEditingTranscript // Hide during transcript editing
    );
  }

  function formatSimpleTimestamp(seconds: number): string {
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = Math.floor(seconds % 60);
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`;
  }

  function handleSegmentClick(startTime: number) {
    dispatch('segmentClick', { startTime });
  }

  function editSegment(segment: Segment) {
    dispatch('editSegment', { segment });
  }

  function saveSegment(segment: Segment) {
    dispatch('saveSegment', { segment });
  }

  function cancelEditSegment() {
    dispatch('cancelEditSegment');
  }

  function handleSegmentSpeakerChange(event: CustomEvent) {
    dispatch('segmentSpeakerChange', event.detail);
  }

  function handleSpeakerCreated(event: CustomEvent) {
    dispatch('speakerCreatedFromDropdown', event.detail);
  }
</script>

<div bind:this={transcriptContainer} class="transcript-display-container">
  <!-- Reading progress bar at top -->
  {#if totalSegments > 0}
    <div class="reading-progress-bar">
      <div
        class="reading-progress-fill"
        style="width: {scrollProgress}%"
      ></div>
    </div>
  {/if}
  <div
    class="transcript-display"
    bind:this={transcriptDisplayElement}
  >
  {#each groupedTranscriptSegments as group (groupKey(group))}
    {#if group.isOverlapGroup}
      <!-- Overlap Group Container -->
      <div class="{diarizationDisabled ? '' : 'overlap-group'}" data-overlap-group-id="{group.overlapGroupId}" data-seg-index={group.startSegmentIndex}>
        {#if !diarizationDisabled}
        <div class="overlap-indicator">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
            <circle cx="9" cy="7" r="4"></circle>
            <path d="M23 21v-2a4 4 0 0 0-3-3.87"></path>
            <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
          </svg>
          <span class="overlap-label">{$t('transcript.overlapIndicator', { count: group.segments.length })}</span>
          <span class="overlap-time">{formatSimpleTimestamp(group.startTime)} - {formatSimpleTimestamp(group.endTime)}</span>
        </div>
        <div class="overlap-connector"></div>
        {/if}
        {#each group.segments as segment, segIdx (segment.uuid)}
          <div
            class="transcript-segment{diarizationDisabled ? '' : ' in-overlap'}"
            class:first-in-overlap={!diarizationDisabled && segIdx === 0}
            class:last-in-overlap={!diarizationDisabled && segIdx === group.segments.length - 1}
            data-segment-id="{segment.uuid}"
          >
            {#if editingSegmentId === segment.uuid}
              <div class="segment-edit-container">
                <div class="segment-time">{segment.display_timestamp || segment.formatted_timestamp || formatSimpleTimestamp(segment.start_time)}</div>
                {#if !diarizationDisabled}
                <div class="segment-speaker">{translateSpeakerLabel(segment.speaker?.display_name || segment.speaker?.name || segment.speaker_label || $t('fileDetail.unknownSpeaker'))}</div>
                {/if}
                <div class="segment-edit-input">
                  <textarea bind:value={editingSegmentText} rows="3" class="segment-textarea"></textarea>
                  <div class="segment-edit-actions">
                    <button
                      class="cancel-button"
                      on:click={cancelEditSegment}
                      title={$t('transcript.cancelSegmentTitle')}
                    >{$t('common.cancel')}</button>
                    <button
                      class="save-button"
                      on:click={() => saveSegment(segment)}
                      disabled={savingTranscript}
                      title={$t('transcript.saveSegmentTitle')}
                    >
                      {savingTranscript ? $t('common.saving') : $t('common.save')}
                    </button>
                  </div>
                </div>
              </div>
            {:else}
              <div class="segment-row">
                <button
                  class="segment-content{diarizationDisabled ? ' monologue' : ''}"
                  on:click={() => handleSegmentClick(segment.start_time)}
                  on:keydown={(e) => e.key === 'Enter' && handleSegmentClick(segment.start_time)}
                  title={$t('transcript.jumpToSegment')}
                >
                  <div class="segment-time">{segment.display_timestamp || segment.formatted_timestamp || formatSimpleTimestamp(segment.start_time)}</div>
                  {#if !diarizationDisabled}
                  <div
                    class="segment-speaker-wrapper"
                    role="button"
                    tabindex="0"
                    on:click|stopPropagation
                    on:keydown={(e) => e.key === 'Enter' && e.stopPropagation()}
                  >
                    <SegmentSpeakerDropdown
                      {segment}
                      speakers={speakerList}
                      mediaFileUuid={file?.uuid?.toString() || ''}
                      on:change={handleSegmentSpeakerChange}
                      on:speakerCreated={handleSpeakerCreated}
                    />
                  </div>
                  {/if}
                  <div class="segment-text">
                    {@html sanitizeHighlightHtml(highlightTextWithMatches(
                      segment.text,
                      searchQuery,
                      getOriginalSegmentIndex(segment),
                      searchMatches,
                      currentMatchIndex
                    ))}
                  </div>
                </button>
                <button
                  class="edit-button"
                  on:click|stopPropagation={() => editSegment(segment)}
                  title={$t('transcript.editSegment')}
                >
                  {$t('common.edit')}
                </button>
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {:else}
      <!-- Regular Segment -->
      {@const segment = group.segments[0]}
      <div
        class="transcript-segment"
        data-segment-id="{segment.uuid}"
        data-seg-index={group.startSegmentIndex}
      >
        {#if editingSegmentId === segment.uuid}
          <div class="segment-edit-container">
            <div class="segment-time">{segment.display_timestamp || segment.formatted_timestamp || formatSimpleTimestamp(segment.start_time)}</div>
            {#if !diarizationDisabled}
            <div class="segment-speaker">{translateSpeakerLabel(segment.speaker?.display_name || segment.speaker?.name || segment.speaker_label || $t('fileDetail.unknownSpeaker'))}</div>
            {/if}
            <div class="segment-edit-input">
              <textarea bind:value={editingSegmentText} rows="3" class="segment-textarea"></textarea>
              <div class="segment-edit-actions">
                <button
                  class="cancel-button"
                  on:click={cancelEditSegment}
                  title={$t('transcript.cancelSegmentTitle')}
                >{$t('common.cancel')}</button>
                <button
                  class="save-button"
                  on:click={() => saveSegment(segment)}
                  disabled={savingTranscript}
                  title={$t('transcript.saveSegmentTitle')}
                >
                  {savingTranscript ? $t('common.saving') : $t('common.save')}
                </button>
              </div>
            </div>
          </div>
        {:else}
          <div class="segment-row">
            <button
              class="segment-content{diarizationDisabled ? ' monologue' : ''}"
              on:click={() => handleSegmentClick(segment.start_time)}
              on:keydown={(e) => e.key === 'Enter' && handleSegmentClick(segment.start_time)}
              title={$t('transcript.jumpToSegment')}
            >
              <div class="segment-time">{segment.display_timestamp || segment.formatted_timestamp || formatSimpleTimestamp(segment.start_time)}</div>
              {#if !diarizationDisabled}
              <div
                class="segment-speaker-wrapper"
                role="button"
                tabindex="0"
                on:click|stopPropagation
                on:keydown={(e) => e.key === 'Enter' && e.stopPropagation()}
              >
                <SegmentSpeakerDropdown
                  {segment}
                  speakers={speakerList}
                  mediaFileUuid={file?.uuid?.toString() || ''}
                  on:change={handleSegmentSpeakerChange}
                  on:speakerCreated={handleSpeakerCreated}
                />
              </div>
              {/if}
              <div class="segment-text">
                {@html sanitizeHighlightHtml(highlightTextWithMatches(
                  segment.text,
                  searchQuery,
                  getOriginalSegmentIndex(segment),
                  searchMatches,
                  currentMatchIndex
                ))}
                {#if segment.confidence !== undefined && segment.confidence !== null && segment.confidence < 0.7}
                  <span class="low-confidence-dot" title={$t('transcript.segmentLowConfidence') + ': ' + Math.round(segment.confidence * 100) + '%'}>●</span>
                {/if}
              </div>
            </button>
            <button
              class="edit-button"
              on:click|stopPropagation={() => editSegment(segment)}
              title={$t('transcript.editSegment')}
            >
              {$t('common.edit')}
            </button>
          </div>
        {/if}
      </div>
    {/if}
  {/each}

  <!-- Infinite scroll sentinel and loading indicator -->
  {#if hasMoreSegments}
    <div
      class="infinite-scroll-sentinel"
      bind:this={infiniteScrollSentinel}
    >
      {#if loadingMoreSegments}
        <div class="loading-more-indicator">
          <Spinner size="small" />
          <span>{$t('transcript.loadingMoreSegments')}</span>
        </div>
      {/if}
    </div>
  {/if}
  </div>

  <!-- Segments loaded info -->
  {#if totalSegments > 0 && loadedSegments < totalSegments}
    <div class="segments-loaded-info">
      <span class="segments-count">{$t('transcript.segmentsLoaded', { loaded: loadedSegments, total: totalSegments })}</span>
      {#if loadingMoreSegments}
        <span class="loading-indicator">
          <Spinner size="small" />
          {$t('common.loading')}
        </span>
      {/if}
    </div>
  {/if}

  <!-- Scrollbar Position Indicator - Inside transcript-display-container for proper positioning -->
  {#if scrollbarIndicatorEnabled}
    <ScrollbarIndicator
      {currentTime}
      {transcriptSegments}
      containerElement={transcriptContainer?.querySelector('.transcript-display')}
      disabled={isEditingTranscript || !file?.transcript_segments?.length}
      on:seekToPlayhead={handleSeekToPlayhead}
    />
  {/if}
</div>

<style>
  /* Reading progress bar - horizontal bar at top showing scroll position */
  .reading-progress-bar {
    position: sticky;
    top: 0;
    left: 0;
    right: 0;
    height: 3px;
    background: var(--border-color);
    z-index: 10;
    border-radius: 0;
  }

  .reading-progress-fill {
    height: 100%;
    background: #3b82f6;
    transition: width 0.1s ease-out;
    border-radius: 0;
  }

  /* Infinite scroll styles */
  .infinite-scroll-sentinel {
    min-height: 1px;
    padding: 8px 0;
  }

  .loading-more-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 16px;
    color: var(--text-muted);
    font-size: 14px;
  }

  /* Segments loaded info bar */
  .segments-loaded-info {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 8px 16px;
    background: var(--surface-secondary);
    border-top: 1px solid var(--border-color);
    font-size: 13px;
    color: var(--text-muted);
  }

  .segments-count {
    font-weight: 500;
  }

  .loading-indicator {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .cancel-button {
    background: #6b7280;
    color: white;
    border: 1px solid #6b7280;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-weight: 500;
    transition: all 0.2s ease;
    font-size: 0.95rem;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .cancel-button:hover {
    background: #4b5563;
    border-color: #4b5563;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(75, 85, 99, 0.25);
  }

  .cancel-button:active {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(75, 85, 99, 0.2);
  }

  .transcript-display-container {
    position: relative;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }

  .transcript-display {
    max-height: 600px;
    overflow-y: auto;
    overflow-x: hidden;
    position: relative;
  }

  .transcript-segment {
    border-bottom: 1px solid var(--border-light);
    /* Browser-native virtualization: skip rendering/layout/paint of off-screen rows.
       Unlike JS windowing, every segment stays in the DOM, so infinite-scroll, the
       scrollbar indicator, search-scroll-to, segment editing, and the highlight-flash
       all keep working unchanged. `auto` remembers each row's real rendered height,
       so the intrinsic-size estimate only affects never-yet-rendered far-offscreen rows.
       This is why $components/gallery/VirtualList.svelte is NOT reused here: it windows
       a fixed 44px row into a spacer-padded slice, which would evict off-screen segments
       from the DOM and break every one of those features (they all reach segments by
       `document.querySelector('[data-segment-id=…]')`). Variable-height rows — wrapped
       text, multi-segment overlap groups, the expanded edit textarea — rule it out too. */
    content-visibility: auto;
    contain-intrinsic-size: auto 56px;
  }

  .transcript-segment:last-child {
    border-bottom: none;
  }

  .segment-row {
    display: flex;
    align-items: stretch;
    min-width: 0; /* Allow flex container to shrink */
    width: 100%;
  }

  .segment-content {
    flex: 1 1 0; /* Allow shrinking below content size */
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr); /* minmax(0, 1fr) allows column to shrink */
    gap: 12px;
    align-items: start;
    padding: 8px 12px;
    background: none;
    border: none;
    text-align: left;
    cursor: pointer;
    transition: all 0.2s ease;
    border-radius: 4px;
    margin: 2px 4px;
    min-width: 0; /* Allow grid to shrink */
    max-width: 100%;
    overflow: hidden;
  }

  .segment-content.monologue {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .segment-speaker-wrapper {
    display: flex;
    align-items: center;
    min-width: fit-content;
  }

  .segment-content:hover {
    background: rgba(59, 130, 246, 0.08);
  }

  .segment-time {
    font-size: 12px;
    font-weight: 600;
    color: var(--primary-color);
    font-family: monospace;
    white-space: nowrap;
    min-width: fit-content;
  }

  .segment-speaker {
    font-size: 12px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 12px;
    white-space: nowrap;
    min-width: fit-content;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    border: 1px solid;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    transition: all 0.2s ease;
    color: var(--speaker-light);
  }

  /* Dark mode speaker colors */
  :global([data-theme='dark']) .segment-speaker {
    color: var(--speaker-dark);
  }

  .segment-text {
    font-size: 14px;
    color: var(--text-primary);
    line-height: 1.4;
    position: relative;
    padding-left: 12px;
    word-wrap: break-word;
    overflow-wrap: break-word;
    word-break: break-word;
    min-width: 0; /* Allow text to shrink in grid layout */
  }

  /* Content redaction — "blur" mask style. The backend emits
     <span class="redacted" data-cat="..."> around the original text; we blur it
     and reveal on hover (authorized viewers only see this style at all). */
  .segment-text :global(span.redacted) {
    filter: blur(5px);
    background: var(--surface-hover, rgba(127, 127, 127, 0.15));
    border-radius: 3px;
    cursor: help;
    transition: filter 0.12s ease;
    user-select: none;
  }
  .segment-text :global(span.redacted:hover) {
    filter: blur(0);
    user-select: text;
  }

  .segment-text::before {
    content: '';
    position: absolute;
    left: 0;
    top: 2px;
    width: 2px;
    height: 20px;
    background: rgba(107, 114, 128, 0.3);
    border-radius: 1px;
  }

  .low-confidence-dot {
    color: var(--warning-color, #f59e0b);
    font-size: 0.6rem;
    vertical-align: super;
    margin-left: 2px;
    cursor: help;
  }

  .edit-button {
    padding: 8px 12px;
    background: none;
    border: none;
    color: #3b82f6;
    cursor: pointer;
    font-size: 12px;
    font-weight: 400;
    transition: all 0.2s ease;
    text-decoration: underline;
    text-decoration-color: transparent;
  }

  .edit-button:hover {
    color: #1d4ed8;
    text-decoration-color: #1d4ed8;
  }

  .segment-edit-container {
    padding: 16px;
    background: var(--background-secondary);
    border-left: 3px solid var(--primary-color);
  }

  .segment-edit-input {
    margin-top: 8px;
  }

  .segment-textarea {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--surface-color);
    color: var(--text-primary);
    font-size: 14px;
    line-height: 1.4;
    resize: vertical;
  }

  .segment-edit-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    margin-top: 8px;
  }

  .segment-edit-actions .save-button,
  .segment-edit-actions .cancel-button {
    padding: 0.4rem 0.8rem;
    border-radius: 10px;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    border: none;
    min-width: 60px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .segment-edit-actions .save-button {
    background: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .segment-edit-actions .save-button:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .segment-edit-actions .save-button:active:not(:disabled) {
    transform: scale(1);
  }

  .segment-edit-actions .save-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .segment-edit-actions .cancel-button {
    background: #6b7280;
    color: white;
    border: 1px solid #6b7280;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .segment-edit-actions .cancel-button:hover {
    background: #4b5563;
    border-color: #4b5563;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(75, 85, 99, 0.25);
  }

  .segment-edit-actions .cancel-button:active {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(75, 85, 99, 0.2);
  }

  :global(.highlight-flash) {
    animation: highlightFlash 2s ease-out;
  }

  @keyframes highlightFlash {
    0% { background-color: rgba(59, 130, 246, 0.3); }
    100% { background-color: transparent; }
  }

  /* Overlap Group Styles */
  .overlap-group {
    position: relative;
    margin: 8px 0;
    padding-left: 12px;
    border-left: 3px solid var(--primary-color);
    background: linear-gradient(90deg, rgba(59, 130, 246, 0.05) 0%, transparent 100%);
    border-radius: 0 8px 8px 0;
  }

  .overlap-indicator {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    background: rgba(59, 130, 246, 0.1);
    border-radius: 0 6px 0 0;
    font-size: 12px;
    color: var(--primary-color);
    font-weight: 600;
  }

  .overlap-indicator svg {
    color: var(--primary-color);
    opacity: 0.8;
  }

  .overlap-label {
    flex: 1;
  }

  .overlap-time {
    font-family: monospace;
    font-size: 11px;
    color: var(--text-secondary);
    font-weight: 500;
  }

  .overlap-connector {
    position: absolute;
    left: 6px;
    top: 40px;
    bottom: 8px;
    width: 2px;
    background: #3b82f6;
    opacity: 0.3;
    border-radius: 1px;
  }

  .transcript-segment.in-overlap {
    margin-left: 4px;
    border-left: none;
  }

  .transcript-segment.in-overlap .segment-content {
    background: transparent;
  }

  .transcript-segment.in-overlap:hover .segment-content {
    background: rgba(59, 130, 246, 0.1);
  }

  .transcript-segment.first-in-overlap {
    margin-top: 4px;
  }

  .transcript-segment.last-in-overlap {
    margin-bottom: 4px;
  }

  /* Dark mode adjustments for overlap groups */
  :global([data-theme='dark']) .overlap-group {
    background: linear-gradient(90deg, rgba(59, 130, 246, 0.08) 0%, transparent 100%);
  }

  :global([data-theme='dark']) .overlap-indicator {
    background: rgba(59, 130, 246, 0.15);
  }

  @media (max-width: 768px) {
    /* Stack: [time | speaker] on row 1, [text spanning full width] on row 2 */
    .segment-content {
      grid-template-columns: auto 1fr;
      grid-template-rows: auto auto;
      gap: 4px 8px;
      padding: 8px;
    }

    .segment-content.monologue {
      grid-template-columns: auto 1fr;
    }

    /* Time stays in column 1 */
    .segment-time {
      font-size: 11px;
      grid-column: 1;
      grid-row: 1;
    }

    /* Speaker stays in column 2 */
    .segment-speaker-wrapper {
      grid-column: 2;
      grid-row: 1;
    }

    .segment-speaker {
      font-size: 10px;
      padding: 1px 6px;
      white-space: normal;
      max-width: none;
      text-align: center;
      line-height: 1.3;
    }

    /* Text spans full width on row 2 */
    .segment-text {
      grid-column: 1 / -1;
      grid-row: 2;
      padding-left: 0;
      font-size: 13px;
    }

    .segment-text::before {
      display: none;
    }
  }
</style>
