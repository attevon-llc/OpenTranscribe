<script lang="ts">
  import type { TagWithCount } from '$lib/types/tag';
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import RangeSlider from 'svelte-range-slider-pips';
  import '$styles/range-slider-dual.css';
  import DateRangePicker from '$components/ui/DateRangePicker.svelte';
  import FilterChipButton from '$components/ui/FilterChipButton.svelte';
  import {
    latestFacetLoad,
    loadLibraryMetadata,
    loadOwners,
    loadSpeakers,
    loadTags,
    ownerLabel,
    speakerLabel,
    SPEAKER_FILTER_PAGE_SIZE,
    type FilterSpeaker,
    type OwnerOption
  } from '$lib/gallery/filterData';
  import {
    isDurationChipActive as chipActive,
    rangeFromValues,
    toggleItem,
    chipRange,
    visibleDurationChips as visibleChipsFor,
    type DurationChip
  } from '$lib/gallery/filterChips';
  import CollectionsFilter from './CollectionsFilter.svelte';
  import SearchableMultiSelect from './SearchableMultiSelect.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import { DURATION_CHIP_KEYS, labelFor } from '$lib/i18n/keyMaps';
  import { createDebouncedHandler } from '$lib/utils/debounce';
  import { formatClock } from '$lib/utils/formatting';

  // Props
  // The filename/title search lives in the gallery toolbar (`GalleryHeader`)
  // and on /search's own query box, never in this sidebar. The prop stays only
  // so the `filter` event keeps echoing the page's current search text back to
  // the route handler unchanged.
  /** @type {string} */
  export let searchQuery = '';

  /** @type {string[]} */
  export let selectedTags: string[] = [];

  /** @type {string[]} */
  export let selectedSpeakers: string[] = [];

  /** @type {DateRange} */
  export let dateRange: { from: Date | null, to: Date | null } = { from: null, to: null };

  /** @type {string|null} */
  export let selectedCollectionId: string | null = null;
  // Transcript language (#453). Options come from /files/metadata-filters, which
  // now returns the distinct languages of the user's own library — a static list
  // of 100+ WhisperX languages would offer filters that match nothing.
  export let selectedLanguage: string | null = null;

  // Duration range for filtering
  /** @type {{ min: number|null, max: number|null }} */
  export let durationRange: { min: number | null, max: number | null } = {
    min: null,
    max: null
  };

  // Server-provided min/max values for sliders.
  //
  // ⚠️ These are PLACEHOLDERS until `/files/metadata-filters` answers, and the
  // sliders are not rendered before then (#744). A slider drawn against the
  // fabricated 0–3600 range emits a `durationRange` measured in a scale the
  // library does not use: on a library of two-minute files every position of
  // the handle matches every file, which is exactly what "the duration filter
  // does nothing" looked like. The same applied permanently when the request
  // FAILED — the old catch set `metadataLoaded = true` and carried on with the
  // seed, so the control silently described a library that did not exist.
  let durationBounds = { min: 0, max: 3600 };
  let metadataLoaded = false;
  let errorMetadata: string | null = null;

  // Slider values (two-element arrays for dual handles)
  let durationSliderValues: [number, number] = [0, 3600];

  /** @type {string[]} */
  export let selectedFileTypes: string[] = []; // ['audio', 'video']

  /** @type {string[]} */
  export let selectedStatuses: string[] = []; // ['pending', 'processing', 'completed', 'error']

  /** @type {'all' | 'mine' | 'shared'} */
  export let ownershipFilter: 'all' | 'mine' | 'shared' = 'all';

  // Specific-owner filter (issue #966) — orthogonal to `ownershipFilter` above:
  // a searchable multi-select of the individual owners whose files the caller
  // can already see, layered on top of the coarse all/mine/shared bucket
  // rather than replacing it (the backend applies it as an additional
  // predicate — see `resolve_owner_user_ids`/`apply_owner_filter`).
  /** @type {string[]} */
  export let selectedOwners: string[] = []; // Owner UUIDs

  // `GET /search` has no notion of a specific-owner filter (it ranks
  // OpenSearch hits, not `/files` rows), so the search page hides the owner
  // dropdown (the Mine/Shared chips above it stay) — rendering a control that
  // silently does nothing would be worse than not rendering it.
  export let showOwnerFilter = true;

  // State
  /** @type {Tag[]} */
  let allTags: TagWithCount[] = [];
  /** The option shape SearchableMultiSelect expects (its own `Option` type is component-local). */
  type MultiSelectOption = { id: string; name: string; count: number };

  // Reactive: Prepare dropdown tags with proper format
  $: dropdownTags = allTags.map((tag): MultiSelectOption => ({
    id: tag.uuid,
    name: tag.name,
    count: tag.usage_count || 0
  }));

  // Reactive: Convert selected tag names to IDs for multiselect
  $: selectedTagIds = allTags
    .filter(tag => selectedTags.includes(tag.name))
    .map(tag => tag.uuid);

  // Owners the caller can already see (issue #966).
  let allOwners: OwnerOption[] = [];
  let loadingOwners = false;
  let errorOwners: string | null = null;

  $: dropdownOwners = allOwners.map(owner => ({
    id: owner.uuid,
    name: ownerLabel(owner),
  }));

  // Component refs
  let collectionsFilterRef: CollectionsFilter | undefined;

  /** @type {Speaker[]} */
  let allSpeakers: FilterSpeaker[] = [];

  // Named people vs. unlabeled diarization placeholders (#743).
  //
  // The API flags any entry no human has named with `is_unnamed`. A placeholder
  // is scoped to ONE FILE — `SPEAKER_00` in two recordings is two different
  // people — so these must NEVER be rendered as people. They are collapsed into
  // a single "files with unlabeled speakers" facet below, which is a true
  // statement about a file instead of a false one about a person.
  $: namedSpeakers = allSpeakers.filter(speaker => !speaker.is_unnamed);
  $: unlabeledSpeakers = allSpeakers.filter(speaker => speaker.is_unnamed);
  $: unlabeledLabels = unlabeledSpeakers.map(speakerLabel);
  $: unlabeledSelected =
    unlabeledLabels.length > 0 && unlabeledLabels.every(label => selectedSpeakers.includes(label));

  // Reactive: Prepare dropdown speakers with proper format
  $: dropdownSpeakers = namedSpeakers.map((speaker): MultiSelectOption => ({
    id: speaker.uuid,
    name: translateSpeakerLabel(speakerLabel(speaker)),
    count: speaker.media_count || 0
  }));

  // Reactive: Convert selected speaker names to IDs for multiselect
  $: selectedSpeakerIds = namedSpeakers
    .filter(speaker => selectedSpeakers.includes(speakerLabel(speaker)))
    .map(speaker => speaker.uuid);

  let loadingTags = false;

  let loadingSpeakers = false;

  let errorTags: string | null = null;

  let errorSpeakers: string | null = null;

  // Available options for filters
  /** @type {string[]} */
  const availableFileTypes = ['audio', 'video'];
  /** @type {string[]} */
  const availableStatuses = ['pending', 'processing', 'completed', 'error'];

  // Event dispatcher
  const dispatch = createEventDispatcher();

  // Debounce infrastructure for auto-triggering filters
  const DEBOUNCE_DELAY = 400;
  let isInitialized = false;
  const debouncedApply = createDebouncedHandler(() => applyFilters(), DEBOUNCE_DELAY);

  // Previous values for reactive change detection
  let prevCollectionId = selectedCollectionId;
  let prevLanguage = selectedLanguage;

  function triggerFiltersImmediate() {
    debouncedApply.cleanup();
    applyFilters();
  }

  function triggerFiltersDebounced() {
    debouncedApply.trigger();
  }

  onDestroy(() => {
    debouncedApply.cleanup();
  });

  // Reactive watcher for collection selection (immediate)
  // Immediate, not debounced: a select is a discrete choice, like the collection
  // filter beside it. Debouncing is for the free-text box.
  function applyLanguageChange(language: string | null, initialized: boolean) {
    if (!initialized || language === prevLanguage) return;
    prevLanguage = language;
    triggerFiltersImmediate();
  }

  function applyCollectionChange(collectionId: string | null, initialized: boolean) {
    if (!initialized || collectionId === prevCollectionId) return;
    prevCollectionId = collectionId;
    triggerFiltersImmediate();
  }

  $: applyLanguageChange(selectedLanguage, isInitialized);
  $: applyCollectionChange(selectedCollectionId, isInitialized);

  // Each facet keeps only its newest load's outcome (`latestFacetLoad`): a superseded load
  // leaves the section to the newer one, and a cancelled one is not an error.
  const tagsLoad = latestFacetLoad(loadTags);
  const ownersLoad = latestFacetLoad(loadOwners);
  const speakersLoad = latestFacetLoad(loadSpeakers);
  const metadataLoad = latestFacetLoad(loadLibraryMetadata);

  async function fetchTags() {
    loadingTags = true;
    errorTags = null;
    const outcome = await tagsLoad();
    if (outcome.kind === 'superseded') return;
    loadingTags = false;
    if (outcome.kind === 'ok') {
      allTags = outcome.value;
    } else if (outcome.kind === 'error') {
      console.error('[FilterSidebar] Error fetching tags:', outcome.cause);
      allTags = [];
      errorTags = outcome.message;
    }
  }

  async function fetchOwners() {
    if (!showOwnerFilter) return;
    loadingOwners = true;
    errorOwners = null;
    const outcome = await ownersLoad();
    if (outcome.kind === 'superseded') return;
    loadingOwners = false;
    if (outcome.kind === 'ok') {
      allOwners = outcome.value;
    } else if (outcome.kind === 'error') {
      console.error('[FilterSidebar] Error fetching owners:', outcome.cause);
      allOwners = [];
      errorOwners = outcome.message;
    }
  }

  let speakersTruncated = false;

  async function fetchSpeakers(q = '') {
    loadingSpeakers = true;
    errorSpeakers = null;
    const outcome = await speakersLoad(q);
    if (outcome.kind === 'superseded') return;
    loadingSpeakers = false;
    if (outcome.kind === 'ok') {
      speakersTruncated = outcome.value.truncated;
      allSpeakers = outcome.value.speakers;
    } else if (outcome.kind === 'error') {
      console.error('Error fetching speakers:', outcome.cause);
      allSpeakers = [];
      speakersTruncated = false;
      errorSpeakers = outcome.message;
    }
  }

  /** Debounced so typing a speaker name doesn't fire one request per keystroke. */
  const SPEAKER_SEARCH_DEBOUNCE_MS = 300;
  let speakerSearchQuery = '';
  let searchingSpeakers = false;
  const debouncedSpeakerSearch = createDebouncedHandler(async () => {
    await fetchSpeakers(speakerSearchQuery);
    searchingSpeakers = false;
  }, SPEAKER_SEARCH_DEBOUNCE_MS);

  function scheduleSpeakerSearch() {
    searchingSpeakers = true;
    debouncedSpeakerSearch.trigger();
  }

  onDestroy(() => debouncedSpeakerSearch.cleanup());

  function toggleTag(tag: string) {
    selectedTags = toggleItem(selectedTags, tag);
    triggerFiltersImmediate();
  }

  /**
   * Handle tag selection from multiselect dropdown
   * @param {CustomEvent} event - Event with tag id
   */
  function handleTagSelect(event: CustomEvent) {
    const tagId = event.detail.id;
    const tag = allTags.find(t => t.uuid === tagId);
    if (tag && !selectedTags.includes(tag.name)) {
      selectedTags = [...selectedTags, tag.name];
      triggerFiltersImmediate();
    }
  }

  /**
   * Handle tag deselection from multiselect dropdown
   * @param {CustomEvent} event - Event with tag id
   */
  function handleTagDeselect(event: CustomEvent) {
    const tagId = event.detail.id;
    const tag = allTags.find(t => t.uuid === tagId);
    if (tag) {
      selectedTags = selectedTags.filter(t => t !== tag.name);
      triggerFiltersImmediate();
    }
  }

  /**
   * Handle owner selection from the multiselect dropdown (issue #966).
   * @param {CustomEvent} event - Event with owner uuid
   */
  function handleOwnerSelect(event: CustomEvent) {
    const ownerUuid = event.detail.id;
    if (!selectedOwners.includes(ownerUuid)) {
      selectedOwners = [...selectedOwners, ownerUuid];
      triggerFiltersImmediate();
    }
  }

  /**
   * Handle owner deselection from the multiselect dropdown (issue #966).
   * @param {CustomEvent} event - Event with owner uuid
   */
  function handleOwnerDeselect(event: CustomEvent) {
    const ownerUuid = event.detail.id;
    selectedOwners = selectedOwners.filter(o => o !== ownerUuid);
    triggerFiltersImmediate();
  }

  function toggleSpeaker(speaker: string) {
    selectedSpeakers = toggleItem(selectedSpeakers, speaker);
    triggerFiltersImmediate();
  }

  /**
   * Toggle the "files with unlabeled speakers" facet (#743).
   *
   * Selects (or clears) EVERY unlabeled diarization label at once. The server
   * ORs the `speaker` params, so this resolves to "files that still contain a
   * speaker nobody has named" — the review question a user actually has —
   * without ever claiming that `SPEAKER_00` is one person across files.
   */
  function toggleUnlabeledSpeakers() {
    if (unlabeledSelected) {
      selectedSpeakers = selectedSpeakers.filter(s => !unlabeledLabels.includes(s));
    } else {
      selectedSpeakers = [
        ...selectedSpeakers,
        ...unlabeledLabels.filter(label => !selectedSpeakers.includes(label))
      ];
    }
    triggerFiltersImmediate();
  }

  /**
   * Handle speaker selection from multiselect dropdown
   * @param {CustomEvent} event - Event with speaker id
   */
  function handleSpeakerSelect(event: CustomEvent) {
    const speakerId = event.detail.id;
    const speaker = allSpeakers.find(s => s.uuid === speakerId);
    if (speaker) {
      const speakerName = speaker.display_name || speaker.name;
      if (!selectedSpeakers.includes(speakerName)) {
        selectedSpeakers = [...selectedSpeakers, speakerName];
        triggerFiltersImmediate();
      }
    }
  }

  /**
   * Handle speaker deselection from multiselect dropdown
   * @param {CustomEvent} event - Event with speaker id
   */
  function handleSpeakerDeselect(event: CustomEvent) {
    const speakerId = event.detail.id;
    const speaker = allSpeakers.find(s => s.uuid === speakerId);
    if (speaker) {
      const speakerName = speaker.display_name || speaker.name;
      selectedSpeakers = selectedSpeakers.filter(s => s !== speakerName);
      triggerFiltersImmediate();
    }
  }

  /** The shared picker owns its own open/closing state and date formatting. */
  function handleDateRangeChange(event: CustomEvent<{ from: Date | null; to: Date | null }>) {
    dateRange = { from: event.detail.from, to: event.detail.to };
    triggerFiltersImmediate();
  }

  /**
   * Clear date range filter
   */
  function clearDateRange() {
    // `DateRangePicker` resets its own plugin state when both ends go null.
    dateRange = { from: null, to: null };
    triggerFiltersImmediate();
  }

  let availableLanguages: string[] = [];

  async function fetchMediaMetadata() {
    errorMetadata = null;
    const outcome = await metadataLoad();
    if (outcome.kind === 'ok') {
      const data = outcome.value;
      if (data.durationBounds) {
        durationBounds = data.durationBounds;
        // Only reset slider if user hasn't set a filter
        if (durationRange.min === null && durationRange.max === null) {
          durationSliderValues = [durationBounds.min, durationBounds.max];
        }
      }
      availableLanguages = data.languages;
      metadataLoaded = true;
    } else if (outcome.kind === 'error') {
      console.error('Error fetching media metadata:', outcome.cause);
      // Deliberately NOT `metadataLoaded = true` (#744): sliders drawn against the seed bounds
      // silently match every file. Show the failure and offer a retry instead.
      metadataLoaded = false;
      errorMetadata = outcome.message;
    }
  }

  function handleDurationSliderChange(e: CustomEvent<{ values: number[] }>) {
    const [min, max] = e.detail.values;
    if (min === undefined || max === undefined) return;
    durationRange = rangeFromValues(min, max, durationBounds);
    triggerFiltersDebounced();
  }

  $: visibleDurationChips = visibleChipsFor(durationBounds);

  const isDurationChipActive = (chip: DurationChip): boolean =>
    chipActive(chip, durationBounds, durationRange);

  function selectDurationChip(chip: DurationChip) {
    const [min, max] = chipRange(chip, durationBounds);
    durationSliderValues = [min, max];
    durationRange = rangeFromValues(min, max, durationBounds);
    // Discrete choice, unlike the debounced free-drag slider.
    triggerFiltersImmediate();
  }

  /**
   * Handle ownership filter change
   */
  function setOwnershipFilter(value: 'all' | 'mine' | 'shared') {
    ownershipFilter = value;
    triggerFiltersImmediate();
  }

  function toggleFileType(fileType: string) {
    selectedFileTypes = toggleItem(selectedFileTypes, fileType);
    triggerFiltersImmediate();
  }

  function toggleStatus(status: string) {
    selectedStatuses = toggleItem(selectedStatuses, status);
    triggerFiltersImmediate();
  }

  // Apply filters
  function applyFilters() {
    dispatch('filter', {
      search: searchQuery,
      tags: selectedTags,
      speaker: selectedSpeakers,
      collectionId: selectedCollectionId,
      language: selectedLanguage,
      dates: dateRange,
      durationRange,
      fileTypes: selectedFileTypes,
      statuses: selectedStatuses,
      ownership: ownershipFilter,
      owners: selectedOwners,
    });
  }

  // Reset filters
  function resetFilters() {
    // Temporarily disable reactive watchers to prevent intermediate triggers
    isInitialized = false;

    searchQuery = '';
    selectedTags = [];
    selectedSpeakers = [];
    selectedCollectionId = null;
    selectedLanguage = null;
    dateRange = { from: null, to: null };
    durationRange = { min: null, max: null };
    selectedFileTypes = [];
    selectedStatuses = [];
    ownershipFilter = 'all';
    selectedOwners = [];

    // Reset sliders to full bounds
    durationSliderValues = [durationBounds.min, durationBounds.max];

    // Sync prev values so watchers don't fire on re-enable
    prevCollectionId = null;

    // Clear any pending debounce
    debouncedApply.cleanup();

    dispatch('reset');

    // Re-enable reactive watchers after the current tick
    setTimeout(() => {
      isInitialized = true;
    }, 0);
  }

  // Public method to refresh collections
  export function refreshCollections() {
    if (collectionsFilterRef && collectionsFilterRef.fetchCollections) {
      collectionsFilterRef.fetchCollections();
    }
  }

  // Push-based cache invalidation listener
  function handleCacheInvalidation(event: Event) {
    const scope = (event as CustomEvent).detail?.scope;
    if (scope === 'tags' || scope === 'all') void fetchTags();
    // Re-fetch under whatever the user is currently typing, not the unfiltered
    // default — otherwise a push-invalidation while searching would silently
    // discard the in-progress search and repopulate the full list underneath it.
    if (scope === 'speakers' || scope === 'all') void fetchSpeakers(speakerSearchQuery);
    if (scope === 'metadata' || scope === 'files' || scope === 'all') void fetchMediaMetadata();
    // Owner visibility changes with the files a caller can see (a new upload)
    // and with collection sharing — both scopes can add or remove an owner.
    if (scope === 'files' || scope === 'collections' || scope === 'all') void fetchOwners();
  }

  onMount(() => {
    void fetchTags();
    void fetchOwners();
    void fetchSpeakers();
    void fetchMediaMetadata();

    // Listen for push-based cache invalidation from WebSocket
    window.addEventListener('cache-invalidated', handleCacheInvalidation);

    // Restore slider positions from filter props
    if (durationRange.min !== null || durationRange.max !== null) {
      durationSliderValues = [
        durationRange.min ?? durationBounds.min,
        durationRange.max ?? durationBounds.max,
      ];
    }

    // Sync prev values and enable reactive watchers after mount
    prevCollectionId = selectedCollectionId;
    setTimeout(() => {
      isInitialized = true;
    }, 0);

    return () => {
      window.removeEventListener('cache-invalidated', handleCacheInvalidation);
    };
  });
