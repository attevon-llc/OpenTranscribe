<script lang="ts">
  /**
   * The consolidated "view transcript" modal (issue #755).
   *
   * Replaces BOTH `components/TranscriptModal.svelte` (the file-detail "View transcript"
   * button) and `components/search/SearchTranscriptModal.svelte` (the search-result
   * transcript browser). Renders through the SAME components the live file-detail transcript
   * uses — `TranscriptSegmentList` (in read-only `editable=false` mode) — so there is one
   * segment renderer, one highlighter, one redaction style, one grouping resolver, instead of
   * three. See `frontend/src/lib/search/CLAUDE.md` for why the two *matchers* underneath
   * (`$lib/utils/searchHighlight` for the live query-driven find;
   * `$lib/transcript/matchClassification` for server-ranked occurrence classification) stay
   * separate modules — they answer genuinely different questions.
   *
   * Two modes:
   *  - `mode="file"` (default): the file-detail page already has the transcript loaded and
   *    owns pagination — this component just renders what it is given and dispatches
   *    `loadMore`/`toggleRedaction` back up.
   *  - `mode="search"`: opened from a search result. The caller has no transcript loaded at
   *    all, so this component self-fetches a window of segments (issue #755 J3 — a
   *    self-contained fetch, not a second page-owned pager) and classifies them against the
   *    `occurrences` prop via `matchClassification`.
   */
  import { supportSessionGate } from '$stores/supportSession';
  import { createEventDispatcher, tick } from 'svelte';
  import type { Speaker } from '$lib/types/speaker';
  import type { GroupedTranscriptSegment } from '$lib/types/media';
  import type { TranscriptSegment, TranscriptSourceFile } from '$lib/types/media';
  import type { SearchOccurrence } from '$stores/search';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import TranscriptSegmentList from './TranscriptSegmentList.svelte';
  import { resolveGroupedSegments } from '$lib/transcript/resolveGroupedSegments';
  import { classifySegments, countByType } from '$lib/transcript/matchClassification';
  import { appendSegmentPage, type SegmentPage, type SegmentBearingFile } from '$lib/fileDetail/segmentSync';
  import axiosInstance from '$lib/axios';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  export let isOpen = false;
  export let mode: 'file' | 'search' = 'file';
  export let fileName = '';
  export let diarizationDisabled = false;
  export let speakerList: Speaker[] = [];

  // --- mode="file" props (data owned by the file-detail page) -----------------------------
  // Only `file` is required — grouping is resolved here via the shared
  // `resolveGroupedSegments` helper (the same one `TranscriptDisplay` uses), so the page
  // does not need to compute or pass it twice.
  export let file: TranscriptSourceFile | null = null;
  export let totalSegments = 0;
  export let hasMoreSegments = false;
  export let loadingMoreSegments = false;
  export let showRedactionToggle = false;
  export let showOriginal = false;
  export let redactionToggleBusy = false;
  export let copyStatus: 'idle' | 'copying' | 'copied' | 'failed' | 'empty' = 'idle';

  // --- mode="search" props (self-fetching — issue #755 J3) --------------------------------
  export let fileUuid = '';
  export let searchQuery = '';
  export let occurrences: SearchOccurrence[] = [];

  const dispatch = createEventDispatcher<{
    close: void;
    loadMore: void;
    toggleRedaction: void;
    copyTranscript: void;
  }>();

  const SEARCH_PAGE_SIZE = 200;

  let searchSegments: TranscriptSegment[] = [];
  let searchGroupedRaw: GroupedTranscriptSegment[] = [];
  let searchTotalSegments = 0;
  let searchLoading = false;
  let searchLoadingMore = false;
  let searchError: string | null = null;
  let searchMyPermission: string | null | undefined = undefined;
  let searchShowOriginal = false;
  let searchRedactionBusy = false;
  let fetchedForUuid: string | null = null;
  let modalContentEl: HTMLElement | null = null;
  let currentOccurrenceIdx = 0;

  $: searchHasMore = searchTotalSegments > searchSegments.length;

  function syncSearchFetch(open: boolean, viewMode: string, uuid: string) {
    if (viewMode !== 'search') return;
    if (open && uuid && fetchedForUuid !== uuid) {
      fetchedForUuid = uuid;
      currentOccurrenceIdx = 0;
      void initSearchFetch();
    } else if (!open && fetchedForUuid !== null) {
      fetchedForUuid = null;
      searchSegments = [];
      searchGroupedRaw = [];
      searchTotalSegments = 0;
      searchShowOriginal = false;
      searchError = null;
    }
  }

  $: syncSearchFetch(isOpen, mode, fileUuid);

  async function initSearchFetch() {
    searchLoading = true;
    searchError = null;
    try {
      const { data } = await axiosInstance.get(`/files/${fileUuid}`, {
        params: { segment_limit: SEARCH_PAGE_SIZE, segment_offset: 0 },
      });
      searchSegments = data.transcript_segments || [];
      searchGroupedRaw = data.grouped_segments || [];
      searchTotalSegments = data.total_segments || 0;
      searchMyPermission = data.my_permission ?? null;
    } catch (e: unknown) {
      searchError = getErrorMessage(e, $t('searchTranscript.error'));
    } finally {
      searchLoading = false;
    }
  }

  async function loadMoreSearchSegments(): Promise<void> {
    if (searchLoadingMore || !searchHasMore) return;
    searchLoadingMore = true;
    try {
      const page: SegmentPage = (
        await axiosInstance.get(`/files/${fileUuid}/segments`, {
          params: {
            segment_limit: SEARCH_PAGE_SIZE,
            segment_offset: searchSegments.length,
            ...(searchShowOriginal ? { redact: false } : {}),
          },
        })
      ).data;
      // `TranscriptSegment` (this component's own segment shape, from
      // `$lib/utils/scrollbarCalculations`) and `SegmentBearingFile`'s `SegmentLike`
      // (`$lib/fileDetail/segmentSync`) describe the same wire objects with two different,
      // independently-typed interfaces — a cast at this one boundary, not a loosening of
      // either shared type.
      const merged = appendSegmentPage(
        {
          transcript_segments: searchSegments,
          grouped_segments: searchGroupedRaw,
        } as unknown as SegmentBearingFile,
        page
      );
      searchSegments = (merged.transcript_segments || []) as unknown as TranscriptSegment[];
      searchGroupedRaw = (merged.grouped_segments || []) as GroupedTranscriptSegment[];
    } catch (e: unknown) {
      toastStore.error(getErrorMessage(e, $t('fileDetail.failedToLoadMoreSegments')));
    } finally {
      searchLoadingMore = false;
    }
  }

  async function toggleSearchOriginal(): Promise<void> {
    if (searchRedactionBusy) return;
    searchRedactionBusy = true;
    const next = !searchShowOriginal;
    try {
      const { data } = await axiosInstance.get(`/files/${fileUuid}`, {
        params: {
          segment_limit: Math.max(searchSegments.length, SEARCH_PAGE_SIZE),
          segment_offset: 0,
          ...(next ? { redact: false } : {}),
        },
      });
      searchShowOriginal = next;
      searchSegments = data.transcript_segments || [];
      searchGroupedRaw = data.grouped_segments || [];
    } catch (e: unknown) {
      toastStore.error(getErrorMessage(e, $t('transcript.toggleRedactionFailed')));
    } finally {
      searchRedactionBusy = false;
    }
  }

  function handleLoadMore() {
    if (mode === 'file') {
      dispatch('loadMore');
    } else {
      void loadMoreSearchSegments();
    }
  }

  function handleToggleRedaction() {
    if (mode === 'file') {
      dispatch('toggleRedaction');
    } else {
      void toggleSearchOriginal();
    }
  }

  function handleClose() {
    dispatch('close');
  }

  // --- Effective props fed to TranscriptSegmentList, regardless of mode -------------------
  $: fileTranscriptSegments = (file?.transcript_segments || []) as TranscriptSegment[];
  $: activeTranscriptSegments = mode === 'search' ? searchSegments : fileTranscriptSegments;
  $: activeGroupedSegments =
    mode === 'search'
      ? resolveGroupedSegments(searchSegments, searchGroupedRaw)
      : resolveGroupedSegments(fileTranscriptSegments, file?.grouped_segments);
  $: activeFile = mode === 'search' ? { uuid: fileUuid } : file;
  $: activeTotalSegments = mode === 'search' ? searchTotalSegments : totalSegments;
  $: activeHasMore = mode === 'search' ? searchHasMore : hasMoreSegments;
  $: activeLoadingMore = mode === 'search' ? searchLoadingMore : loadingMoreSegments;
  $: activeShowOriginal = mode === 'search' ? searchShowOriginal : showOriginal;
  $: activeShowRedactionToggle =
    mode === 'search'
      ? searchMyPermission === null || searchMyPermission === 'owner'
      : showRedactionToggle;
  $: activeRedactionBusy = mode === 'search' ? searchRedactionBusy : redactionToggleBusy;

  // Issue #755 D1-D4: keyword/semantic classification, only in search mode.
  $: segmentClassification =
    mode === 'search' ? classifySegments(activeTranscriptSegments, occurrences) : {};
  // §4.3 / J11 — the occurrence set the backend hands us is already capped
  // (`SEARCH_MAX_SNIPPETS_PER_FILE`), so a raw count is a claim the data can't support.
  // Render it honestly with the same "+" suffix convention TranscriptSearch already uses
  // for its own unloaded-window case, rather than a confidently wrong total.
  $: classifiedCounts = mode === 'search' ? countByType(occurrences) : null;
  $: sortedOccurrences =
    mode === 'search' ? [...occurrences].sort((a, b) => a.start_time - b.start_time) : [];
  $: totalOccurrences = sortedOccurrences.length;

  async function scrollToOccurrence(idx: number) {
    const occ = sortedOccurrences[idx];
    if (!occ) return;
    const findSegment = () =>
      activeTranscriptSegments.find(
        (s) => occ.start_time < Number(s.end_time) && occ.end_time > Number(s.start_time)
      );
    let seg = findSegment();
    if (!seg && mode === 'search' && searchHasMore && !searchLoadingMore) {
      await loadMoreSearchSegments();
      seg = findSegment();
    }
    if (!seg) return;
    await tick();
    const el = modalContentEl?.querySelector<HTMLElement>(`[data-segment-id="${seg.uuid}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('highlight-flash');
      setTimeout(() => el.classList.remove('highlight-flash'), 1500);
    }
  }

  function goToNextOccurrence() {
    if (!totalOccurrences) return;
    currentOccurrenceIdx = (currentOccurrenceIdx + 1) % totalOccurrences;
    void scrollToOccurrence(currentOccurrenceIdx);
  }

  function goToPrevOccurrence() {
    if (!totalOccurrences) return;
    currentOccurrenceIdx = (currentOccurrenceIdx - 1 + totalOccurrences) % totalOccurrences;
    void scrollToOccurrence(currentOccurrenceIdx);
  }
</script>

<div class="transcript-view-modal-wrapper" class:search-transcript-modal-wrapper={mode === 'search'}>
<BaseModal {isOpen} size="2xl" onClose={handleClose}>
  <svelte:fragment slot="header">
    <h2 class="modal-title">
      {mode === 'search' ? $t('searchTranscript.title', { fileName }) : $t('transcriptModal.title', { fileName })}
    </h2>

    {#if activeShowRedactionToggle}
      <button
        type="button"
        class="redaction-link-btn"
        on:click={handleToggleRedaction}
        disabled={activeRedactionBusy}
        title={activeShowOriginal
          ? $t('settings.contentRedaction.showRedactedTooltip')
          : $t('settings.contentRedaction.showOriginalTooltip')}
      >
        {#if activeRedactionBusy}
          <Spinner size="small" />
        {/if}
        {activeShowOriginal
          ? $t('settings.contentRedaction.showRedacted')
          : $t('settings.contentRedaction.showOriginal')}
      </button>
    {/if}

    {#if mode === 'file'}
      <div class="header-actions">
        {#if activeTranscriptSegments.length > 0 && !$supportSessionGate.active}
          <button
            class="copy-button-header"
            class:copied={copyStatus === 'copied'}
            on:click={() => dispatch('copyTranscript')}
            disabled={copyStatus === 'copying'}
            aria-label={$t('transcriptModal.copyTranscript')}
            title={copyStatus === 'copied' ? $t('transcriptModal.transcriptCopied') : $t('transcriptModal.copyTranscript')}
          >
            {#if copyStatus === 'copied'}
              {$t('transcriptModal.copied')}
            {:else if copyStatus === 'copying'}
              {$t('transcriptModal.copying')}
            {:else if copyStatus === 'failed'}
              {$t('transcriptModal.copyFailed')}
            {:else if copyStatus === 'empty'}
              {$t('transcriptModal.noContent')}
            {:else}
              {$t('transcriptModal.copy')}
            {/if}
          </button>
        {/if}
      </div>
    {/if}
  </svelte:fragment>

  {#if mode === 'search'}
    <div class="search-nav-bar">
      <span class="search-query-echo">"{searchQuery}"</span>
      {#if classifiedCounts}
        <span class="match-legend">
          <span class="legend-item keyword">
            {$t('searchTranscript.keywordLegendCount', {
              count: classifiedCounts.keyword + (classifiedCounts.keyword >= 10 ? '+' : ''),
            })}
          </span>
          <span class="legend-item semantic">
            {$t('searchTranscript.semanticLegendCount', {
              count: classifiedCounts.semantic + (classifiedCounts.semantic >= 10 ? '+' : ''),
            })}
          </span>
        </span>
      {/if}
      {#if searchLoadingMore}
        <span class="loading-more-hint"><Spinner size="small" />{$t('searchTranscript.loadingMore')}</span>
      {/if}
      {#if totalOccurrences > 0}
        <span class="nav-controls">
          <span class="nav-count">{$t('searchTranscript.matchCount', { current: currentOccurrenceIdx + 1, total: totalOccurrences })}</span>
          <button class="nav-btn" on:click={goToPrevOccurrence} aria-label={$t('transcriptModal.previousMatch')} title={$t('transcriptModal.previousMatchShortcut')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="15,18 9,12 15,6"></polyline>
            </svg>
          </button>
          <button class="nav-btn" on:click={goToNextOccurrence} aria-label={$t('transcriptModal.nextMatch')} title={$t('transcriptModal.nextMatchShortcut')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="9,18 15,12 9,6"></polyline>
            </svg>
          </button>
        </span>
      {/if}
    </div>
  {/if}

  <div class="modal-content-wrapper" bind:this={modalContentEl}>
    {#if mode === 'search' && searchLoading}
      <div class="loading-container"><Spinner size="large" /><p>{$t('transcriptModal.loading')}</p></div>
    {:else if mode === 'search' && searchError}
      <div class="error-container">
        <h3>{$t('transcriptModal.errorTitle')}</h3>
        <p>{searchError}</p>
      </div>
    {:else if activeTranscriptSegments.length > 0}
      <TranscriptSegmentList
        file={activeFile}
        groupedTranscriptSegments={activeGroupedSegments}
        {speakerList}
        {diarizationDisabled}
        editable={false}
        editingSegmentId={null}
        editingSegmentText=""
        savingTranscript={false}
        searchQuery={mode === 'file' ? '' : searchQuery}
        {segmentClassification}
        totalSegments={activeTotalSegments}
        hasMoreSegments={activeHasMore}
        loadingMoreSegments={activeLoadingMore}
        on:loadMore={handleLoadMore}
      />
    {:else}
      <div class="no-transcript">
        {#if mode === 'search'}
          <p>{$t('searchTranscript.noTranscript')}</p>
        {:else}
          <h3>{$t('transcriptModal.noTranscriptTitle')}</h3>
          <p>{$t('transcriptModal.noTranscriptMessage')}</p>
        {/if}
      </div>
    {/if}
  </div>
</BaseModal>
</div>

<style>
  .transcript-view-modal-wrapper :global(.modal-body) {
    padding: 0 !important;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .modal-title {
    font-size: var(--font-size-2xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
    margin: 0;
    margin-right: var(--space-6);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  .copy-button-header.copied {
    background-color: var(--success-bg);
    border-color: var(--success-color);
    color: var(--success-text);
  }

  .redaction-link-btn {
    background: none;
    border: none;
    padding: 0.15rem 0.35rem;
    border-radius: var(--radius-sm);
    font-size: 0.78rem;
    font-weight: var(--font-weight-medium);
    color: var(--primary-on-surface);
    cursor: pointer;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
  .redaction-link-btn:disabled {
    opacity: 0.6;
    cursor: wait;
  }

  /* D14 — sticky nav bar (ported from the deleted SearchTranscriptModal, #745). */
  .search-nav-bar {
    position: sticky;
    top: 0;
    z-index: 5;
    display: flex;
    align-items: center;
    gap: var(--space-4);
    padding: 0.6rem var(--space-6);
    border-bottom: 1px solid var(--border-color);
    background-color: var(--bg-secondary);
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  .search-query-echo {
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 260px;
  }

  .match-legend {
    display: flex;
    gap: var(--space-3);
    font-size: 0.8rem;
  }

  .legend-item.keyword {
    color: var(--text-primary);
  }
  .legend-item.semantic {
    color: var(--text-secondary);
  }

  .loading-more-hint {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .nav-controls {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-left: auto;
  }

  .nav-count {
    font-size: 0.85rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .nav-btn {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    padding: var(--space-1);
    cursor: pointer;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 28px;
    min-height: 28px;
  }
  .nav-btn:hover {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    color: var(--text-primary);
  }

  .modal-content-wrapper {
    flex: 1;
    overflow: auto;
    padding: var(--space-6);
  }

  .loading-container,
  .error-container,
  .no-transcript {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-12);
    text-align: center;
    gap: var(--space-4);
    color: var(--text-secondary);
  }

  .no-transcript h3 {
    margin: 0;
    color: var(--text-primary);
    font-size: var(--font-size-xl);
    font-weight: var(--font-weight-semibold);
  }
</style>
