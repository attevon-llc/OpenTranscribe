<script lang="ts">
  import type { Speaker } from '$lib/types/speaker';
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { type TranscriptSegment } from '$lib/utils/scrollbarCalculations';
  import { type SearchMatch } from '$lib/utils/searchHighlight';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import axiosInstance from '$lib/axios';
  import { isRequestCancelled } from '$lib/axios';
  import { createDebouncedHandler } from '$lib/utils/debounce';
  import SearchBar from '$components/ui/SearchBar.svelte';

  export let transcriptSegments: TranscriptSegment[] = [];
  export let speakerList: Speaker[] = [];
  export let disabled: boolean = false;
  /** File UUID — enables the whole-transcript completeness probe (backend, file-scoped). */
  export let fileUuid: string = '';
  /** Pagination state from the page: are there unloaded segments, and is a load in flight? */
  export let hasMoreSegments: boolean = false;
  export let loadingMoreSegments: boolean = false;

  const dispatch = createEventDispatcher();

  let searchQuery = '';
  let currentMatch = 0; // 1-based index into loaded matches
  let totalMatches = 0; // literal matches within the LOADED window
  let searchMatches: SearchMatch[] = [];
  let searchBar: SearchBar;
  let isVisible = false;

  // Whole-transcript completeness (backend, file-scoped keyword search). OpenSearch
  // tokenization means this count is approximate, so it is used only as a "there are
  // more matches in unloaded segments" signal — never shown as the authoritative
  // literal count. Literal matching stays consistent everywhere.
  let backendTotal: number | null = null;
  let backendLoading = false;
  let reqSeq = 0;
  let backendController: AbortController | null = null;

  // Progressive navigation into unloaded segments.
  let pendingAdvance = false;
  let advanceLoads = 0; // bounds how many pages a single "next" will pull in
  const MAX_ADVANCE_LOADS = 8;
  let lastSegmentCount = 0;

  const backendDebounced = createDebouncedHandler(() => probeBackend(searchQuery), 300);

  // ---- Local (literal, in-loaded-window) search -----------------------------

  function computeMatches(query: string): SearchMatch[] {
    const normalizedQuery = query.toLowerCase().trim();
    if (!normalizedQuery || !transcriptSegments?.length) return [];

    const speakerMapping = new Map<string, string>();
    speakerList.forEach((speaker: Speaker) => {
      speakerMapping.set(speaker.name, translateSpeakerLabel(speaker.display_name || speaker.name));
    });

    const matches: SearchMatch[] = [];
    transcriptSegments.forEach((segment, segmentIndex) => {
      if (!segment || typeof segment.text !== 'string') return;

      const text = segment.text.toLowerCase();
      let textIndex = 0;
      while ((textIndex = text.indexOf(normalizedQuery, textIndex)) !== -1) {
        matches.push({ segmentIndex, start: textIndex, length: normalizedQuery.length, type: 'text' });
        textIndex += normalizedQuery.length;
      }

      const speakerLabel = segment.speaker_label || segment.speaker?.name || '';
      const displayName = speakerMapping.get(speakerLabel) || segment.speaker?.display_name || speakerLabel;
      if (speakerLabel.toLowerCase().includes(normalizedQuery)) {
        matches.push({ segmentIndex, start: 0, length: speakerLabel.length, type: 'speaker' });
      }
      if (displayName !== speakerLabel && displayName.toLowerCase().includes(normalizedQuery)) {
        matches.push({ segmentIndex, start: 0, length: displayName.length, type: 'speaker' });
      }
    });
    return matches;
  }

  function emitResults() {
    dispatch('searchResults', {
      matches: searchMatches,
      currentMatch,
      totalMatches,
      query: searchQuery.toLowerCase().trim(),
    });
  }

  function performLocalSearch(query: string) {
    searchMatches = computeMatches(query);
    totalMatches = searchMatches.length;
    lastSegmentCount = transcriptSegments.length;
    currentMatch = totalMatches > 0 ? 1 : 0;
    emitResults();
    if (totalMatches > 0) navigateToMatch(0);
  }

  // Recompute over a grown segment set WITHOUT resetting the user's position.
  function reindexLocal() {
    const prevTotal = totalMatches;
    searchMatches = computeMatches(searchQuery);
    totalMatches = searchMatches.length;
    lastSegmentCount = transcriptSegments.length;
    if (currentMatch > totalMatches) currentMatch = totalMatches;
    emitResults();
    return prevTotal;
  }

  function onSearch(query: string) {
    searchQuery = query;
    if (!query.trim()) {
      clearSearch();
      return;
    }
    performLocalSearch(query); // instant, literal
    backendLoading = true; // show progress until the probe resolves
    backendDebounced.trigger(); // whole-transcript completeness (debounced)
  }

  function clearSearch() {
    backendDebounced.cleanup();
    backendController?.abort();
    reqSeq++;
    searchMatches = [];
    totalMatches = 0;
    currentMatch = 0;
    backendTotal = null;
    backendLoading = false;
    pendingAdvance = false;
    advanceLoads = 0;
    emitResults();
  }

  // ---- Whole-transcript completeness probe (backend) ------------------------

  async function probeBackend(query: string) {
    if (!query.trim() || !fileUuid) {
      backendTotal = null;
      backendLoading = false;
      return;
    }
    backendController?.abort();
    const controller = new AbortController();
    backendController = controller;
    const seq = ++reqSeq;
    backendLoading = true;
    try {
      // Lightweight count endpoint (size=0, no snippets/embeddings) — fast + concurrent.
      const resp = await axiosInstance.get('/search/count', {
        params: { q: query, file_uuid: fileUuid },
        signal: controller.signal,
      });
      if (seq !== reqSeq) return; // stale — a newer query superseded this one
      backendTotal = typeof resp.data?.total === 'number' ? resp.data.total : 0;
    } catch (err) {
      if (isRequestCancelled(err)) return;
      // Degrade gracefully to loaded-window-only search.
      if (seq === reqSeq) backendTotal = null;
    } finally {
      if (seq === reqSeq) backendLoading = false;
    }
  }

  // ---- Navigation (with progressive load-more into unloaded segments) --------

  function moreMatchesMayExist(): boolean {
    // The count endpoint returns matching *chunks* across the whole file; when the
    // term is present anywhere and unloaded segments remain, more matches may lie
    // beyond the loaded window. Progressive loading stops once a page yields no new
    // matches (see the reactive block) or the transcript is fully loaded.
    return hasMoreSegments && (backendTotal ?? 0) > 0;
  }

  function requestMoreSegments() {
    if (!loadingMoreSegments && hasMoreSegments) dispatch('loadMore');
  }

  function navigateToNext() {
    if (totalMatches > 0 && currentMatch < totalMatches) {
      currentMatch += 1;
      navigateToMatch(currentMatch - 1);
      emitResults();
      return;
    }
    // At/after the last loaded match: pull in more of the transcript if matches remain.
    if (moreMatchesMayExist() && advanceLoads < MAX_ADVANCE_LOADS) {
      pendingAdvance = true;
      advanceLoads += 1;
      requestMoreSegments();
      return;
    }
    if (totalMatches > 0) {
      currentMatch = 1; // wrap
      navigateToMatch(0);
      emitResults();
    }
  }

  function navigateToPrevious() {
    if (totalMatches === 0) return;
    currentMatch = currentMatch > 1 ? currentMatch - 1 : totalMatches;
    navigateToMatch(currentMatch - 1);
    emitResults();
  }

  function navigateToMatch(matchIndex: number, autoSeek: boolean = false) {
    if (matchIndex < 0 || matchIndex >= searchMatches.length) return;
    const match = searchMatches[matchIndex];
    if (!match || match.segmentIndex < 0 || match.segmentIndex >= transcriptSegments.length) return;
    const segment = transcriptSegments[match.segmentIndex];
    if (!segment) return;

    dispatch('navigateToMatch', { match, segment, segmentIndex: match.segmentIndex, autoSeek });

    // Scroll to the segment (defer a frame so freshly loaded rows are in the DOM).
    requestAnimationFrame(() => {
      const el = document.querySelector(`[data-segment-id="${segment.uuid}"]`);
      if (!el) return;
      el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
      el.classList.add('search-current-match');
      // Matches the `search-pulse 1s` animation on `.search-current-match`
      // (src/styles/search.css) — was 2000ms, double the actual animation length.
      setTimeout(() => el.classList.remove('search-current-match'), 1000);
    });
  }

  function seekToCurrentMatch() {
    if (totalMatches === 0 || currentMatch < 1) return;
    const match = searchMatches[currentMatch - 1];
    const segment = transcriptSegments[match.segmentIndex];
    dispatch('navigateToMatch', { match, segment, segmentIndex: match.segmentIndex, autoSeek: true });
  }

  // Renaming a speaker changes what `computeMatches` produces without changing the
  // segment count, so a count-only trigger left `type: 'speaker'` matches pointing at the
  // old name — searching the name the user had just typed found nothing. Fold the current
  // display names into the trigger.
  $: speakerNameSignature = speakerList
    .map((speaker: Speaker) => `${speaker.name}=${speaker.display_name || ''}`)
    .join('|');
  let lastSpeakerNameSignature = '';

  // When the page appends newly loaded segments or a speaker is renamed, re-index and (if
  // the user pressed "next" past the loaded window) advance to the first newly revealed
  // match.
  $: if (
    isVisible &&
    searchQuery.trim() &&
    (transcriptSegments.length !== lastSegmentCount ||
      speakerNameSignature !== lastSpeakerNameSignature)
  ) {
    lastSpeakerNameSignature = speakerNameSignature;
    const prevTotal = reindexLocal();
    if (pendingAdvance) {
      if (totalMatches > prevTotal) {
        pendingAdvance = false;
        advanceLoads = 0;
        currentMatch = prevTotal + 1;
        navigateToMatch(currentMatch - 1);
        emitResults();
      } else if (moreMatchesMayExist() && advanceLoads < MAX_ADVANCE_LOADS) {
        advanceLoads += 1;
        requestMoreSegments(); // keep pulling pages until a new match appears
      } else {
        pendingAdvance = false;
        advanceLoads = 0;
      }
    }
  }

  // ---- Counter / status presentation ----------------------------------------

  $: moreAvailable = hasMoreSegments && (backendTotal ?? 0) > 0;
  $: counterText = (() => {
    if (totalMatches > 0) {
      const base = $t('transcriptSearch.matchCounter', { current: currentMatch, total: totalMatches });
      return moreAvailable ? `${base}+` : base;
    }
    if (searchQuery.trim() && !backendLoading) {
      // Nothing in the loaded window; matches may still live in unloaded segments.
      if (moreAvailable) return $t('transcriptSearch.moreInTranscript');
      return '';
    }
    return '';
  })();
  $: noResults =
    !!searchQuery.trim() && totalMatches === 0 && !backendLoading && !(backendTotal && backendTotal > 0);
  $: statusText = backendLoading ? $t('transcriptSearch.searchingAll') : '';

  // ---- Ctrl+F open / Escape close -------------------------------------------

  function handleGlobalKeydown(event: KeyboardEvent) {
    if ((event.ctrlKey || event.metaKey) && event.key === 'f') {
      event.preventDefault();
      openSearch();
    } else if (event.key === 'Escape' && isVisible) {
      // Close from anywhere while the find bar is open (focus may be on a nav button).
      closeSearch();
    }
  }

  function openSearch() {
    isVisible = true;
    setTimeout(() => searchBar?.focus(), 50);
  }

  function closeSearch() {
    isVisible = false;
    searchQuery = '';
    clearSearch();
  }

  function handleBarKeydown(event: CustomEvent<KeyboardEvent>) {
    if (event.detail.key === 'Escape' && isVisible) closeSearch();
  }

  onMount(() => {
    if (typeof window !== 'undefined') window.addEventListener('keydown', handleGlobalKeydown);
  });

  onDestroy(() => {
    if (typeof window !== 'undefined') window.removeEventListener('keydown', handleGlobalKeydown);
    backendDebounced.cleanup();
    backendController?.abort();
  });
