<script lang="ts">
  import FiltersIcon from '$components/ui/FiltersIcon.svelte';
  import { resolve } from '$app/paths';
  import { onMount, onDestroy, tick } from 'svelte';
  import { pageStore } from '$stores/pageStore';
  import { goto, beforeNavigate } from '$app/navigation';
  import axiosInstance, { isRequestCancelled } from '$lib/axios';
  import { t } from '$stores/locale';
  import { getErrorMessage, getErrorStatus } from '$lib/utils/apiError';
  import { searchStore, type SearchResponse, type SearchOccurrence, type SearchSource, SEARCH_SOURCES } from '$stores/search';
  import SearchResultsBody from '$components/search/SearchResultsBody.svelte';
  import TranscriptViewModal from '$components/transcript/TranscriptViewModal.svelte';
  import SearchPagination from '$components/search/SearchPagination.svelte';
  import SummaryModal from '$components/SummaryModal.svelte';
  import FilterSidebar from '$components/FilterSidebar.svelte';
  import FilterPanelToggle from '$components/ui/FilterPanelToggle.svelte';
  import FilterChipButton from '$components/ui/FilterChipButton.svelte';
  import SearchAutocomplete from '$components/search/SearchAutocomplete.svelte';
  import SortDropdown, { type SortOption } from '$components/ui/SortDropdown.svelte';
  import type { PlayerHandle } from '$lib/player/types';
  import {
    parseSearchUrl,
    buildPageUrlParams,
    buildApiParams,
    serializeApiParams,
    searchCacheKey,
    applyFilterDetail,
    resetStoreFilters
  } from '$lib/mediaSearch/searchParams';
  import { openPreviewMedia, speakerAtTime } from '$lib/mediaSearch/previewMedia';
  import { dateRangeFromStore, searchRestoreAction } from '$lib/mediaSearch/restoreState';
  import { watchPanelViewport } from '$lib/mediaSearch/viewport';
  import CardGridSkeleton from '$components/ui/CardGridSkeleton.svelte';

  const searchSortOptions: SortOption[] = [
    { value: 'relevance', label: 'search.sort.relevance', noDirection: true },
    { value: 'upload_time', label: 'gallery.sort.uploadDate' },
    { value: 'completed_at', label: 'gallery.sort.completedDate' },
    { value: 'filename', label: 'gallery.sort.filename' },
    { value: 'duration', label: 'gallery.sort.duration' },
    { value: 'file_size', label: 'gallery.sort.fileSize' },
  ];

  let searchInput = '';
  let previewMediaUrl = '';
  let showFilters = true;
  let sidebarMounted = false;
  let neuralSearchActive: boolean | null = null; // null = loading/unknown

  // Cancellation for the in-flight `GET /search`. Sorting, filtering, paging and
  // typing all re-run `performSearch`, and a slow earlier query used to resolve
  // *after* a newer one and overwrite the results with stale hits. Aborting the
  // previous request makes the most recent call the only one that can win.
  let searchController: AbortController | null = null;
  let removeViewportListeners: (() => void) | null = null;

  // FilterSidebar state
  let filterSelectedTags: string[] = [];
  let filterSelectedSpeakers: string[] = [];
  let filterDateRange: { from: Date | null; to: Date | null } = { from: null, to: null };
  let filterSelectedCollectionId: string | null = null;
  let filterSelectedLanguage: string | null = null;
  let filterDurationRange: { min: number | null; max: number | null } = { min: null, max: null };
  let filterSelectedFileTypes: string[] = [];
  let filterSelectedStatuses: string[] = [];

  // Detect if any sidebar filters are active
  $: hasActiveFilters =
    filterSelectedTags.length > 0 ||
    filterSelectedSpeakers.length > 0 ||
    filterSelectedFileTypes.length > 0 ||
    filterSelectedStatuses.length > 0 ||
    filterSelectedCollectionId !== null ||
    filterSelectedLanguage !== null ||
    filterDateRange.from !== null ||
    filterDurationRange.min !== null || filterDurationRange.max !== null;

  // Sticky preview player state
  let previewData: { fileUuid: string; title: string; startTime: number; speaker: string; contentType: string } | null = null;
  // `playing` follows the real player (native play/pause; `ended` also reports pause), so a
  // result card shows pause only while audio is actually running.
  let activePreview: { fileUuid: string; startTime: number; playing: boolean } | null = null;
  let previewPlayer: { getPlayer(): PlayerHandle | null } | null = null;
  // The player brings Video.js (about 56 KB gzip), so it is fetched with the first preview, in
  // parallel with that preview's stream URL, not with the page (issue #1327). The bundle gate
  // fails if it ever becomes a static import of this route again.
  type PreviewPlayerComponent = typeof import('$components/FloatingPreviewPlayer.svelte').default;
  let FloatingPreviewPlayer: PreviewPlayerComponent | null = null;
  let previewPlayerLoad: Promise<PreviewPlayerComponent | null> | null = null;

  /** The player component, loaded once; resolves null (and retries next time) if the fetch fails. */
  function loadPreviewPlayer(): Promise<PreviewPlayerComponent | null> {
    previewPlayerLoad ??= import('$components/FloatingPreviewPlayer.svelte').then(
      (m) => (FloatingPreviewPlayer = m.default),
      (err) => {
        console.error('Failed to load the preview player:', err);
        previewPlayerLoad = null;
        return null;
      }
    );
    return previewPlayerLoad;
  }
  // Each click bumps this; a stream-URL fetch that resolves after a newer click is dropped.
  let previewRequest = 0;

  // The knowledge-panel aside needs ~44rem column + 2rem gap + 19rem panel of CONTENT width;
  // the filter sidebar takes 320px open / 50px collapsed (docs/design/search_results_ux_audit.md).
  let wideViewport = false;
  let midViewport = false;
  $: panelMode = showFilters ? wideViewport : midViewport;
  let previewCurrentTime = 0;
  let previewCurrentSpeaker = '';
  let previewUrlRefresher: { stop: () => void } | null = null;

  // Search transcript modal state
  let transcriptModalOpen = false;
  let transcriptModalFileUuid = '';
  let transcriptModalFileName = '';
  let transcriptModalOccurrences: SearchOccurrence[] = [];

  // Summary modal state (issue #462) — opened from a summary search hit, scrolled to
  // the matched section via SummaryModal's own find/highlight machinery.
  // `SummaryModal`'s `fileId` prop is (despite the name) the file's UUID, not its
  // integer id — see the note on that prop.
  let summaryModalOpen = false;
  let summaryModalFileUuid: string | null = null;
  let summaryModalFileName = '';
  let summaryModalKeyPath: string | null = null;

  // Issue #760, §7.1: `total_pages` has been `max(transcript_pages, summary_pages)`
  // server-side since #831 — DO NOT reintroduce a client-side derivation. Both
  // sections share the single `$searchStore.totalPages` pager below.
  $: showTranscriptSection = $searchStore.selectedSources.some(
    (s) => s === 'content' || s === 'title' || s === 'speaker'
  );
  $: showSummarySection = $searchStore.selectedSources.includes('summary');
  $: hasAnySourceSelected = $searchStore.selectedSources.length > 0;

  function sourceLabel(source: SearchSource): string {
    switch (source) {
      case 'content':
        return $t('search.sourceTranscript');
      case 'title':
        return $t('search.sourceTitle');
      case 'speaker':
        return $t('search.sourceSpeaker');
      case 'summary':
        return $t('search.sourceSummary');
    }
  }

  function findSpeakerAtTime(time: number): string {
    if (!previewData) return '';
    // Find the matching file in results to get all occurrences
    const hit = $searchStore.results.find(r => r.file_uuid === previewData!.fileUuid);
    if (!hit) return previewData.speaker || '';
    return speakerAtTime(hit.occurrences, time, previewData.speaker || '');
  }

  // Read initial state from URL
  $: urlState = parseSearchUrl($pageStore.url.searchParams);
  $: urlQuery = urlState.query;
  $: urlPage = urlState.page;
  $: urlSort = urlState.sort;
  $: urlSortOrder = urlState.sortOrder;
  $: urlMode = urlState.mode;
  $: urlSources = urlState.sources;
  $: urlSpeakers = urlState.speakers;
  $: urlTags = urlState.tags;
  $: urlCollection = urlState.collection;

  onMount(() => {
    // Restore search input: prefer URL param, fall back to store query
    const restoredQuery = urlQuery || $searchStore.query;
    searchInput = restoredQuery;

    searchStore.setSortBy(urlSort);
    searchStore.setSortOrder(urlSortOrder);
    searchStore.setSearchMode(urlMode);
    if (urlSources) searchStore.setSources(urlSources);
    if (urlSpeakers.length) searchStore.setSpeakers(urlSpeakers);
    if (urlTags.length) searchStore.setTags(urlTags);
    if (urlCollection) searchStore.setCollectionId(urlCollection);

    // Restore filter sidebar state from search store
    filterSelectedTags = [...$searchStore.selectedTags];
    filterSelectedSpeakers = [...$searchStore.selectedSpeakers];
    filterSelectedFileTypes = [...$searchStore.selectedFileTypes];
    filterSelectedStatuses = [...$searchStore.selectedStatuses];
    filterSelectedCollectionId = $searchStore.selectedCollectionId;
    filterSelectedLanguage = $searchStore.selectedLanguage;
    filterDurationRange = { ...$searchStore.durationRange };
    const restoredDates = dateRangeFromStore($searchStore.dateFrom, $searchStore.dateTo);
    if (restoredDates) filterDateRange = restoredDates;

    // D3: reuse cached results when they are the ones this URL asks for
    const restore = searchRestoreAction(
      restoredQuery,
      buildSearchParamsString(restoredQuery, urlPage),
      {
        lastSearchParams: $searchStore.lastSearchParams,
        resultsLength: $searchStore.results.length,
        query: $searchStore.query
      }
    );
    if (restore === 'reuse-cached') {
      // Results match - skip API call and restore scroll position
      searchInput = restoredQuery;
      if ($searchStore.scrollPosition > 0) {
        requestAnimationFrame(() => {
          const scrollable = document.querySelector('.scrollable-content');
          if (scrollable) scrollable.scrollTop = $searchStore.scrollPosition;
        });
      }
    } else if (restore === 'keep-results') {
      // Store has matching results even if params string differs (e.g. back navigation)
      searchInput = restoredQuery;
    } else if (restore === 'search') {
      performSearch(restoredQuery, urlPage);
    }

    // The search results are restored from the store (above), but the preview
    // player is intentionally NOT reopened on back-navigation — reloading and
    // autoplaying the previous clip is unwanted noise.

    // Collapse filters on mobile by default
    if (window.innerWidth < 768) {
      showFilters = false;
    }

    // Enable sidebar transitions only after initial render is complete
    requestAnimationFrame(() => {
      sidebarMounted = true;
    });

    removeViewportListeners = watchPanelViewport(({ wide, mid }) => {
      wideViewport = wide;
      midViewport = mid;
    });

    // Check neural search availability
    axiosInstance.get('/search/models/neural').then((res) => {
      neuralSearchActive = !!(res.data?.neural_enabled && res.data?.active_model_id);
    }).catch(() => {
      neuralSearchActive = false;
    });
  });

  function buildSearchParamsString(query: string, pageNum: number): string {
    return searchCacheKey(query, pageNum, $searchStore);
  }

  async function performSearch(query: string, pageNum: number = 1) {
    if (!query.trim()) return;
    // Issue #760, J-760-8: zero sources selected is a deliberate UI state —
    // no request is sent, the pill row's last counts stay on screen.
    if ($searchStore.selectedSources.length === 0) return;

    // Supersede any in-flight search so its response can't land after this one.
    searchController?.abort();
    const controller = new AbortController();
    searchController = controller;

    searchStore.setQuery(query);
    searchStore.setLoading(true);

    // Update URL without navigation
    const params = buildPageUrlParams(query, pageNum, $searchStore);

    goto(resolve(`/search?${params.toString()}`), { replace: true, reset: false });

    try {
      const res = await axiosInstance.get('/search', {
        params: buildApiParams(query, pageNum, $searchStore),
        signal: controller.signal,
        paramsSerializer: serializeApiParams,
      });

      const searchData = res.data as SearchResponse;
      searchStore.setResults(searchData);
      // D3: Store params that produced these results
      searchStore.setLastSearchParams(buildSearchParamsString(query, pageNum));
    } catch (e: unknown) {
      // A superseded search is not a failure: a newer request owns the results
      // and the loading flag, so leave both alone.
      if (isRequestCancelled(e)) return;
      if (getErrorStatus(e) === 429) {
        // Localized throttle copy, not the English string
        // rate_limit_exceeded_handler puts in the response detail (issue #904).
        searchStore.setError($t('common.rateLimited'));
      } else {
        console.error('Search failed:', e);
        searchStore.setError(getErrorMessage(e, $t('search.searchFailed')));
      }
      searchStore.setLoading(false);
    } finally {
      if (searchController === controller) searchController = null;
    }
  }

  function handleSearch() {
    performSearch(searchInput, 1);
  }

  function handleClearSearch() {
    searchInput = '';
    searchStore.reset();
    // Update URL to remove search params
    const url = new URL(window.location.href);
    url.searchParams.delete('q');
    url.searchParams.delete('page');
    const remaining = url.searchParams.toString();
    goto(resolve(remaining ? `/search?${remaining}` : '/search'), { replace: true });
  }

  function handlePageChange(event: CustomEvent<number>) {
    performSearch($searchStore.query, event.detail);
    // Scroll to top of results
    const scrollable = document.querySelector('.scrollable-content');
    if (scrollable) {
      scrollable.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }

  function handleSortChange(event: CustomEvent<{ sortBy: string; sortOrder: 'asc' | 'desc' }>) {
    const { sortBy, sortOrder } = event.detail;
    searchStore.setSort(sortBy, sortOrder);
    if ($searchStore.query) {
      performSearch($searchStore.query, 1);
    }
  }

  function handleSearchModeChange(mode: string) {
    searchStore.setSearchMode(mode);
    if ($searchStore.query) {
      performSearch($searchStore.query, 1);
    }
  }

  function handleToggleSource(source: SearchSource) {
    searchStore.toggleSource(source);
    // Zero-sources (J-760-8): don't search, just show the deliberate empty state.
    if ($searchStore.query && $searchStore.selectedSources.length > 0) {
      performSearch($searchStore.query, 1);
    }
  }

  function handleOpenSummaryMatch(
    event: CustomEvent<{ fileUuid: string; title: string; keyPath: string | null }>
  ) {
    const { fileUuid, title, keyPath } = event.detail;
    summaryModalFileUuid = fileUuid;
    summaryModalFileName = title;
    summaryModalKeyPath = keyPath;
    summaryModalOpen = true;
  }

  function handleFilterEvent(event: CustomEvent) {
    const detail = event.detail;
    if (!detail) return;

    applyFilterDetail(detail);

    // Re-run search with new filters
    if ($searchStore.query) {
      performSearch($searchStore.query, 1);
    }
  }

  // Tag / collection chips in the info card: filter the current query in place, keeping the
  // other filters. With the sidebar open its own collection watcher applies the change (and
  // would otherwise search a second time); collapsed, it is not mounted, so search here.
  function handleFilterByTag(event: CustomEvent<string>) {
    filterSelectedTags = [event.detail];
    searchStore.setTags([event.detail]);
    if ($searchStore.query) performSearch($searchStore.query, 1);
  }

  function handleFilterByCollection(event: CustomEvent<string>) {
    filterSelectedCollectionId = event.detail;
    if (showFilters) return;
    searchStore.setCollectionId(event.detail);
    if ($searchStore.query) performSearch($searchStore.query, 1);
  }

  function handleFilterReset() {
    resetStoreFilters();
    filterSelectedTags = [];
    filterSelectedSpeakers = [];
    filterDateRange = { from: null, to: null };
    filterSelectedCollectionId = null;
    filterSelectedLanguage = null;
    filterDurationRange = { min: null, max: null };
    filterSelectedFileTypes = [];
    filterSelectedStatuses = [];

    if ($searchStore.query) {
      performSearch($searchStore.query, 1);
    }
  }

  function handleSuggestionSelect(event: CustomEvent<string>) {
    searchInput = event.detail;
    performSearch(event.detail, 1);
  }

  // Sticky preview player
  async function handlePreview(event: CustomEvent) {
    const data = event.detail;
    if (!data) {
      closePreview();
      return;
    }

    // Mark the new occurrence active right away (not playing yet), so the previous card
    // drops its pause state at the click rather than after the URL round trip.
    const request = ++previewRequest;
    activePreview = { fileUuid: data.fileUuid, startTime: data.startTime, playing: false };
    const playerLoad = loadPreviewPlayer();

    // Tear down existing preview to force full re-render
    previewData = null;
    await tick();

    // Stop any existing URL refresher
    if (previewUrlRefresher) {
      previewUrlRefresher.stop();
      previewUrlRefresher = null;
    }

    // Fetch presigned URL before rendering the media element
    let media: Awaited<ReturnType<typeof openPreviewMedia>>;
    try {
      media = await openPreviewMedia(data.fileUuid, (newUrl) => {
        previewMediaUrl = newUrl;
      });
    } catch (err) {
      console.error('Failed to get media stream URL:', err);
      if (request === previewRequest) activePreview = null;
      return;
    }
    if (request !== previewRequest) {
      // A newer click won while the URL was in flight: don't leave its refresher running.
      media.refresher.stop();
      return;
    }
    if (!(await playerLoad) || request !== previewRequest) {
      media.refresher.stop();
      if (request === previewRequest) activePreview = null;
      return;
    }
    previewMediaUrl = media.url;
    previewUrlRefresher = media.refresher;

    previewData = data;
    previewCurrentTime = data.startTime;
    previewCurrentSpeaker = data.speaker || '';
  }

  function setPreviewPlaying(playing: boolean) {
    if (activePreview && activePreview.playing !== playing) {
      activePreview = { ...activePreview, playing };
    }
  }

  // The result card's control for the clip already loaded: pause it, or resume it.
  function handleTogglePlayback() {
    const player = previewPlayer?.getPlayer();
    if (!player || !activePreview) return;
    if (activePreview.playing) {
      player.pause();
    } else {
      void player.play();
    }
  }

  function handleViewTranscript(event: CustomEvent) {
    const { fileUuid, title, occurrences } = event.detail;
    transcriptModalFileUuid = fileUuid;
    transcriptModalFileName = title;
    transcriptModalOccurrences = occurrences;
    transcriptModalOpen = true;
  }

  function handlePreviewTimeUpdate(event: CustomEvent<{ currentTime: number }>) {
    previewCurrentTime = event.detail.currentTime;
    previewCurrentSpeaker = findSpeakerAtTime(previewCurrentTime);
  }

  function closePreview() {
    if (previewUrlRefresher) {
      previewUrlRefresher.stop();
      previewUrlRefresher = null;
    }
    previewRequest++;
    previewData = null;
    activePreview = null;
  }

  // Save all transient state before navigating away
  function saveState() {
    // Save scroll position
    const scrollable = document.querySelector('.scrollable-content');
    if (scrollable) {
      searchStore.setScrollPosition(scrollable.scrollTop);
    }
  }

  // Catch SvelteKit client-side navigation (e.g. clicking "Jump to" link)
  beforeNavigate(() => {
    saveState();
  });

  function formatSearchTime(ms: number): string {
    return (ms / 1000).toFixed(2);
  }

  onDestroy(() => {
    saveState();
    searchController?.abort();
    searchController = null;
    removeViewportListeners?.();
    if (previewUrlRefresher) {
      previewUrlRefresher.stop();
      previewUrlRefresher = null;
    }
  });
</script>

<svelte:head>
  <title>{$searchStore.query ? `${$searchStore.query} - ` : ''}{$t('search.title')}</title>
</svelte:head>

<div class="search-page">
  <!-- Left Sidebar: Filters (Sticky) -->
  <div class="filter-sidebar {showFilters ? 'show' : ''}" class:animate={sidebarMounted}>
    <div class="filter-toggle-container">
      <FilterPanelToggle expanded={showFilters} on:click={() => (showFilters = !showFilters)} />
    </div>

    <!-- Filter Content (hidden when collapsed) -->
    {#if showFilters}
      <div class="filter-content">
        <FilterSidebar
          bind:selectedTags={filterSelectedTags}
          bind:selectedSpeakers={filterSelectedSpeakers}
          bind:dateRange={filterDateRange}
          bind:selectedCollectionId={filterSelectedCollectionId}
          bind:selectedLanguage={filterSelectedLanguage}
          bind:durationRange={filterDurationRange}
          bind:selectedFileTypes={filterSelectedFileTypes}
          bind:selectedStatuses={filterSelectedStatuses}
          showOwnerFilter={false}
          on:filter={handleFilterEvent}
          on:reset={handleFilterReset}
        />
      </div>
    {/if}
  </div>

  <!-- Main Content Area -->
  <div class="content-area">
    <div class="scrollable-content">
      <!-- Search Header -->
      <header class="search-header">
        <div class="search-title-row">
          <a href={resolve('/')} class="back-to-gallery" title={$t('nav.backToGallery')}>
            <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="15 18 9 12 15 6"></polyline>
            </svg>
          </a>
          <h1 class="search-title">{$t('search.title')}</h1>
        </div>
        <div class="search-bar">
          <SearchAutocomplete
            bind:value={searchInput}
            on:search={handleSearch}
            on:select={handleSuggestionSelect}
            on:clear={handleClearSearch}
            placeholder={$t('searchPage.placeholder')}
          />
          <button class="search-btn" on:click={handleSearch} disabled={$searchStore.isLoading} aria-label={$t('searchPage.search')}>
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
          </button>
        </div>

        {#if $searchStore.query}
          <div
            class="source-toggle"
            role="group"
            aria-label={$t('search.sourceToggleLabel')}
            aria-describedby="search-source-counts-hint"
          >
            {#each SEARCH_SOURCES as source (source)}
              <FilterChipButton
                selected={$searchStore.selectedSources.includes(source)}
                showCheck
                count={$searchStore.sourceCounts[source] ?? null}
                on:click={() => handleToggleSource(source)}
              >
                {sourceLabel(source)}
              </FilterChipButton>
            {/each}
          </div>
          <p id="search-source-counts-hint" class="visually-hidden">
            {$t('search.sourceCountsAreKeyword')}
          </p>
        {/if}

        {#if hasActiveFilters}
          <div class="filter-hint">
            <FiltersIcon size={12} />
            {$t('search.filtersApplied')}
          </div>
        {/if}

        <!-- Results Info Bar (transcript-plane only — the mode toggle/neural status/sort
             below are all transcript-specific; the summary section has its own count line). -->
        {#if showTranscriptSection && $searchStore.query && !$searchStore.isLoading && $searchStore.totalResults >= 0 && $searchStore.results.length > 0}
          <div class="results-info">
            <span class="result-summary">
              {$t('search.results', { count: $searchStore.totalFiles, time: formatSearchTime($searchStore.searchTimeMs) })}
            </span>
            <div class="results-controls">
              <!-- Neural search status indicator -->
              {#if neuralSearchActive !== null}
                <div class="neural-status" class:active={neuralSearchActive} title={neuralSearchActive ? $t('search.neuralActive') : $t('search.neuralInactiveTooltip')}>
                  <span class="neural-dot"></span>
                  <span class="neural-label">{neuralSearchActive ? $t('search.neuralActive') : $t('search.neuralInactive')}</span>
                </div>
              {/if}
              <!-- Search Mode Toggle -->
              <div class="mode-toggle">
                <button
                  class="mode-btn"
                  class:active={$searchStore.searchMode === 'hybrid'}
                  on:click={() => handleSearchModeChange('hybrid')}
                  title={$t('search.smartModeDesc')}
                >
                  {$t('search.smartMode')}
                </button>
                <button
                  class="mode-btn"
                  class:active={$searchStore.searchMode === 'keyword'}
                  on:click={() => handleSearchModeChange('keyword')}
                  title={$t('search.exactModeDesc')}
                >
                  {$t('search.exactMode')}
                </button>
              </div>
              <SortDropdown
                sortOptions={searchSortOptions}
                sortBy={$searchStore.sortBy}
                sortOrder={$searchStore.sortOrder}
                ariaLabelKey="search.sort.label"
                align="right"
                on:change={handleSortChange}
              />
            </div>
          </div>
        {/if}
      </header>

      <!-- Results. `<div>`, not `<main>`: this nests inside AppContent.svelte's
           `<main id="main-content">` landmark, and two nested `main` landmarks is itself an
           axe finding (issue #785). -->
      <div class="results">
        {#if $searchStore.isLoading}
          <CardGridSkeleton variant="search" count={6} />
        {:else if $searchStore.error}
          <div class="state-container error">
            <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="12" y1="8" x2="12" y2="12"></line>
              <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
            <p class="state-text">{$searchStore.error}</p>
          </div>
        {:else if !$searchStore.query}
          <div class="state-container welcome">
            <svg xmlns="http://www.w3.org/2000/svg" width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" class="empty-icon">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <p class="state-title">{$t('searchPage.placeholder')}</p>
            <p class="state-hint">{$t('search.welcomeHint')}</p>
            <p class="state-hint search-tip">{$t('search.speakerSearchTip')}</p>
          </div>
        {:else if !hasAnySourceSelected}
          <!-- J-760-8: a deliberate, reachable state — not an error. -->
          <div class="state-container">
            <svg xmlns="http://www.w3.org/2000/svg" width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" class="empty-icon">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <p class="state-title">{$t('search.noSourcesSelected')}</p>
            <p class="state-hint">{$t('search.noSourcesSelectedHint')}</p>
          </div>
        {:else}
          <!-- Issue #760: one collapsible section per selected source plane (OR-union, never
               AND-intersected). Markup and the E2E selectors it carries live in
               SearchResultsBody. -->
          <SearchResultsBody
            query={$searchStore.query}
            searchMode={$searchStore.searchMode}
            {showSummarySection}
            {showTranscriptSection}
            results={$searchStore.results}
            summaryResults={$searchStore.summaryResults}
            summaryTotal={$searchStore.summaryTotal}
            summaryUnavailable={$searchStore.summaryUnavailable}
            {activePreview}
            {panelMode}
            matchListOpen={$searchStore.matchListOpen}
            on:setMatchListsOpen={(e) => searchStore.setMatchListsOpen(e.detail.fileUuids, e.detail.open)}
            on:filterTag={handleFilterByTag}
            on:filterCollection={handleFilterByCollection}
            on:openMatch={handleOpenSummaryMatch}
            on:preview={handlePreview}
            on:togglePlayback={handleTogglePlayback}
            on:viewTranscript={handleViewTranscript}
          />

          {#if $searchStore.totalPages > 1}
            <div class="pagination-slot">
              <SearchPagination
                page={$searchStore.page}
                totalPages={$searchStore.totalPages}
                on:pageChange={handlePageChange}
              />
            </div>
          {/if}
        {/if}
      </div>
    </div>
  </div>

  <!-- Sticky Floating Preview Player -->
  {#if previewData && FloatingPreviewPlayer}
    <svelte:component
      this={FloatingPreviewPlayer}
      bind:this={previewPlayer}
      title={previewData.title}
      subtitle={previewCurrentSpeaker}
      currentTime={previewCurrentTime}
      jumpToFileId={previewData.fileUuid}
      jumpToTime={previewCurrentTime || previewData.startTime}
      closeTitle={$t('search.closePreview')}
      closeAriaLabel={$t('search.closePreviewAriaLabel')}
      mediaUrl={previewMediaUrl}
      contentType={previewData.contentType}
      startTime={previewData.startTime}
      fileId={previewData.fileUuid}
      autoplay={true}
      on:close={closePreview}
      on:timeupdate={handlePreviewTimeUpdate}
      on:play={() => setPreviewPlaying(true)}
      on:pause={() => setPreviewPlaying(false)}
    />
  {/if}

  <TranscriptViewModal
    mode="search"
    bind:isOpen={transcriptModalOpen}
    fileUuid={transcriptModalFileUuid}
    fileName={transcriptModalFileName}
    searchQuery={$searchStore.query}
    occurrences={transcriptModalOccurrences}
    on:close={() => transcriptModalOpen = false}
  />

  {#if summaryModalFileUuid !== null}
    <SummaryModal
      fileId={summaryModalFileUuid}
      fileName={summaryModalFileName}
      isOpen={summaryModalOpen}
      scrollToKeyPath={summaryModalKeyPath}
      on:close={() => (summaryModalOpen = false)}
    />
  {/if}
</div>

<style>
  .search-page {
    display: flex;
    height: calc(100vh - var(--content-top));
    height: calc(100dvh - var(--content-top));
    overflow: hidden;
    padding-top: 0;
  }

  /* Filter Sidebar - matches gallery styling exactly */
  .filter-sidebar {
    flex-shrink: 0;
    background-color: var(--surface-color);
    border-right: 1px solid var(--border-color);
    height: 100%;
    display: flex;
    flex-direction: column;
  }

  /* Only animate after initial mount to prevent flicker on navigation */
  .filter-sidebar.animate {
    transition: width var(--duration-slow) ease;
  }

  /* Expanded state */
  .filter-sidebar.show {
    width: 320px;
  }

  /* Collapsed state */
  .filter-sidebar:not(.show) {
    width: 50px;
  }

  .filter-toggle-container {
    padding: var(--space-2) var(--space-2) 0;
    margin-bottom: var(--space-2);
    flex-shrink: 0;
  }

  .filter-sidebar.show .filter-toggle-container {
    padding: var(--space-2) var(--space-4) 0;
  }

  .filter-content {
    flex: 1;
    overflow-y: auto;
    padding: 0 var(--space-4);
  }

  /* Content Area */
  .content-area {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .scrollable-content {
    flex: 1;
    overflow-y: scroll;
    padding: var(--space-6);
  }

  /* Header */
  /* The header band (title, query field, source chips, results bar) spans the whole
     content area like the gallery toolbar; only the results column below is capped at a
     reading measure (SearchResultsBody). docs/design/search_polish_round2_plan.md, item 1. */
  .search-header {
    margin-bottom: var(--space-6);
  }

  .search-title-row {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    margin-bottom: var(--space-4);
  }

  .back-to-gallery {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    text-decoration: none;
    flex-shrink: 0;
    transition: background var(--duration-fast), color var(--duration-fast);
  }

  .back-to-gallery:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .search-title {
    font-size: var(--font-size-2xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin: 0;
  }

  .search-bar {
    display: flex;
    gap: var(--space-2);
  }

  .filter-hint {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    font-size: var(--font-size-xs);
    color: var(--primary-on-surface);
    opacity: 0.7;
    margin-top: var(--space-1);
  }

  .search-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 52px;
    height: 44px;
    padding: 0 var(--space-4);
    flex-shrink: 0;
    background-color: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--duration-base) ease;
  }

  .search-btn:hover:not(:disabled) {
    background-color: var(--primary-color-dark);
    transform: translateY(-1px);
  }

  .search-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Results Info */
  .results-info {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-3);
    margin-top: var(--space-4);
    padding-bottom: var(--space-3);
    border-bottom: 1px solid var(--border-color);
  }

  .result-summary {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .results-controls {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  /* Search Mode Toggle - sliding pill style */
  .mode-toggle {
    display: flex;
    background: var(--hover-color);
    border-radius: var(--radius-lg);
    padding: var(--space-0-5);
    gap: 0;
  }

  .mode-btn {
    padding: var(--space-1-5) var(--space-3);
    background: transparent;
    border: none;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    cursor: pointer;
    transition: all var(--duration-base) ease;
    white-space: nowrap;
  }

  .mode-btn.active {
    background: var(--primary-color);
    color: var(--text-on-solid);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
  }

  .mode-btn:hover:not(.active) {
    color: var(--text-color);
  }

  /* Multi-select source toggle (issue #760, replaces the exclusive
     transcripts/summaries tab strip from #462). Independent, combinable
     pills — `role="group"` + `aria-pressed`, NOT `tablist`/`tab`/`aria-selected`,
     which are single-select by definition. */
  .source-toggle {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  /* Results */
  .results {
    min-height: 300px;
    width: 100%;
  }

  /* Pagination belongs to the results column, not under the info panel. */
  .pagination-slot {
    max-width: 44rem;
  }

  /* Empty / Loading States */
  .state-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-16) var(--space-8);
    text-align: center;
  }

  .state-container.error {
    color: var(--error-color);
  }

  .state-container svg {
    color: var(--text-secondary);
    margin-bottom: var(--space-4);
  }

  .state-container.error svg {
    color: var(--error-color);
  }

  .state-title {
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    margin: 0 0 var(--space-1-5);
  }

  .state-text {
    font-size: var(--font-size-body);
    color: var(--text-secondary);
    margin: 0;
  }

  .state-hint {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin: 0;
  }

  .search-tip {
    margin-top: var(--space-3);
    font-size: var(--font-size-xs);
    font-style: italic;
    opacity: 0.7;
  }

  .empty-icon {
    opacity: 0.35;
  }

  /* Responsive */
  @media (max-width: 768px) {
    .search-page {
      flex-direction: column;
      overflow-x: hidden;
    }

    /* Slide-in overlay sidebar — matches gallery page pattern */
    .filter-sidebar {
      position: fixed;
      top: var(--content-top);
      left: -100%;
      width: 100% !important;
      min-width: 100% !important;
      height: calc(100vh - var(--content-top));
      height: calc(100dvh - var(--content-top));
      background: var(--surface-color);
      z-index: var(--z-dropdown);
      transition: left var(--duration-slow) ease;
      border-right: none;
      border-top: 1px solid var(--border-color);
    }

    .filter-sidebar.show {
      left: 0;
    }

    .filter-sidebar:not(.show) {
      width: auto !important;
      min-width: auto !important;
      position: static;
      height: auto;
    }

    .content-area {
      width: 100%;
      overflow-x: hidden;
      min-width: 0;
    }

    .scrollable-content {
      padding: var(--space-3);
      overflow-x: hidden;
    }

    .results-info {
      flex-direction: column;
      align-items: flex-start;
    }

    .results-controls {
      flex-wrap: wrap;
      gap: var(--space-2);
    }

    .search-title {
      font-size: var(--font-size-xl);
    }

    .back-to-gallery {
      width: 36px;
      height: 36px;
    }

    .results {
      min-width: 0;
    }
  }

  /* Neural search status indicator */
  .neural-status {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: var(--space-1) var(--space-2-5);
    border-radius: 20px;
    font-size: 0.7rem;
    font-weight: var(--font-weight-medium);
    border: 1px solid;
    cursor: default;
    white-space: nowrap;
  }

  .neural-status.active {
    background: rgba(16, 185, 129, 0.08);
    border-color: rgba(16, 185, 129, 0.25);
    color: #059669;
  }

  :global([data-theme='dark']) .neural-status.active {
    background: rgba(16, 185, 129, 0.1);
    border-color: rgba(16, 185, 129, 0.3);
    color: #34d399;
  }

  .neural-status:not(.active) {
    background: rgba(245, 158, 11, 0.08);
    border-color: rgba(245, 158, 11, 0.25);
    color: #d97706;
  }

  :global([data-theme='dark']) .neural-status:not(.active) {
    background: rgba(245, 158, 11, 0.1);
    border-color: rgba(245, 158, 11, 0.3);
    color: #fbbf24;
  }

  .neural-dot {
    width: 6px;
    height: 6px;
    border-radius: var(--radius-round);
    background: currentColor;
    flex-shrink: 0;
  }

  .neural-status.active .neural-dot {
    animation: pulse-dot 2s ease-in-out infinite;
  }

  @keyframes pulse-dot {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .neural-label {
    line-height: 1;
  }
</style>
