<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { page } from '$app/stores';
  import { goto, beforeNavigate } from '$app/navigation';
  import axiosInstance, { isRequestCancelled } from '$lib/axios';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { searchStore, type SearchResponse, type SearchOccurrence, type SearchResultType } from '$stores/search';
  import SearchResultCard from '$components/search/SearchResultCard.svelte';
  import SearchTranscriptModal from '$components/search/SearchTranscriptModal.svelte';
  import SearchPagination from '$components/search/SearchPagination.svelte';
  import SummaryResultCard from '$components/search/SummaryResultCard.svelte';
  import SummaryModal from '$components/SummaryModal.svelte';
  import FilterSidebar from '$components/FilterSidebar.svelte';
  import SearchAutocomplete from '$components/search/SearchAutocomplete.svelte';
  import SortDropdown, { type SortOption } from '$components/ui/SortDropdown.svelte';
  import FloatingPreviewPlayer from '$components/FloatingPreviewPlayer.svelte';
  import RetrievalQualityNotice from '$components/RetrievalQualityNotice.svelte';
  import { getMediaStreamUrl, getCachedUrlInfo, createUrlRefresher, clearMediaUrlCache } from '$lib/api/mediaUrl';
  import { prefetchNextSearchPage } from '$lib/prefetch';
  import CardGridSkeleton from '../../components/ui/CardGridSkeleton.svelte';

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

  // FilterSidebar state
  let filterSearchQuery = '';
  let filterSelectedTags: string[] = [];
  let filterSelectedSpeakers: string[] = [];
  let filterDateRange: { from: Date | null; to: Date | null } = { from: null, to: null };
  let filterSelectedCollectionId: string | null = null;
  let filterSelectedLanguage: string | null = null;
  let filterDurationRange: { min: number | null; max: number | null } = { min: null, max: null };
  let filterFileSizeRange: { min: number | null; max: number | null } = { min: null, max: null };
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
    filterDurationRange.min !== null || filterDurationRange.max !== null ||
    filterFileSizeRange.min !== null || filterFileSizeRange.max !== null ||
    filterSearchQuery !== '';

  // Sticky preview player state
  let previewData: { fileUuid: string; title: string; startTime: number; speaker: string; contentType: string } | null = null;
  let activePreview: { fileUuid: string; startTime: number } | null = null;
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

  // Backend total_pages is only computed for the transcripts leg (result_type=summaries
  // gets the placeholder `SearchResponseSchema` with total_pages hardcoded 0 — see
  // `api/endpoints/search.py::search_transcripts`). Derived client-side here as a
  // purely-presentational value from data the page already has, same exception as
  // export formatting gets under the thin-frontend rule.
  $: summaryTotalPages = Math.ceil(($searchStore.summaryTotal || 0) / ($searchStore.pageSize || 20));

  function findSpeakerAtTime(time: number): string {
    if (!previewData) return '';
    // Find the matching file in results to get all occurrences
    const hit = $searchStore.results.find(r => r.file_uuid === previewData!.fileUuid);
    if (!hit) return previewData.speaker || '';
    // Find the occurrence whose time range contains the current time
    for (const occ of hit.occurrences) {
      if (time >= occ.start_time && time <= occ.end_time) {
        return occ.speaker || '';
      }
    }
    return previewData.speaker || '';
  }

  // Read initial state from URL
  $: urlQuery = $page.url.searchParams.get('q') || '';
  $: urlPage = parseInt($page.url.searchParams.get('page') || '1');
  $: urlSort = $page.url.searchParams.get('sort') || 'relevance';
  $: urlSortOrder = ($page.url.searchParams.get('sort_order') || 'desc') as 'asc' | 'desc';
  $: urlMode = $page.url.searchParams.get('mode') || 'hybrid';
  $: urlType = ($page.url.searchParams.get('type') || 'transcripts') as SearchResultType;
  $: urlSpeakers = $page.url.searchParams.getAll('speakers');
  $: urlTags = $page.url.searchParams.getAll('tags');

  onMount(() => {
    // Restore search input: prefer URL param, fall back to store query
    const restoredQuery = urlQuery || $searchStore.query;
    searchInput = restoredQuery;

    searchStore.setSortBy(urlSort);
    searchStore.setSortOrder(urlSortOrder);
    searchStore.setSearchMode(urlMode);
    searchStore.setResultType(urlType);
    if (urlSpeakers.length) searchStore.setSpeakers(urlSpeakers);
    if (urlTags.length) searchStore.setTags(urlTags);

    // Restore filter sidebar state from search store
    filterSelectedTags = [...$searchStore.selectedTags];
    filterSelectedSpeakers = [...$searchStore.selectedSpeakers];
    filterSelectedFileTypes = [...$searchStore.selectedFileTypes];
    filterSelectedStatuses = [...$searchStore.selectedStatuses];
    filterSelectedCollectionId = $searchStore.selectedCollectionId;
    filterSelectedLanguage = $searchStore.selectedLanguage;
    filterDurationRange = { ...$searchStore.durationRange };
    filterFileSizeRange = { ...$searchStore.fileSizeRange };
    filterSearchQuery = $searchStore.titleFilter;
    if ($searchStore.dateFrom || $searchStore.dateTo) {
      filterDateRange = {
        from: $searchStore.dateFrom ? new Date($searchStore.dateFrom + 'T00:00:00') : null,
        to: $searchStore.dateTo ? new Date($searchStore.dateTo + 'T00:00:00') : null,
      };
    }

    if (restoredQuery) {
      // D3: Check if we can reuse cached results
      const currentParams = buildSearchParamsString(restoredQuery, urlPage);
      if ($searchStore.lastSearchParams === currentParams && $searchStore.results.length > 0) {
        // Results match - skip API call
        searchInput = restoredQuery;

        // Restore scroll position
        if ($searchStore.scrollPosition > 0) {
          requestAnimationFrame(() => {
            const scrollable = document.querySelector('.scrollable-content');
            if (scrollable) scrollable.scrollTop = $searchStore.scrollPosition;
          });
        }
      } else if ($searchStore.results.length > 0 && $searchStore.query === restoredQuery) {
        // Store has matching results even if params string differs (e.g. back navigation)
        searchInput = restoredQuery;
      } else {
        performSearch(restoredQuery, urlPage);
      }
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

    // Check neural search availability
    axiosInstance.get('/search/models/neural').then((res) => {
      neuralSearchActive = !!(res.data?.neural_enabled && res.data?.active_model_id);
    }).catch(() => {
      neuralSearchActive = false;
    });
  });

  function buildSearchParamsString(query: string, pageNum: number): string {
    return JSON.stringify({
      q: query, page: pageNum, sort: $searchStore.sortBy, sortOrder: $searchStore.sortOrder,
      mode: $searchStore.searchMode, resultType: $searchStore.resultType, speakers: $searchStore.selectedSpeakers,
      tags: $searchStore.selectedTags, dateFrom: $searchStore.dateFrom, dateTo: $searchStore.dateTo,
      fileTypes: $searchStore.selectedFileTypes, collectionId: $searchStore.selectedCollectionId,
      durationRange: $searchStore.durationRange, fileSizeRange: $searchStore.fileSizeRange,
      titleFilter: $searchStore.titleFilter,
    });
  }

  async function performSearch(query: string, pageNum: number = 1) {
    if (!query.trim()) return;

    // Supersede any in-flight search so its response can't land after this one.
    searchController?.abort();
    const controller = new AbortController();
    searchController = controller;

    searchStore.setQuery(query);
    searchStore.setLoading(true);

    // Update URL without navigation
    const params = new URLSearchParams();
    params.set('q', query);
    if (pageNum > 1) params.set('page', String(pageNum));
    if ($searchStore.sortBy !== 'relevance') params.set('sort', $searchStore.sortBy);
    if ($searchStore.sortOrder !== 'desc') params.set('sort_order', $searchStore.sortOrder);
    if ($searchStore.searchMode !== 'hybrid') params.set('mode', $searchStore.searchMode);
    if ($searchStore.resultType !== 'transcripts') params.set('type', $searchStore.resultType);
    $searchStore.selectedSpeakers.forEach((s) => params.append('speakers', s));
    $searchStore.selectedTags.forEach((tag) => params.append('tags', tag));

    goto(`/search?${params.toString()}`, { replaceState: true, keepFocus: true });

    try {
      const apiParams: Record<string, any> = {
        q: query,
        page: pageNum,
        page_size: $searchStore.pageSize,
        sort_by: $searchStore.sortBy,
        sort_order: $searchStore.sortOrder,
        search_mode: $searchStore.searchMode,
        result_type: $searchStore.resultType,
        speakers: $searchStore.selectedSpeakers.length ? $searchStore.selectedSpeakers : undefined,
        tags: $searchStore.selectedTags.length ? $searchStore.selectedTags : undefined,
        date_from: $searchStore.dateFrom || undefined,
        date_to: $searchStore.dateTo || undefined,
      };

      // Gallery filter params
      if ($searchStore.selectedFileTypes.length) {
        apiParams.file_type = $searchStore.selectedFileTypes;
      }
      if ($searchStore.selectedLanguage) {
        apiParams.language = $searchStore.selectedLanguage;
      }

      if ($searchStore.selectedCollectionId) {
        apiParams.collection_id = $searchStore.selectedCollectionId;
      }
      if ($searchStore.durationRange.min !== null) {
        apiParams.min_duration = $searchStore.durationRange.min;
      }
      if ($searchStore.durationRange.max !== null) {
        apiParams.max_duration = $searchStore.durationRange.max;
      }
      if ($searchStore.fileSizeRange.min !== null) {
        apiParams.min_file_size = $searchStore.fileSizeRange.min * 1024 * 1024; // MB to bytes
      }
      if ($searchStore.fileSizeRange.max !== null) {
        apiParams.max_file_size = $searchStore.fileSizeRange.max * 1024 * 1024; // MB to bytes
      }
      if ($searchStore.titleFilter) {
        apiParams.title_filter = $searchStore.titleFilter;
      }

      const res = await axiosInstance.get('/search', {
        params: apiParams,
        signal: controller.signal,
        paramsSerializer: (params) => {
          const searchParams = new URLSearchParams();
          Object.entries(params).forEach(([key, value]) => {
            if (value === undefined) return;
            if (Array.isArray(value)) {
              value.forEach((v) => searchParams.append(key, v));
            } else {
              searchParams.set(key, String(value));
            }
          });
          return searchParams.toString();
        },
      });

      const searchData = res.data as SearchResponse;
      searchStore.setResults(searchData);
      // D3: Store params that produced these results
      searchStore.setLastSearchParams(buildSearchParamsString(query, pageNum));

      // Prefetch next page of results
      const totalPages = Math.ceil((searchData.total_results || 0) / $searchStore.pageSize);
      if (totalPages > pageNum) {
        prefetchNextSearchPage(query, pageNum, totalPages, apiParams);
      }
    } catch (e: unknown) {
      // A superseded search is not a failure: a newer request owns the results
      // and the loading flag, so leave both alone.
      if (isRequestCancelled(e)) return;
      console.error('Search failed:', e);
      searchStore.setError(getErrorMessage(e, $t('search.searchFailed')));
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
    goto(url.toString(), { replaceState: true });
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

  function handleResultTypeChange(resultType: SearchResultType) {
    if (resultType === $searchStore.resultType) return;
    searchStore.setResultType(resultType);
    if ($searchStore.query) {
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

    // Map FilterSidebar event to search store
    if (detail.tags !== undefined) {
      searchStore.setTags(detail.tags);
    }
    if (detail.speaker !== undefined) {
      searchStore.setSpeakers(detail.speaker);
    }
    if (detail.collectionId !== undefined) {
      searchStore.setCollectionId(detail.collectionId);
    }
    if (detail.language !== undefined) {
      searchStore.setLanguage(detail.language);
    }
    if (detail.dates !== undefined) {
      const dateFrom = detail.dates?.from ? detail.dates.from.toISOString().split('T')[0] : '';
      const dateTo = detail.dates?.to ? detail.dates.to.toISOString().split('T')[0] : '';
      searchStore.setDateRange(dateFrom, dateTo);
    }
    if (detail.durationRange !== undefined) {
      searchStore.setDurationRange(detail.durationRange);
    }
    if (detail.fileSizeRange !== undefined) {
      searchStore.setFileSizeRange(detail.fileSizeRange);
    }
    if (detail.fileTypes !== undefined) {
      searchStore.setFileTypes(detail.fileTypes);
    }
    if (detail.statuses !== undefined) {
      searchStore.setStatuses(detail.statuses);
    }
    if (detail.search !== undefined) {
      searchStore.setTitleFilter(detail.search);
    }

    // Re-run search with new filters
    if ($searchStore.query) {
      performSearch($searchStore.query, 1);
    }
  }

  function handleFilterReset() {
    searchStore.setSpeakers([]);
    searchStore.setTags([]);
    searchStore.setDateRange('', '');
    searchStore.setFileTypes([]);
    searchStore.setCollectionId(null);
    searchStore.setLanguage(null);
    searchStore.setDurationRange({ min: null, max: null });
    searchStore.setFileSizeRange({ min: null, max: null });
    searchStore.setStatuses([]);
    searchStore.setTitleFilter('');
    filterSearchQuery = '';
    filterSelectedTags = [];
    filterSelectedSpeakers = [];
    filterDateRange = { from: null, to: null };
    filterSelectedCollectionId = null;
    filterSelectedLanguage = null;
    filterDurationRange = { min: null, max: null };
    filterFileSizeRange = { min: null, max: null };
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

    // Tear down existing preview to force full re-render
    previewData = null;
    await tick();

    // Stop any existing URL refresher
    if (previewUrlRefresher) {
      previewUrlRefresher.stop();
      previewUrlRefresher = null;
    }

    // Fetch presigned URL before rendering the media element
    try {
      clearMediaUrlCache(data.fileUuid);
      previewMediaUrl = await getMediaStreamUrl(data.fileUuid, 'video');

      // Set up automatic URL refresh to prevent 401 on long playback, using the URL's
      // real expiry rather than a hardcoded interval.
      const info = getCachedUrlInfo(data.fileUuid, 'video');
      const expiresIn = info ? Math.max(60, Math.floor((info.expiresAt - Date.now()) / 1000)) : 300;
      previewUrlRefresher = createUrlRefresher(
        data.fileUuid,
        (newUrl) => {
          previewMediaUrl = newUrl;
        },
        expiresIn
      );
    } catch (err) {
      console.error('Failed to get media stream URL:', err);
      return;
    }

    previewData = data;
    activePreview = { fileUuid: data.fileUuid, startTime: data.startTime };
    previewCurrentTime = data.startTime;
    previewCurrentSpeaker = data.speaker || '';
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

  // Check if all results are semantic-only (for informational banner)
  $: allSemanticOnly = $searchStore.results.length > 0 && $searchStore.results.every(r => r.semantic_only);

  onDestroy(() => {
    saveState();
    searchController?.abort();
    searchController = null;
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
      <button
        class="filter-toggle-btn {showFilters ? 'expanded' : 'collapsed'}"
        on:click={() => (showFilters = !showFilters)}
        title={showFilters ? $t('gallery.hideFiltersPanel') : $t('gallery.showFiltersPanel')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line>
          <line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line>
          <line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line>
          <line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line>
          <line x1="17" y1="16" x2="23" y2="16"></line>
        </svg>
      </button>
    </div>

    <!-- Filter Content (hidden when collapsed) -->
    {#if showFilters}
      <div class="filter-content">
        <FilterSidebar
          bind:searchQuery={filterSearchQuery}
          bind:selectedTags={filterSelectedTags}
          bind:selectedSpeakers={filterSelectedSpeakers}
          bind:dateRange={filterDateRange}
          bind:selectedCollectionId={filterSelectedCollectionId}
          bind:selectedLanguage={filterSelectedLanguage}
          bind:durationRange={filterDurationRange}
          bind:fileSizeRange={filterFileSizeRange}
          bind:selectedFileTypes={filterSelectedFileTypes}
          bind:selectedStatuses={filterSelectedStatuses}
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
          <a href="/" class="back-to-gallery" title={$t('nav.backToGallery')}>
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
          <div class="result-type-toggle" role="tablist" aria-label={$t('search.resultTypeToggleLabel')}>
            <button
              type="button"
              class="result-type-btn"
              class:active={$searchStore.resultType === 'transcripts'}
              role="tab"
              aria-selected={$searchStore.resultType === 'transcripts'}
              on:click={() => handleResultTypeChange('transcripts')}
            >
              {$t('search.resultTypeTranscripts')}
            </button>
            <button
              type="button"
              class="result-type-btn"
              class:active={$searchStore.resultType === 'summaries'}
              role="tab"
              aria-selected={$searchStore.resultType === 'summaries'}
              on:click={() => handleResultTypeChange('summaries')}
            >
              {$t('search.resultTypeSummaries')}
            </button>
          </div>
        {/if}

        {#if hasActiveFilters}
          <div class="filter-hint">
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line>
              <line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line>
              <line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line>
              <line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line>
              <line x1="17" y1="16" x2="23" y2="16"></line>
            </svg>
            {$t('search.filtersApplied')}
          </div>
        {/if}

        <!-- Results Info Bar (transcripts only — the mode toggle/neural status/sort
             below are all transcript-specific; summaries have their own count line). -->
        {#if $searchStore.resultType === 'transcripts' && $searchStore.query && !$searchStore.isLoading && $searchStore.totalResults >= 0 && $searchStore.results.length > 0}
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

      <!-- Results -->
      <main class="results">
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
        {:else if $searchStore.query && $searchStore.resultType === 'transcripts' && $searchStore.results.length === 0}
          <div class="state-container">
            <svg xmlns="http://www.w3.org/2000/svg" width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" class="empty-icon">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <p class="state-title">{$t('searchPage.noResults', { query: $searchStore.query })}</p>
            <p class="state-hint">{$t('search.noResultsHint')}</p>
          </div>
        {:else if $searchStore.query && $searchStore.resultType === 'summaries' && $searchStore.summaryResults.length === 0}
          <div class="state-container">
            <svg xmlns="http://www.w3.org/2000/svg" width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" class="empty-icon">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <p class="state-title">{$t('search.noSummaryResults', { query: $searchStore.query })}</p>
            <p class="state-hint">{$t('search.noResultsHint')}</p>
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
        {:else if $searchStore.resultType === 'summaries'}
          <!-- Summary hits are a SIBLING container to .results-list, never nested
               inside it — test_search.py's `.results-list > *` count must stay a
               pure count of transcript hits regardless of which tab is active. -->
          <div class="summary-results-info">
            {$t('search.summariesFound', { count: $searchStore.summaryTotal })}
          </div>
          <div class="summary-results-list">
            {#each $searchStore.summaryResults as hit (hit.file_uuid)}
              <SummaryResultCard {hit} on:openMatch={handleOpenSummaryMatch} />
            {/each}
          </div>

          {#if summaryTotalPages > 1}
            <SearchPagination
              page={$searchStore.page}
              totalPages={summaryTotalPages}
              on:pageChange={handlePageChange}
            />
          {/if}
        {:else}
          <!-- Smart mode only: Exact mode is literal BM25 keyword matching and is
               untouched by the fusion ranking #461 measured. Kept OUTSIDE
               .results-list because test_search.py counts `.results-list > *`. -->
          {#if $searchStore.searchMode === 'hybrid'}
            <div class="quality-notice-slot">
              <RetrievalQualityNotice surface="search" />
            </div>
          {/if}

          <div class="results-list">
              {#if allSemanticOnly}
                <div class="no-keyword-notice">
                  <p>{$t('search.noKeywordMatches')}</p>
                </div>
              {/if}

              {#each $searchStore.results as hit (hit.file_uuid)}
                <SearchResultCard
                  {hit}
                  {activePreview}
                  on:preview={handlePreview}
                  on:viewTranscript={handleViewTranscript}
                />
              {/each}
          </div>

          {#if $searchStore.totalPages > 1}
            <SearchPagination
              page={$searchStore.page}
              totalPages={$searchStore.totalPages}
              on:pageChange={handlePageChange}
            />
          {/if}
        {/if}
      </main>
    </div>
  </div>

  <!-- Sticky Floating Preview Player -->
  {#if previewData}
    <FloatingPreviewPlayer
      title={previewData.title}
      subtitle={previewCurrentSpeaker}
      currentTime={previewCurrentTime}
      jumpToHref="/files/{previewData.fileUuid}?t={previewCurrentTime || previewData.startTime}"
      closeTitle={$t('search.closePreview')}
      closeAriaLabel={$t('search.closePreviewAriaLabel')}
      mediaUrl={previewMediaUrl}
      contentType={previewData.contentType}
      startTime={previewData.startTime}
      fileId={previewData.fileUuid}
      autoplay={true}
      on:close={closePreview}
      on:timeupdate={handlePreviewTimeUpdate}
    />
  {/if}

  <SearchTranscriptModal
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
    height: calc(100vh - var(--content-top, 60px));
    height: calc(100dvh - var(--content-top, 60px));
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
    transition: width 0.3s ease;
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
    padding: 0.5rem 0.5rem 0;
    margin-bottom: 0.5rem;
    flex-shrink: 0;
  }

  .filter-sidebar.show .filter-toggle-container {
    padding: 0.5rem 1rem 0;
  }

  .filter-toggle-btn {
    width: 100%;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.6rem 1rem;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    height: 40px;
    white-space: nowrap;
  }

  .filter-toggle-btn:hover {
    background-color: var(--hover-color);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .filter-toggle-btn:active {
    transform: scale(0.98);
  }

  .filter-toggle-btn svg {
    flex-shrink: 0;
    opacity: 0.8;
  }

  .filter-toggle-btn.collapsed {
    justify-content: center;
    padding: 0.6rem;
    width: auto;
  }

  .filter-content {
    flex: 1;
    overflow-y: auto;
    padding: 0 1rem;
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
    padding: 1.5rem;
  }

  /* Header */
  .search-header {
    margin-bottom: 1.5rem;
  }

  .search-title-row {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    margin-bottom: 1rem;
  }

  .back-to-gallery {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border-radius: 6px;
    color: var(--text-secondary);
    text-decoration: none;
    flex-shrink: 0;
    transition: background 0.15s, color 0.15s;
  }

  .back-to-gallery:hover {
    background: var(--hover-color, rgba(0, 0, 0, 0.05));
    color: var(--text-color);
  }

  .search-title {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-color, #111827);
    margin: 0;
  }

  .search-bar {
    display: flex;
    gap: 0.5rem;
  }

  .filter-hint {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    font-size: 0.75rem;
    color: var(--primary-color, #3b82f6);
    opacity: 0.7;
    margin-top: 0.25rem;
  }

  .search-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 52px;
    height: 44px;
    padding: 0 1rem;
    flex-shrink: 0;
    background-color: var(--primary-color, #3b82f6);
    color: white;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .search-btn:hover:not(:disabled) {
    background-color: var(--primary-color-dark, #2563eb);
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
    gap: 0.75rem;
    margin-top: 1rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border-color, #e5e7eb);
  }

  .result-summary {
    font-size: 0.8125rem;
    color: var(--text-secondary, #6b7280);
  }

  .results-controls {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  /* Search Mode Toggle - sliding pill style */
  .mode-toggle {
    display: flex;
    background: var(--hover-color, #f1f5f9);
    border-radius: 8px;
    padding: 2px;
    gap: 0;
  }

  .mode-btn {
    padding: 0.375rem 0.75rem;
    background: transparent;
    border: none;
    border-radius: 6px;
    color: var(--text-secondary, #6b7280);
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .mode-btn.active {
    background: var(--primary-color, #4f46e5);
    color: white;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
  }

  .mode-btn:hover:not(.active) {
    color: var(--text-color, #374151);
  }

  /* Result type toggle (transcripts vs summaries, issue #462) - same pill style as
     the search-mode toggle above, but result type is orthogonal to it, so it lives
     as its own control near the search bar rather than inside .results-controls
     (which only renders once there are transcript results). */
  .result-type-toggle {
    display: inline-flex;
    background: var(--hover-color, #f1f5f9);
    border-radius: 8px;
    padding: 2px;
    gap: 0;
    margin-top: 0.5rem;
  }

  .result-type-btn {
    padding: 0.375rem 0.875rem;
    background: transparent;
    border: none;
    border-radius: 6px;
    color: var(--text-secondary, #6b7280);
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .result-type-btn.active {
    background: var(--primary-color, #4f46e5);
    color: white;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
  }

  .result-type-btn:hover:not(.active) {
    color: var(--text-color, #374151);
  }

  .result-type-btn:focus-visible {
    outline: 2px solid var(--primary-color, #4f46e5);
    outline-offset: 1px;
  }

  /* Results */
  .results {
    min-height: 300px;
    width: 100%;
  }

  .results-list {
    display: flex;
    flex-direction: column;
  }

  /* Summary results (issue #462) - a SIBLING container to .results-list, never
     nested inside it; see the template comment above .summary-results-list. */
  .summary-results-info {
    font-size: 0.8125rem;
    color: var(--text-secondary, #6b7280);
    margin-bottom: 0.75rem;
  }

  .summary-results-list {
    display: flex;
    flex-direction: column;
  }

  .quality-notice-slot {
    margin-bottom: 12px;
  }

  /* Empty / Loading States */
  .state-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 4rem 2rem;
    text-align: center;
  }

  .state-container.error {
    color: var(--error-color, #ef4444);
  }

  .state-container svg {
    color: var(--text-secondary, #d1d5db);
    margin-bottom: 1rem;
  }

  .state-container.error svg {
    color: var(--error-color, #ef4444);
  }

  .state-title {
    font-size: 1rem;
    font-weight: 500;
    color: var(--text-color, #374151);
    margin: 0 0 0.375rem;
  }

  .state-text {
    font-size: 0.9375rem;
    color: var(--text-secondary, #6b7280);
    margin: 0;
  }

  .state-hint {
    font-size: 0.8125rem;
    color: var(--text-secondary, #9ca3af);
    margin: 0;
  }

  .search-tip {
    margin-top: 0.75rem;
    font-size: 0.75rem;
    font-style: italic;
    opacity: 0.7;
  }

  .empty-icon {
    opacity: 0.35;
  }

  .no-keyword-notice {
    padding: 12px 16px;
    margin-bottom: 12px;
    background: var(--color-warning-bg, #fef3c7);
    color: var(--color-warning-text, #92400e);
    border-radius: 8px;
    font-size: 0.9rem;
  }

  :global(.dark) .no-keyword-notice {
    background: rgba(245, 158, 11, 0.1);
    color: #fbbf24;
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
      top: var(--content-top, 60px);
      left: -100%;
      width: 100% !important;
      min-width: 100% !important;
      height: calc(100vh - var(--content-top, 60px));
      height: calc(100dvh - var(--content-top, 60px));
      background: var(--surface-color);
      z-index: 1000;
      transition: left 0.3s ease;
      border-right: none;
      border-top: 1px solid var(--border-color, #e5e7eb);
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
      padding: 0.75rem;
      overflow-x: hidden;
    }

    .results-info {
      flex-direction: column;
      align-items: flex-start;
    }

    .results-controls {
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .search-title {
      font-size: 1.25rem;
    }

    .back-to-gallery {
      width: 36px;
      height: 36px;
    }

    .results {
      min-width: 0;
    }

    .results-list {
      min-width: 0;
    }
  }

  /* Neural search status indicator */
  .neural-status {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.25rem 0.625rem;
    border-radius: 20px;
    font-size: 0.7rem;
    font-weight: 500;
    border: 1px solid;
    cursor: default;
    white-space: nowrap;
  }

  .neural-status.active {
    background: rgba(16, 185, 129, 0.08);
    border-color: rgba(16, 185, 129, 0.25);
    color: #059669;
  }

  :global(.dark) .neural-status.active {
    background: rgba(16, 185, 129, 0.1);
    border-color: rgba(16, 185, 129, 0.3);
    color: #34d399;
  }

  .neural-status:not(.active) {
    background: rgba(245, 158, 11, 0.08);
    border-color: rgba(245, 158, 11, 0.25);
    color: #d97706;
  }

  :global(.dark) .neural-status:not(.active) {
    background: rgba(245, 158, 11, 0.1);
    border-color: rgba(245, 158, 11, 0.3);
    color: #fbbf24;
  }

  .neural-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
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