</script>

{#if isVisible}
  <div class="transcript-search" class:disabled>
    <SearchBar
      bind:this={searchBar}
      bind:value={searchQuery}
      {disabled}
      size="sm"
      debounceMs={0}
      showNav
      navDisabled={totalMatches === 0}
      loading={backendLoading}
      {counterText}
      {statusText}
      {noResults}
      placeholder={$t('transcriptSearch.placeholder')}
      ariaLabel={$t('transcriptSearch.ariaLabel')}
      clearLabel={$t('transcriptSearch.closeSearchTitle')}
      nextLabel={$t('transcriptSearch.nextMatchTitle')}
      previousLabel={$t('transcriptSearch.previousMatchTitle')}
      on:search={({ detail }) => onSearch(detail.value)}
      on:clear={closeSearch}
      on:next={navigateToNext}
      on:previous={navigateToPrevious}
      on:keydown={handleBarKeydown}
    />
    <button
      class="jump-button"
      on:click={seekToCurrentMatch}
      disabled={totalMatches === 0}
      title={$t('transcriptSearch.jumpToMatchTitle')}
      aria-label={$t('transcriptSearch.jumpToMatchAriaLabel')}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polygon points="5 3 19 12 5 21 5 3"></polygon>
      </svg>
    </button>
    <button class="close-button" on:click={closeSearch} title={$t('transcriptSearch.closeSearchTitle')} aria-label={$t('transcriptSearch.closeSearchTitle')}>
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M18 6L6 18M6 6l12 12" />
      </svg>
    </button>
  </div>
{:else}
  <div class="search-trigger">
    <button class="search-trigger-button" on:click={openSearch} title={$t('transcriptSearch.searchButtonTitle')} {disabled}>
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="11" cy="11" r="8"></circle>
        <path d="M21 21l-4.35-4.35"></path>
      </svg>
      {$t('transcriptSearch.searchButtonLabel')}
    </button>
  </div>
{/if}

<style>
  .transcript-search {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    margin: 0 0 6px 0;
  }

  .transcript-search.disabled {
    opacity: 0.6;
    pointer-events: none;
  }

  .jump-button {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 32px;
    padding: 0 8px;
    background: var(--primary-color);
    border: 1px solid var(--primary-color);
    border-radius: 4px;
    color: white;
    cursor: pointer;
    transition:
      background 0.15s ease,
      opacity 0.15s ease;
    flex-shrink: 0;
  }

  .jump-button:hover:not(:disabled) {
    background: var(--primary-hover);
  }

  .jump-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .close-button {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    color: var(--text-secondary);
    cursor: pointer;
    transition:
      background 0.15s ease,
      color 0.15s ease;
    flex-shrink: 0;
  }

  .close-button:hover {
    background: var(--button-hover, var(--hover-color));
    color: var(--text-color);
  }

  .search-trigger {
    margin: 0 0 6px 0;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    display: flex;
    align-items: center;
    height: 40px;
    width: fit-content;
    min-width: 120px;
  }

  .search-trigger-button {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0.6rem 1rem;
    background: transparent;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 0.9rem;
    font-weight: 500;
    transition:
      background 0.15s ease,
      color 0.15s ease;
    border-radius: 6px;
    width: 100%;
    height: 100%;
  }

  .search-trigger-button:hover:not(:disabled) {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .search-trigger-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