</script>

<div class="filter-sidebar">
  <div class="filter-header">
    <h2>{$t('filter.title')}</h2>
    <div class="header-buttons">
      <button
        class="reset-button"
        on:click={resetFilters}
        title={$t('filter.resetTooltip')}
      >{$t('filter.reset')}</button>
    </div>
  </div>

  <!-- Section order is settled (#750 item 26, owner decision 2026-10-11): the
       sections that mirror a list-view column come first in that column's
       order (Type, Speakers, Duration, Date, Status), then the sections with
       no column (Tags, Collections, Language), then Ownership last. The
       file-size filter was removed on purpose; size is a proxy for duration. -->

  <!-- File Type -->
  <div class="filter-section">
    <h3>{$t('filter.fileType')}</h3>
    <div class="file-type-list">
      {#each availableFileTypes as fileType (fileType)}
        <FilterChipButton
          class="file-type-button"
          selected={selectedFileTypes.includes(fileType)}
          on:click={() => toggleFileType(fileType)}
          title={$t('filter.fileTypeTooltip', { type: fileType })}
        >
          <svelte:fragment slot="icon">
            {#if fileType === 'video'}
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <polygon points="23 7 16 12 23 17 23 7"></polygon>
                <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
              </svg>
            {:else if fileType === 'audio'}
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                <line x1="12" y1="19" x2="12" y2="23"></line>
                <line x1="8" y1="23" x2="16" y2="23"></line>
              </svg>
            {/if}
          </svelte:fragment>
          {fileType === 'audio' ? $t('common.audio') : $t('common.video')}
        </FilterChipButton>
      {/each}
    </div>
  </div>

  <div class="filter-section">
    <h3>{$t('filter.speakers')}</h3>
    <!-- No standalone search input here. The dropdown below already owns a
         search box, and its `on:search` drives the same server-side fetch, so a
         second input beside it was pure redundancy: two search fields plus the
         quick chips, all filtering one list. -->
    {#if loadingSpeakers && !searchingSpeakers}
      <p class="loading-text">{$t('filter.loadingSpeakers')}</p>
    {:else if errorSpeakers}
      <EmptyState
        icon="warning"
        title={$t('filter.speakersLoadFailed')}
        description={$t('filter.facetLoadFailedHelp')}
        padding="12px 0"
      >
        <button
          class="retry-button"
          data-testid="speakers-retry"
          on:click={() => fetchSpeakers(speakerSearchQuery)}
        >{$t('filter.retry')}</button>
      </EmptyState>
    {:else}
      {#if namedSpeakers.length === 0}
        <p class="empty-text">
          {#if speakerSearchQuery.trim()}
            {$t('filter.noSpeakersMatchSearch')}
          {:else if unlabeledSpeakers.length > 0}
            {$t('filter.noNamedSpeakers')}
          {:else}
            {$t('filter.noSpeakersDetected')}
          {/if}
        </p>
      {:else}
        <div class="speakers-list">
          {#each namedSpeakers.slice(0, 4) as speaker, i (i)}
            <FilterChipButton
              class="speaker-button"
              selected={selectedSpeakers.includes(speakerLabel(speaker))}
              count={speaker.media_count || null}
              on:click={() => toggleSpeaker(speakerLabel(speaker))}
              title={$t('filter.speakerTooltip', { speaker: translateSpeakerLabel(speakerLabel(speaker)), count: speaker.media_count ? $t('filter.speakerAppearsInFiles', { count: speaker.media_count }) : '' })}
            >
              {translateSpeakerLabel(speakerLabel(speaker))}
            </FilterChipButton>
          {/each}
        </div>
        <div class="dropdown-section">
          <SearchableMultiSelect
            options={dropdownSpeakers}
            selectedIds={selectedSpeakerIds}
            placeholder={$t('filter.selectSpeakersPlaceholder')}
            maxHeight="300px"
            showCounts={true}
            on:select={handleSpeakerSelect}
            on:deselect={handleSpeakerDeselect}
            on:search={(e) => {
              speakerSearchQuery = e.detail.term;
              scheduleSpeakerSearch();
            }}
          />
          {#if searchingSpeakers}
            <div class="speaker-search-status"><Spinner label={$t('common.loading')} size="small" /></div>
          {/if}
        </div>
      {/if}
      <!-- Unlabeled speakers: ONE facet, never a list of pseudo-people (#743). -->
      {#if unlabeledSpeakers.length > 0}
        <div class="speakers-list">
          <FilterChipButton
            class="speaker-button"
            selected={unlabeledSelected}
            data-testid="unlabeled-speakers-facet"
            on:click={toggleUnlabeledSpeakers}
            title={$t('filter.unlabeledSpeakersTooltip')}
          >
            {$t('filter.unlabeledSpeakers')}
          </FilterChipButton>
        </div>
        <small class="input-help">{$t('filter.unlabeledSpeakersHelp')}</small>
      {/if}
      {#if speakersTruncated}
        <small class="input-help" data-testid="speakers-truncated">
          {$t('filter.speakersTruncated', { count: SPEAKER_FILTER_PAGE_SIZE })}
        </small>
      {/if}
    {/if}
  </div>

  <!-- Duration range + quick chips.
       Gated on `metadataLoaded` (#744): a range control whose bounds are the
       hardcoded seed rather than the library's real min/max emits a filter
       every file satisfies, which reads as "the slider does nothing". The
       quick chips (#750 item 8) share the exact same null-at-bounds
       conversion as the slider so the two controls never disagree about what
       is filtered. -->
  {#if errorMetadata}
    <div class="filter-section">
      <h3>{$t('filter.duration')}</h3>
      <EmptyState
        icon="warning"
        title={$t('filter.rangesLoadFailed')}
        description={$t('filter.rangesLoadFailedHelp')}
        padding="12px 0"
      >
        <button class="retry-button" data-testid="metadata-retry" on:click={fetchMediaMetadata}
          >{$t('filter.retry')}</button>
      </EmptyState>
    </div>
  {:else if !metadataLoaded}
    <div class="filter-section">
      <h3>{$t('filter.duration')}</h3>
      <p class="loading-text">{$t('filter.loadingRanges')}</p>
    </div>
  {:else}
    <div class="filter-section">
      <h3>{$t('filter.duration')}</h3>
      {#if visibleDurationChips.length > 0}
        <div class="duration-chip-list">
          {#each visibleDurationChips as chip (chip.code)}
            <FilterChipButton
              selected={isDurationChipActive(chip)}
              on:click={() => selectDurationChip(chip)}
            >
              {labelFor($t, DURATION_CHIP_KEYS, chip.code)}
            </FilterChipButton>
          {/each}
        </div>
      {/if}
      <div class="slider-labels">
        <span>{formatClock(durationSliderValues[0])}</span>
        <span>{formatClock(durationSliderValues[1])}</span>
      </div>
      <div class="slider-wrapper slider-dual">
        <RangeSlider
          bind:values={durationSliderValues}
          min={durationBounds.min}
          max={durationBounds.max}
          step={durationBounds.max > 7200 ? 60 : durationBounds.max > 600 ? 30 : 10}
          range
          pushy
          on:change={handleDurationSliderChange}
        />
      </div>
    </div>
  {/if}

  <div class="filter-section">
    <div class="section-header-row">
      <h3>{$t('filter.dateRange')}</h3>
      {#if dateRange.from || dateRange.to}
        <button
          class="clear-inline-btn"
          on:click|stopPropagation={clearDateRange}
          title={$t('filter.clearDates')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      {/if}
    </div>
    <DateRangePicker
      from={dateRange.from}
      to={dateRange.to}
      on:change={handleDateRangeChange}
    />
  </div>

  <!-- Processing Status -->
  <div class="filter-section">
    <h3>{$t('filter.processingStatus')}</h3>
    <div class="status-list">
      {#each availableStatuses as status (status)}
        <FilterChipButton
          class="status-button"
          selected={selectedStatuses.includes(status)}
          on:click={() => toggleStatus(status)}
          title={$t('filter.statusTooltip', { status })}
        >
          {status === 'pending' ? $t('common.pending') : status === 'processing' ? $t('common.processing') : status === 'completed' ? $t('common.completed') : status === 'error' ? $t('common.error') : status.charAt(0).toUpperCase() + status.slice(1)}
        </FilterChipButton>
      {/each}
    </div>
  </div>

  <div class="filter-section">
    <h3>{$t('filter.tags')}</h3>
    <!-- A page's own pseudo-tags, shown whatever the tag list's state (the gallery's computed
         "Legal hold" chip). Search passes nothing. -->
    <slot name="tags-leading" />
    {#if loadingTags}
      <p class="loading-text">{$t('filter.loadingTags')}</p>
    {:else if errorTags}
      <EmptyState
        icon="warning"
        title={$t('filter.tagsLoadFailed')}
        description={$t('filter.facetLoadFailedHelp')}
        padding="12px 0"
      >
        <button class="retry-button" data-testid="tags-retry" on:click={fetchTags}
          >{$t('filter.retry')}</button>
      </EmptyState>
    {:else if allTags.length === 0}
      <p class="empty-text">{$t('filter.noTagsCreated')}</p>
    {:else}
      <div class="tags-list">
        {#each allTags.slice(0, 6) as tag (tag.name)}
          <FilterChipButton
            class="tag-button"
            selected={selectedTags.includes(tag.name)}
            count={tag.usage_count || null}
            on:click={() => toggleTag(tag.name)}
            title={$t('filter.tagTooltip', { tag: tag.name, count: tag.usage_count ? $t('filter.tagUsedInFiles', { count: tag.usage_count }) : '' })}
          >
            {tag.name}
          </FilterChipButton>
        {/each}
      </div>
      {#if allTags.length > 0}
        <div class="dropdown-section">
          <SearchableMultiSelect
            options={dropdownTags}
            selectedIds={selectedTagIds}
            placeholder={$t('filter.selectTagsPlaceholder')}
            maxHeight="300px"
            showCounts={true}
            on:select={handleTagSelect}
            on:deselect={handleTagDeselect}
          />
        </div>
      {/if}
    {/if}
  </div>

  <div class="filter-section">
    <h3>{$t('filter.collections')}</h3>
    <CollectionsFilter bind:selectedCollectionId={selectedCollectionId} bind:this={collectionsFilterRef} />
  </div>

  <!-- Transcript language (#453). Rendered only when the library actually holds more
       than one, so a single-language user never sees a filter that can do nothing. -->
  {#if availableLanguages.length > 1}
    <div class="filter-section">
      <h3>{$t('filter.language')}</h3>
      <div class="file-type-list">
        {#each availableLanguages as lang (lang)}
          <FilterChipButton
            class="file-type-button"
            selected={selectedLanguage === lang}
            on:click={() => (selectedLanguage = selectedLanguage === lang ? null : lang)}
            title={$t('filter.languageTooltip', { language: lang })}
          >
            {lang.toUpperCase()}
          </FilterChipButton>
        {/each}
      </div>
    </div>
  {/if}

  <div class="filter-section">
    <h3>{$t('filter.ownership')}</h3>
    <div class="ownership-list">
      <FilterChipButton
        class="ownership-button"
        selected={ownershipFilter === 'all'}
        on:click={() => setOwnershipFilter('all')}
      >
        {$t('filter.allFiles')}
      </FilterChipButton>
      <FilterChipButton
        class="ownership-button"
        selected={ownershipFilter === 'mine'}
        on:click={() => setOwnershipFilter('mine')}
      >
        {$t('filter.myFiles')}
      </FilterChipButton>
      <FilterChipButton
        class="ownership-button"
        selected={ownershipFilter === 'shared'}
        on:click={() => setOwnershipFilter('shared')}
      >
        {$t('filter.sharedWithMe')}
      </FilterChipButton>
    </div>
    {#if showOwnerFilter}
      {#if loadingOwners}
        <p class="loading-text">{$t('filter.loadingOwners')}</p>
      {:else if errorOwners}
        <EmptyState
          icon="warning"
          title={$t('filter.ownersLoadFailed')}
          description={$t('filter.facetLoadFailedHelp')}
          padding="12px 0"
        >
          <button class="retry-button" data-testid="owners-retry" on:click={fetchOwners}
            >{$t('filter.retry')}</button>
        </EmptyState>
      {:else if allOwners.length === 0}
        <p class="empty-text">{$t('filter.noOwners')}</p>
      {:else}
        <div class="dropdown-section">
          <SearchableMultiSelect
            options={dropdownOwners}
            selectedIds={selectedOwners}
            placeholder={$t('filter.selectOwnersPlaceholder')}
            maxHeight="300px"
            on:select={handleOwnerSelect}
            on:deselect={handleOwnerDeselect}
          />
        </div>
      {/if}
    {/if}
  </div>

  <!-- Page-specific sections after the shared facets (the gallery's hidden-files chips). -->
  <slot />
</div>

<style>
  .filter-sidebar {
    background-color: var(--surface-color);
    border-radius: var(--radius-lg);
    padding: var(--space-3);
    box-shadow: var(--shadow-sm);
    display: flex;
    flex-direction: column;
    /* #750 items 3+4: more breathing room between sections, and a thinner,
       fully-opaque divider reads as more structure than the old thick faded
       one at less distance. */
    gap: var(--space-6);
  }

  .filter-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--space-2);
  }

  .filter-header h2 {
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-semibold);
    margin: 0;
  }

  .header-buttons {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }

  .reset-button {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    padding: 0.3rem 0.7rem;
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-regular);
    cursor: pointer;
    transition: all var(--duration-base) ease;
  }

  .reset-button:hover:not(:disabled) {
    background-color: var(--hover-color);
    border-color: var(--primary-color);
  }

  .reset-button:active:not(:disabled) {
    transform: scale(1);
  }

  .filter-section {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    position: relative;
  }

  .filter-section:not(:first-child) {
    padding-top: var(--space-1);
  }

  .filter-section:not(:first-child)::before {
    content: '';
    position: absolute;
    top: -0.75rem;
    left: 0;
    right: 0;
    height: 1px;
    background: var(--border-color);
    opacity: 1;
  }

  .filter-section h3 {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    margin: 0;
  }

  /* Section header with inline clear button */
  .section-header-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .clear-inline-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    padding: 0;
    border: none;
    border-radius: var(--radius-round);
    background-color: var(--background-color);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--duration-base) ease;
  }

  .clear-inline-btn:hover {
    background-color: var(--hover-color);
    color: var(--text-color);
  }

  .dropdown-section {
    margin-top: var(--space-3);
  }

  .loading-text,
  .empty-text {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin: 0;
  }

  /* The range slider library has no RTL mode (its handles are placed from the left edge), so
     the scale and its min/max labels read left to right in every language, like the player's
     sliders. */
  .slider-labels {
    direction: ltr;
    display: flex;
    justify-content: space-between;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-bottom: var(--space-1);
    font-variant-numeric: tabular-nums;
  }

  .slider-wrapper {
    direction: ltr;
    padding: 0 var(--space-1);
    font-size: var(--font-size-xs);
  }

  /* Status row for the dropdown's server-side speaker search. */
  .speaker-search-status {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.35rem 0;
  }

  /* Retry control for a facet whose fetch failed — an empty facet must never
     be the way an outage looks (#743a). Global inside the sidebar so the child
     facets (CollectionsFilter) render the same control. */
  .filter-sidebar :global(.retry-button) {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    color: var(--text-color);
    padding: 0.3rem 0.8rem;
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all var(--duration-base) ease;
  }

  .filter-sidebar :global(.retry-button:hover) {
    background-color: var(--hover-color);
    border-color: var(--primary-color);
  }

  /* Chip rows. Per-chip visual styling (background, border, selected state)
     now lives entirely in `ui/FilterChipButton.svelte` (#750) — these five
     lists used to each carry their own copy of the same button CSS. */
  .tags-list,
  .speakers-list,
  .file-type-list,
  .status-list,
  .ownership-list,
  .duration-chip-list {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  .duration-chip-list {
    margin-top: 0;
    margin-bottom: var(--space-2);
  }

  /* Input help text */
  .input-help {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-top: var(--space-1);
    display: block;
    font-style: italic;
  }

</style>
