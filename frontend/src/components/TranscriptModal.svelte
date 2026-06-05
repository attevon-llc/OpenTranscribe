<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { getSpeakerColorForSegment } from '$lib/utils/speakerColors';
  import { processedTranscriptSegments, transcriptStore } from '../stores/transcriptStore';
  import { copyToClipboard } from '$lib/utils/clipboard';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import Spinner from './ui/Spinner.svelte';
  import BaseModal from './ui/BaseModal.svelte';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';

  export let fileId: number;
  export let fileName: string = '';
  export let isOpen: boolean = false;
  export let diarizationDisabled: boolean = false;

  // Pagination props
  export let totalSpeakerSegments: number = 0;
  export let hasMoreSegments: boolean = false;
  export let loadingMoreSegments: boolean = false;

  // Reference fileId to suppress warning (will be tree-shaken in production)
  $: { fileId; }

  const dispatch = createEventDispatcher<{
    close: void;
    loadMore: void;
    toggleRedaction: void;
  }>();

  // Content redaction toggle (owner/admin only) — state lives on the file
  // detail page; the modal just renders the same control for consistency.
  export let showRedactionToggle: boolean = false;
  export let showOriginal: boolean = false;
  export let redactionToggleBusy: boolean = false;

  let loading = false;
  let error: string | null = null;
  let consolidatedTranscript = '';

  // Infinite scroll sentinel element
  let infiniteScrollSentinel: HTMLElement | null = null;
  let infiniteScrollObserver: IntersectionObserver | null = null;

  // Scroll progress tracking (reading progress bar)
  let scrollProgress: number = 0;
  let transcriptContentElement: HTMLElement | null = null;

  // Calculate loaded segments info from raw (unmerged) segment count
  // loadedSegments not needed — footer uses displaySegments.length and totalSpeakerSegments

  // Search functionality
  let searchQuery = '';
  let currentMatchIndex = 0;
  let totalMatches = 0;
  let copyButtonText = $t('transcriptModal.copy');

  // Subscribe to the processed transcript segments from the store
  $: displaySegments = $processedTranscriptSegments;

  // Generate consolidated transcript when display segments change
  $: if (displaySegments && displaySegments.length > 0) {
    consolidatedTranscript = diarizationDisabled
      ? displaySegments.map(block => block.text).join(' ')
      : displaySegments
          .map(block => `${translateSpeakerLabel(block.speakerName)} [${formatSimpleTimestamp(block.startTime ?? 0)}-${formatSimpleTimestamp(block.endTime ?? 0)}]: ${block.text}`)
          .join('\n\n');
  } else {
    consolidatedTranscript = '';
  }

  $: if (searchQuery && displaySegments.length > 0) {
    // Count matches across all segment text for accurate search navigation
    const allText = displaySegments.map(segment => segment.text).join(' ');
    totalMatches = countMatches(searchQuery, allText);
    currentMatchIndex = 0;
  } else {
    totalMatches = 0;
    currentMatchIndex = 0;
  }

  function countMatches(query: string, text: string): number {
    if (!query.trim() || !text) return 0;

    const searchTerm = query.toLowerCase();
    const escapedTerm = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const matches = text.toLowerCase().match(new RegExp(escapedTerm, 'g'));
    return matches ? matches.length : 0;
  }

  function escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function highlightSearchTerms(text: string, query: string, matchIndex: number = -1): string {
    if (!query.trim() || !text) return escapeHtml(text);

    const escaped = escapeHtml(text);
    const searchTerm = query.toLowerCase();
    const escapedTerm = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`(${escapedTerm})`, 'gi');

    let currentMatch = 0;
    return escaped.replace(regex, (match) => {
      const isCurrentMatch = currentMatch === matchIndex;
      const matchClass = isCurrentMatch ? 'current-match' : 'search-match';
      const result = `<mark class="${matchClass}" data-match-index="${currentMatch}">${match}</mark>`;
      currentMatch++;
      return result;
    });
  }

  function cycleToNextMatch() {
    if (totalMatches > 0) {
      currentMatchIndex = (currentMatchIndex + 1) % totalMatches;
      scrollToCurrentMatch();
    }
  }

  function cycleToPreviousMatch() {
    if (totalMatches > 0) {
      currentMatchIndex = currentMatchIndex > 0 ? currentMatchIndex - 1 : totalMatches - 1;
      scrollToCurrentMatch();
    }
  }

  function scrollToCurrentMatch() {
    setTimeout(() => {
      const currentMatch = document.querySelector(`[data-match-index="${currentMatchIndex}"].current-match`);
      if (currentMatch) {
        currentMatch.scrollIntoView({
          behavior: 'smooth',
          block: 'center',
          inline: 'nearest'
        });
      }
    }, 50);
  }

  function handleSearchKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      searchQuery = '';
    } else if (event.key === 'Enter') {
      if (event.shiftKey) {
        cycleToPreviousMatch();
      } else {
        cycleToNextMatch();
      }
    }
  }

  function handleClose() {
    dispatch('close');
  }

  function handleCopy() {
    if (!consolidatedTranscript) {
      copyButtonText = $t('transcriptModal.noContent');
      setTimeout(() => {
        copyButtonText = $t('transcriptModal.copy');
      }, 2000);
      return;
    }

    copyToClipboard(
      consolidatedTranscript,
      () => {
        copyButtonText = $t('transcriptModal.copied');
        setTimeout(() => {
          copyButtonText = $t('transcriptModal.copy');
        }, 2000);
      },
      (error) => {
        copyButtonText = $t('transcriptModal.copyFailed');
        setTimeout(() => {
          copyButtonText = $t('transcriptModal.copy');
        }, 2000);
      }
    );
  }


  function clearSearch() {
    searchQuery = '';
  }


  function formatSimpleTimestamp(seconds: number): string {
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = Math.floor(seconds % 60);
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`;
  }

  // Handle scroll to update reading progress.
  // Uses the first visible block index against the backend-precomputed total so
  // progress stays stable as more segments load via infinite scroll.
  function handleTranscriptScroll(event: Event) {
    const target = event.target as HTMLElement;
    if (!target) return;

    const total = totalSpeakerSegments || displaySegments.length;
    if (total === 0) return;

    const blockElements = target.querySelectorAll('[data-seg-index]');
    if (blockElements.length === 0) { scrollProgress = 0; return; }

    // Only snap to 100% when truly at the end (no more segments to load)
    const atBottom = Math.abs(target.scrollHeight - target.clientHeight - target.scrollTop) < 2;
    if (atBottom && !hasMoreSegments) { scrollProgress = 100; return; }

    const viewportTop = target.scrollTop;
    let firstVisibleIdx = 0;
    for (let i = 0; i < blockElements.length; i++) {
      const el = blockElements[i] as HTMLElement;
      if (el.offsetTop - target.offsetTop >= viewportTop) { firstVisibleIdx = i; break; }
    }

    const newProgress = Math.min(99, Math.round((firstVisibleIdx / total) * 100));
    // Never decrease progress while loading more segments (prevents glitch on infinite scroll)
    if (loadingMoreSegments && newProgress < scrollProgress) return;
    scrollProgress = newProgress;
  }

  // Set up infinite scroll observer
  function setupInfiniteScrollObserver() {
    if (typeof IntersectionObserver !== 'undefined' && !infiniteScrollObserver) {
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
  }

  // Observe the sentinel element when it's available
  $: if (infiniteScrollSentinel && infiniteScrollObserver) {
    infiniteScrollObserver.observe(infiniteScrollSentinel);
  }

  // Reset scroll progress when modal opens
  $: if (isOpen) {
    scrollProgress = 0;
    setupInfiniteScrollObserver();
  }

  onDestroy(() => {
    if (infiniteScrollObserver) {
      infiniteScrollObserver.disconnect();
      infiniteScrollObserver = null;
    }
  });
</script>

<div class="transcript-modal-wrapper">
<BaseModal {isOpen} maxWidth="1200px" onClose={handleClose}>
      <svelte:fragment slot="header">
        <h2 class="modal-title">{$t('transcriptModal.title', { fileName })}</h2>
        {#if showRedactionToggle}
          <!-- Left of the header actions — keep it away from the close X -->
          <button
            type="button"
            class="redaction-link-btn"
            on:click={() => dispatch('toggleRedaction')}
            disabled={redactionToggleBusy}
            title={showOriginal
              ? $t('settings.contentRedaction.showRedactedTooltip')
              : $t('settings.contentRedaction.showOriginalTooltip')}
          >
            {#if redactionToggleBusy}
              <Spinner size="small" />
            {/if}
            {showOriginal
              ? $t('settings.contentRedaction.showRedacted')
              : $t('settings.contentRedaction.showOriginal')}
          </button>
        {/if}
        <div class="header-actions">
          {#if consolidatedTranscript}
            <button
              class="copy-button-header"
              class:copied={copyButtonText === $t('transcriptModal.copied')}
              on:click={handleCopy}
              aria-label={$t('transcriptModal.searchTranscript')}
              title={copyButtonText === $t('transcriptModal.copied') ? $t('transcriptModal.transcriptCopied') : $t('transcriptModal.copyTranscript')}
            >
              {#if copyButtonText === $t('transcriptModal.copied')}
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M10.97 4.97a.75.75 0 0 1 1.07 1.05l-3.99 4.99a.75.75 0 0 1-1.08.02L4.324 8.384a.75.75 0 1 1 1.06-1.06l2.094 2.093 3.473-4.425a.267.267 0 0 1 .02-.022z"/>
                </svg>
                {$t('transcriptModal.copied')}
              {:else}
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M4 1.5H3a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V3.5a2 2 0 0 0-2-2h-1v1h1a1 1 0 0 1 1 1V14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1h1v-1z"/>
                  <path d="M9.5 1a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5h3zm-3-1A1.5 1.5 0 0 0 5 1.5v1A1.5 1.5 0 0 0 6.5 4h3A1.5 1.5 0 0 0 11 2.5v-1A1.5 1.5 0 0 0 9.5 0h-3z"/>
                </svg>
                {$t('transcriptModal.copy')}
              {/if}
            </button>
          {/if}
        </div>
      </svelte:fragment>

      <!-- Search Section -->
      {#if displaySegments.length > 0}
        <div class="search-section">
          <div class="search-container">
            <div class="search-input-wrapper">
              <svg class="search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="11" cy="11" r="8"></circle>
                <path d="m21 21-4.35-4.35"></path>
              </svg>
              <input
                type="text"
                bind:value={searchQuery}
                placeholder={$t('transcriptModal.searchPlaceholder')}
                class="search-input"
                on:keydown={handleSearchKeydown}
                aria-label={$t('transcriptModal.searchTranscript')}
              />
              {#if searchQuery}
                <button
                  class="clear-search-button"
                  on:click={clearSearch}
                  aria-label={$t('transcriptModal.clearSearch')}
                  title={$t('transcriptModal.clearSearch')}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="18" y1="6" x2="6" y2="18"></line>
                    <line x1="6" y1="6" x2="18" y2="18"></line>
                  </svg>
                </button>
              {/if}
            </div>

            {#if totalMatches > 0}
              <div class="search-results">
                <span class="match-count">{$t('transcriptModal.matchCount', { current: currentMatchIndex + 1, total: totalMatches })}</span>
                <div class="navigation-buttons">
                  <button
                    class="nav-button"
                    on:click={cycleToPreviousMatch}
                    disabled={totalMatches === 0}
                    aria-label={$t('transcriptModal.previousMatch')}
                    title={$t('transcriptModal.previousMatchShortcut')}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <polyline points="15,18 9,12 15,6"></polyline>
                    </svg>
                  </button>
                  <button
                    class="nav-button"
                    on:click={cycleToNextMatch}
                    disabled={totalMatches === 0}
                    aria-label={$t('transcriptModal.nextMatch')}
                    title={$t('transcriptModal.nextMatchShortcut')}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <polyline points="9,18 15,12 9,6"></polyline>
                    </svg>
                  </button>
                </div>
              </div>
            {/if}
          </div>
        </div>
      {/if}

      <!-- Transcript Content -->
      <div class="modal-content-wrapper">
        <!-- Reading progress bar at top -->
        {#if displaySegments.length > 0}
          <div class="reading-progress-bar">
            <div
              class="reading-progress-fill"
              style="width: {scrollProgress}%"
            ></div>
          </div>
        {/if}

        <div
          class="modal-content"
          bind:this={transcriptContentElement}
          on:scroll={handleTranscriptScroll}
        >
          {#if loading}
            <div class="loading-container">
              <Spinner size="large" />
              <p>{$t('transcriptModal.loading')}</p>
            </div>
          {:else if error}
            <div class="error-container">
              <div class="error-icon">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>
                  <path d="M12 9v4"/>
                  <path d="m12 17 .01 0"/>
                </svg>
              </div>
              <div class="error-message">
                <h3>{$t('transcriptModal.errorTitle')}</h3>
                <p>{error}</p>
              </div>
            </div>
          {:else if displaySegments.length > 0}
            <div class="transcript-content">
              {#each displaySegments as segment, index}
                {#if segment.isOverlapGroup && segment.overlapSegments}
                  {#if diarizationDisabled}
                    {#each segment.overlapSegments as overlapSeg}
                      <div class="transcript-segment monologue" data-seg-index={segment.rawStartIndex}>
                        <div class="segment-text">{@html sanitizeHighlightHtml(highlightSearchTerms(overlapSeg.text, searchQuery, currentMatchIndex))}</div>
                      </div>
                    {/each}
                  {:else}
                    <!-- Overlap Group -->
                    <div class="overlap-group" data-seg-index={segment.rawStartIndex}>
                      <div class="overlap-indicator">
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
                          <circle cx="9" cy="7" r="4"></circle>
                          <path d="M23 21v-2a4 4 0 0 0-3-3.87"></path>
                          <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
                        </svg>
                        <span class="overlap-label">{$t('transcript.overlapIndicator', { count: segment.overlapSegments.length })}</span>
                        <span class="overlap-time">{formatSimpleTimestamp(segment.startTime ?? 0)} - {formatSimpleTimestamp(segment.endTime ?? 0)}</span>
                      </div>
                      <div class="overlap-connector"></div>
                      {#each segment.overlapSegments as overlapSeg}
                        <div class="transcript-segment in-overlap">
                          <div class="segment-header">
                            <div
                              class="segment-speaker"
                              style="background-color: {getSpeakerColorForSegment(overlapSeg).bg}; border-color: {getSpeakerColorForSegment(overlapSeg).border}; --speaker-light: {getSpeakerColorForSegment(overlapSeg).textLight}; --speaker-dark: {getSpeakerColorForSegment(overlapSeg).textDark};"
                            >{translateSpeakerLabel(overlapSeg.speakerName)}</div>
                            <div class="segment-time">{formatSimpleTimestamp(overlapSeg.startTime ?? 0)}-{formatSimpleTimestamp(overlapSeg.endTime ?? 0)}</div>
                          </div>
                          <div class="segment-text">{@html sanitizeHighlightHtml(highlightSearchTerms(overlapSeg.text, searchQuery, currentMatchIndex))}</div>
                        </div>
                      {/each}
                    </div>
                  {/if}
                {:else}
                  <!-- Regular Segment -->
                  <div class="transcript-segment" class:monologue={diarizationDisabled} data-seg-index={segment.rawStartIndex}>
                    {#if !diarizationDisabled}
                      <div class="segment-header">
                        <div
                          class="segment-speaker"
                          style="background-color: {getSpeakerColorForSegment(segment).bg}; border-color: {getSpeakerColorForSegment(segment).border}; --speaker-light: {getSpeakerColorForSegment(segment).textLight}; --speaker-dark: {getSpeakerColorForSegment(segment).textDark};"
                        >{translateSpeakerLabel(segment.speakerName)}</div>
                        <div class="segment-time">{formatSimpleTimestamp(segment.startTime ?? 0)}-{formatSimpleTimestamp(segment.endTime ?? 0)}</div>
                      </div>
                    {/if}
                    <div class="segment-text">{@html sanitizeHighlightHtml(highlightSearchTerms(segment.text, searchQuery, currentMatchIndex))}</div>
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
                      <span>{$t('transcriptModal.loadingMore')}</span>
                    </div>
                  {/if}
                </div>
              {/if}
            </div>
          {:else}
            <div class="no-transcript">
              <div class="no-transcript-icon">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                  <polyline points="14,2 14,8 20,8"></polyline>
                  <line x1="16" y1="13" x2="8" y2="13"></line>
                  <line x1="16" y1="17" x2="8" y2="17"></line>
                  <polyline points="10,9 9,9 8,9"></polyline>
                </svg>
              </div>
              <h3>{$t('transcriptModal.noTranscriptTitle')}</h3>
              <p>{$t('transcriptModal.noTranscriptMessage')}</p>
            </div>
          {/if}
        </div>

        <!-- Segments loaded info -->
        {#if displaySegments.length > 0}
          <div class="segments-loaded-info">
            <span class="segments-count">{$t('transcript.speakerSegmentsOfTotal', { loaded: displaySegments.length, total: totalSpeakerSegments || displaySegments.length })}</span>
            {#if loadingMoreSegments}
              <span class="segments-detail">
                <Spinner size="small" />
              </span>
            {/if}
          </div>
        {/if}
      </div>
</BaseModal>
</div>

<style>
  .modal-content-wrapper {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    position: relative;
  }

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
    flex-shrink: 0;
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
    color: var(--text-secondary);
    font-size: 14px;
  }

  /* Segments loaded info bar */
  .segments-loaded-info {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 8px 16px;
    background: var(--bg-secondary);
    border-top: 1px solid var(--border-color);
    font-size: 13px;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .segments-count {
    font-weight: 500;
  }

  .segments-detail {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--text-muted);
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .copy-button-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
    font-size: 0.85rem;
  }

  .copy-button-header:hover {
    background-color: var(--hover-bg);
    color: var(--primary-color);
    border-color: var(--primary-color);
  }

  .copy-button-header.copied {
    background-color: var(--success-bg);
    border-color: var(--success-color);
    color: var(--success-color);
  }

  .copy-button-header.copied:hover {
    background-color: var(--success-bg);
    border-color: var(--success-color);
    color: var(--success-color);
  }

  /* Matches .redaction-link-btn on the file detail page for UI consistency */
  .redaction-link-btn {
    background: none;
    border: none;
    padding: 0.15rem 0.35rem;
    border-radius: 4px;
    font-size: 0.78rem;
    font-weight: 500;
    color: var(--primary-color);
    cursor: pointer;
    white-space: nowrap;
    transition:
      color 0.12s ease,
      background-color 0.12s ease;
  }
  .redaction-link-btn:hover {
    color: var(--primary-hover);
    background-color: rgba(var(--primary-color-rgb), 0.1);
  }
  .redaction-link-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
  .redaction-link-btn:disabled {
    opacity: 0.6;
    cursor: wait;
  }

  .modal-title {
    font-size: 1.5rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
    margin-right: 1.5rem;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Override BaseModal body padding for full-bleed transcript layout */
  .transcript-modal-wrapper :global(.modal-body) {
    padding: 0 !important;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .search-section {
    padding: 0.75rem 1.5rem;
    border-bottom: 1px solid var(--border-color);
    background-color: var(--bg-secondary);
    flex-shrink: 0;
  }

  .search-container {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .search-input-wrapper {
    flex: 1;
    position: relative;
    display: flex;
    align-items: center;
  }

  .search-icon {
    position: absolute;
    left: 0.75rem;
    color: var(--text-secondary);
    pointer-events: none;
    z-index: 1;
  }

  .search-input {
    width: 100%;
    padding: 0.5rem 0.75rem 0.5rem 2.5rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    font-size: 0.9rem;
    transition: border-color 0.2s ease;
  }

  .search-input:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
  }

  .search-input::placeholder {
    color: var(--text-secondary);
  }

  .clear-search-button {
    position: absolute;
    right: 0.5rem;
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary);
    padding: 0.25rem;
    border-radius: 4px;
    transition: color 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .clear-search-button:hover {
    color: var(--text-primary);
    background-color: var(--hover-bg);
  }

  .search-results {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-shrink: 0;
  }

  .match-count {
    font-size: 0.85rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .navigation-buttons {
    display: flex;
    gap: 0.25rem;
  }

  .nav-button {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 0.25rem;
    cursor: pointer;
    color: var(--text-secondary);
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 28px;
    min-height: 28px;
  }

  .nav-button:hover:not(:disabled) {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    color: var(--text-primary);
  }

  .nav-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .modal-content {
    flex: 1;
    overflow: auto;
    padding: 1.5rem;
  }

  .transcript-content {
    line-height: 1.6;
    color: var(--text-primary);
    font-size: 0.9rem;
    word-wrap: break-word;
  }

  .transcript-segment {
    margin-bottom: 1.5rem;
    display: flex;
    gap: 1rem;
    align-items: flex-start;
  }

  .transcript-segment:last-child {
    margin-bottom: 0;
  }

  .transcript-segment.monologue {
    margin-bottom: 0.25rem;
    display: block;
  }

  .segment-header {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    flex-shrink: 0;
    min-width: fit-content;
    align-items: center;
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

  .segment-time {
    font-size: 12px;
    font-weight: 600;
    color: var(--primary-color);
    font-family: monospace;
    white-space: nowrap;
  }

  .segment-text {
    font-size: 14px;
    color: var(--text-primary);
    line-height: 1.4;
    flex: 1;
    padding-top: 2px; /* Align with speaker chip top */
  }

  .loading-container, .error-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 3rem;
    gap: 1rem;
  }

  .error-container {
    flex-direction: row;
    text-align: left;
  }

  .error-icon {
    font-size: 2rem;
    flex-shrink: 0;
    color: var(--error-color);
  }

  .error-message h3 {
    margin: 0 0 0.5rem 0;
    color: var(--error-color);
  }

  .error-message p {
    margin: 0;
    color: var(--text-secondary);
  }

  .no-transcript {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 3rem;
    text-align: center;
    gap: 1rem;
  }

  .no-transcript-icon {
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .no-transcript h3 {
    margin: 0;
    color: var(--text-primary);
    font-size: 1.25rem;
    font-weight: 600;
  }

  .no-transcript p {
    margin: 0;
    color: var(--text-secondary);
  }

  /* Search highlighting */
  :global(.search-match) {
    background-color: rgba(255, 255, 0, 0.3);
    padding: 0.1em 0.2em;
    border-radius: 3px;
  }

  :global(.current-match) {
    background-color: rgba(255, 165, 0, 0.6);
    padding: 0.1em 0.2em;
    border-radius: 3px;
    box-shadow: 0 0 0 1px rgba(255, 165, 0, 0.8);
  }

  /* Overlap group styles */
  .overlap-group {
    position: relative;
    margin: 1rem 0;
    padding: 0.75rem;
    padding-left: 1.25rem;
    border-left: 3px solid var(--primary-color, #6366f1);
    background: linear-gradient(
      90deg,
      rgba(99, 102, 241, 0.08) 0%,
      transparent 100%
    );
    border-radius: 0 8px 8px 0;
  }

  .overlap-indicator {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
    padding: 0.375rem 0.75rem;
    background: rgba(99, 102, 241, 0.15);
    border-radius: 6px;
    font-size: 0.8rem;
    color: var(--primary-color, #6366f1);
    font-weight: 500;
  }

  .overlap-indicator svg {
    width: 14px;
    height: 14px;
  }

  .overlap-label {
    font-weight: 500;
  }

  .overlap-time {
    font-size: 0.75rem;
    opacity: 0.8;
  }

  .overlap-connector {
    position: absolute;
    left: 0;
    top: 3.5rem;
    bottom: 0.5rem;
    width: 2px;
    background: linear-gradient(
      to bottom,
      var(--primary-color, #6366f1) 0%,
      transparent 100%
    );
  }

  .in-overlap {
    position: relative;
    margin-left: 0.5rem;
    padding-left: 0.75rem;
    border-left: 2px solid var(--border-color, #e5e7eb);
  }

  .in-overlap:hover {
    border-left-color: var(--primary-color, #6366f1);
  }

  @media (max-width: 768px) {
    .modal-title {
      font-size: 1.1rem;
    }

    .search-section {
      padding: 0.75rem 1rem;
    }

    .search-container {
      flex-direction: column;
      align-items: stretch;
      gap: 0.75rem;
    }

    .search-results {
      justify-content: space-between;
    }

    .search-input {
      min-height: 44px;
    }

    .nav-button {
      min-width: 44px;
      min-height: 44px;
    }

    .copy-button-header {
      min-height: 44px;
      padding: 0.5rem 0.625rem;
    }

    .modal-content {
      padding: 1rem;
    }

    .transcript-content {
      font-size: 0.85rem;
    }

    .transcript-segment:not(.monologue) {
      flex-direction: column;
      gap: 0.5rem;
    }

    .segment-header {
      flex-direction: row;
      gap: 0.5rem;
    }
  }
</style>
