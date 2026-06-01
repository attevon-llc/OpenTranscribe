<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import ScrollbarIndicator from '$components/ScrollbarIndicator.svelte';
  import SegmentSpeakerDropdown from '$components/SegmentSpeakerDropdown.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { type TranscriptSegment } from '$lib/utils/scrollbarCalculations';
  import { highlightTextWithMatches, type SearchMatch } from '$lib/utils/searchHighlight';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';

  export let file: any = null;
  export let groupedTranscriptSegments: any[] = [];
  export let transcriptSegments: TranscriptSegment[] = [];
  export let speakerList: any[] = [];
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

  // Handle scroll to update progress bar based on segment index (not scroll position)
  // This ensures progress is relative to total transcript size, not just loaded segments
  function handleTranscriptScroll(event: Event) {
    const target = event.target as HTMLElement;
    if (!target || totalSegments === 0) return;

    // Find all segment elements with data-seg-index attribute
    const segmentElements = target.querySelectorAll('[data-seg-index]');
    if (segmentElements.length === 0) {
      scrollProgress = 0;
      return;
    }

    // Find the first visible segment (at top of viewport)
    const viewportTop = target.scrollTop;
    let firstVisibleSegmentIndex = 0;

    for (const el of Array.from(segmentElements)) {
      const segmentTop = (el as HTMLElement).offsetTop - target.offsetTop;
      if (segmentTop >= viewportTop) {
        const dataIndex = el.getAttribute('data-seg-index');
        firstVisibleSegmentIndex = dataIndex ? parseInt(dataIndex, 10) : 0;
        break;
      }
    }

    // Calculate progress as percentage of total segments
    scrollProgress = Math.round((firstVisibleSegmentIndex / totalSegments) * 100);
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
  });

  // Observe the sentinel element when it's available
  $: if (infiniteScrollSentinel && infiniteScrollObserver) {
    infiniteScrollObserver.observe(infiniteScrollSentinel);
  }

  // Helper to get original segment index for search highlighting
  function getOriginalSegmentIndex(segment: any): number {
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

  function editSegment(segment: any) {
    dispatch('editSegment', { segment });
  }

  function saveSegment(segment: any) {
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
    on:scroll={handleTranscriptScroll}
  >
  {#each groupedTranscriptSegments as group}
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
        {#each group.segments as segment, segIdx}
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
