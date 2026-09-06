<script lang="ts">
  import type { Tag, TagWithCount } from '$lib/types/tag';
  import { createEventDispatcher, onMount, onDestroy, tick } from 'svelte';
  import RangeSlider from 'svelte-range-slider-pips';
  import { DatePicker } from '@svelte-plugins/datepicker';
  import { format } from 'date-fns';
  import axiosInstance from '../lib/axios';
  import { listTags } from '$lib/api/tags';
  import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
  import CollectionsFilter from './CollectionsFilter.svelte';
  import SearchableMultiSelect from './SearchableMultiSelect.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import { createDebouncedHandler } from '$lib/utils/debounce';
  import { formatClock } from '$lib/utils/formatting';

  // Type definitions for props and state
  /**
   * @typedef {Object} Tag
   * @property {number} id - Tag ID
   * @property {string} name - Tag name
   */

  /**
   * @typedef {Object} Speaker
   * @property {number} id - Speaker ID
   * @property {string} name - Speaker name (original name like SPEAKER_01)
   * @property {string|null} display_name - Display name set by user
   */

  /**
   * @typedef {Object} DateRange
   * @property {Date|null} from - Start date
   * @property {Date|null} to - End date
   */

  /**
   * @typedef {Object} DurationRange
   * @property {number|null} min - Minimum duration in seconds
   * @property {number|null} max - Maximum duration in seconds
   */

  /**
   * @typedef {Object} ResolutionRange
   * @property {number|null} minWidth - Minimum width in pixels
   * @property {number|null} maxWidth - Maximum width in pixels
   * @property {number|null} minHeight - Minimum height in pixels
   * @property {number|null} maxHeight - Maximum height in pixels
   */

  // Props
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
  let fileSizeBounds = { min: 0, max: 1024 }; // in MB
  let metadataLoaded = false;
  let errorMetadata: string | null = null;

  // Slider values (two-element arrays for dual handles)
  let durationSliderValues: [number, number] = [0, 3600];
  let fileSizeSliderValues: [number, number] = [0, 1024];

  // File size range for filtering (in MB)
  /** @type {{ min: number|null, max: number|null }} */
  export let fileSizeRange: { min: number | null, max: number | null } = {
    min: null,
    max: null
  };

  /** @type {string[]} */
  export let selectedFileTypes: string[] = []; // ['audio', 'video']

  /** @type {string[]} */
  export let selectedStatuses: string[] = []; // ['pending', 'processing', 'completed', 'error']

  /** @type {'all' | 'mine' | 'shared'} */
  export let ownershipFilter: 'all' | 'mine' | 'shared' = 'all';

  // State
  /** @type {Tag[]} */
  let allTags: TagWithCount[] = [];
  let showAllTags = false;  // Toggle for showing all tags vs top 9
  /** The option shape SearchableMultiSelect expects (its own `Option` type is component-local). */
  type MultiSelectOption = { id: string; name: string; count: number };
  let dropdownTags: MultiSelectOption[] = [];  // All tags for multiselect dropdown

  // Reactive: Prepare dropdown tags with proper format
  $: dropdownTags = allTags.map(tag => ({
    id: tag.uuid,
    name: tag.name,
    count: tag.usage_count || 0
  }));

  // Reactive: Convert selected tag names to IDs for multiselect
  $: selectedTagIds = allTags
    .filter(tag => selectedTags.includes(tag.name))
    .map(tag => tag.uuid);

  // Component refs
  let collectionsFilterRef: any;

  /** @type {Speaker[]} */
  let allSpeakers: any[] = [];
  let dropdownSpeakers: any[] = [];  // Named speakers for multiselect dropdown

  /** The string the API offers a speaker under, and the value sent back as `?speaker=`. */
  const speakerLabel = (speaker: any): string => speaker.display_name || speaker.name;

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
  $: dropdownSpeakers = namedSpeakers.map(speaker => ({
    id: speaker.uuid,
    name: translateSpeakerLabel(speakerLabel(speaker)),
    count: speaker.media_count || 0
  }));

  // Reactive: Convert selected speaker names to IDs for multiselect
  $: selectedSpeakerIds = namedSpeakers
    .filter(speaker => selectedSpeakers.includes(speakerLabel(speaker)))
    .map(speaker => speaker.uuid);

  /** @type {boolean} */
  let loadingTags = false;

  /** @type {boolean} */
  let loadingSpeakers = false;

  /** @type {string|null} */
  let errorTags: string | null = null;

  /** @type {string|null} */
  let errorSpeakers: string | null = null;

  // Available options for filters
  /** @type {string[]} */
  let availableFileTypes = ['audio', 'video'];
  /** @type {string[]} */
  let availableStatuses = ['pending', 'processing', 'completed', 'error'];

  // Event dispatcher
  const dispatch = createEventDispatcher();

  // Debounce infrastructure for auto-triggering filters
  const DEBOUNCE_DELAY = 400;
  let isInitialized = false;
  const debouncedApply = createDebouncedHandler(() => applyFilters(), DEBOUNCE_DELAY);

  // Previous values for reactive change detection
  let prevSearchQuery = searchQuery;
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

  // Reactive watchers for text inputs (debounced)
  $: if (isInitialized && searchQuery !== prevSearchQuery) {
    prevSearchQuery = searchQuery;
    triggerFiltersDebounced();
  }

  // Reactive watcher for collection selection (immediate)
  $: if (isInitialized && selectedLanguage !== prevLanguage) {
    prevLanguage = selectedLanguage;
    // Immediate, not debounced: a select is a discrete choice, like the collection
    // filter beside it. Debouncing is for the free-text box.
    triggerFiltersImmediate();
  }

  $: if (isInitialized && selectedCollectionId !== prevCollectionId) {
    prevCollectionId = selectedCollectionId;
    triggerFiltersImmediate();
  }

  // Date picker state
  let datePickerOpen = false;
  let datePickerClosing = false;
  let dpStartDate: Date | string | null = null;
  let dpEndDate: Date | string | null = null;

  // Auto-scroll to show the full calendar when it opens
  $: if (datePickerOpen && !datePickerClosing) {
    tick().then(() => {
      const cal = document.querySelector('.datepicker-wrapper .calendars-container');
      if (cal) (cal as HTMLElement).scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  }

  /**
   * Turn a caught value into something renderable, without swallowing it.
   *
   * Every facet fetch below FAILS VISIBLY: an empty list is indistinguishable
   * from "you have none of these", so a 500 used to render as a normal, empty
   * filter with no error and no way to retry (#743a).
   */
  function describeError(err: unknown): string {
    return err instanceof Error ? err.message : String(err);
  }

  // Fetch all tags (cached with TTL, invalidated via WebSocket push)
  async function fetchTags() {
    loadingTags = true;
    errorTags = null;

    try {
      allTags = await apiCache.getOrFetch(cacheKey.tags(), () => listTags(), CacheTTL.TAGS);
    } catch (err) {
      console.error('[FilterSidebar] Error fetching tags:', err);
      allTags = [];
      errorTags = describeError(err);
    } finally {
      loadingTags = false;
    }
  }

  // Fetch speakers for filtering (cached with TTL, invalidated via WebSocket push).
  //
  // Server-side type-to-search (`GET /speakers?for_filter=true&q=...`), the same
  // upgrade `PickerSpeakersTab.svelte` (the chat file-scope picker) already made:
  // this endpoint no longer expects the caller to fetch every filterable speaker
  // once and narrow client-side — the backend declines to build that full list
  // past ~500 distinct names for the same reason
  // (`services/chat/speaker_resolver.py`). `q` is optional, so calling this with
  // no argument (initial load, WebSocket-pushed re-fetch) is unchanged.
  //
  // `include_unnamed` asks for speakers nobody has named yet (#743). Without it
  // the roster is people-only, so a library that has been diarized but never
  // renamed — which is every library until someone does the work — offered
  // nothing at all to filter by.
  //
  // `limit` is passed explicitly, one MORE than we display: the server caps the
  // roster, and an over-full page is the only exact way to tell "this was cut"
  // from "this is all of them". Previously neither was sent and a capped list
  // silently omitted speakers (#743b).
  const SPEAKER_FILTER_PAGE_SIZE = 200;
  let speakersTruncated = false;

  async function fetchSpeakers(q = '') {
    loadingSpeakers = true;
    errorSpeakers = null;

    try {
      const rows = await apiCache.getOrFetch(
        cacheKey.speakers(q || undefined),
        async () => {
          const response = await axiosInstance.get('/speakers', {
            params: {
              for_filter: true,
              q: q.trim() || undefined,
              include_unnamed: true,
              limit: SPEAKER_FILTER_PAGE_SIZE + 1
            }
          });
          return response.data;
        },
        CacheTTL.SPEAKERS
      );
      const list = Array.isArray(rows) ? rows : [];
      speakersTruncated = list.length > SPEAKER_FILTER_PAGE_SIZE;
      allSpeakers = list.slice(0, SPEAKER_FILTER_PAGE_SIZE);
    } catch (err) {
      console.error('Error fetching speakers:', err);
      allSpeakers = [];
      speakersTruncated = false;
      errorSpeakers = describeError(err);
    } finally {
      loadingSpeakers = false;
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

  /**
   * Handle tag selection
   * @param {string} tag - The tag to toggle
   */
  function toggleTag(tag: string) {
    const index = selectedTags.indexOf(tag);

    if (index === -1) {
      selectedTags = [...selectedTags, tag];
    } else {
      selectedTags = selectedTags.filter(t => t !== tag);
    }
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
   * Handle speaker selection (multi-select like tags)
   * @param {string} speaker - The speaker to toggle
   */
  function toggleSpeaker(speaker: string) {
    const index = selectedSpeakers.indexOf(speaker);

    if (index === -1) {
      selectedSpeakers = [...selectedSpeakers, speaker];
    } else {
      selectedSpeakers = selectedSpeakers.filter(s => s !== speaker);
    }
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

  /**
   * Handle date picker range change
   */
  function handleDatePickerChange(event: { startDate: Date | string; endDate?: Date | string }) {
    const start = event.startDate ? new Date(event.startDate) : null;
    const end = event.endDate ? new Date(event.endDate) : null;
    dateRange = {
      from: start && !isNaN(start.getTime()) ? start : null,
      to: end && !isNaN(end.getTime()) ? end : null,
    };
    if (dateRange.from && dateRange.to) {
      datePickerClosing = true;
      setTimeout(() => {
        datePickerOpen = false;
        datePickerClosing = false;
      }, 350);
    }
    triggerFiltersImmediate();
  }

  /**
   * Clear date range filter
   */
  function clearDateRange() {
    dpStartDate = null;
    dpEndDate = null;
    dateRange = { from: null, to: null };
    triggerFiltersImmediate();
  }

  let availableLanguages: string[] = [];

  async function fetchMediaMetadata() {
    errorMetadata = null;
    try {
      const data = await apiCache.getOrFetch(
        cacheKey.metadataFilters(),
        async () => {
          const response = await axiosInstance.get('/files/metadata-filters', { params: { ownership: 'all' } });
          return response.data;
        },
        CacheTTL.METADATA
      );

      if (data.duration) {
        const minDur = Math.floor(data.duration.min ?? 0);
        const maxDur = Math.ceil(data.duration.max ?? 0);
        durationBounds = { min: minDur, max: Math.max(maxDur, minDur + 60) };
        // Only reset slider if user hasn't set a filter
        if (durationRange.min === null && durationRange.max === null) {
          durationSliderValues = [durationBounds.min, durationBounds.max];
        }
      }

      if (data.file_size) {
        const minSize = Math.floor((data.file_size.min ?? 0) / (1024 * 1024));
        const maxSize = Math.ceil((data.file_size.max ?? 0) / (1024 * 1024));
        fileSizeBounds = { min: minSize, max: Math.max(maxSize, minSize + 1) };
        if (fileSizeRange.min === null && fileSizeRange.max === null) {
          fileSizeSliderValues = [fileSizeBounds.min, fileSizeBounds.max];
        }
      }

      availableLanguages = Array.isArray(data.languages) ? data.languages : [];

      metadataLoaded = true;
    } catch (error) {
      console.error('Error fetching media metadata:', error);
      // Deliberately NOT `metadataLoaded = true` (#744): the range sliders are
      // meaningless without the library's real bounds, and rendering them
      // against the 0–3600 / 0–1024 seed is what made a moved handle silently
      // match every file. Show the failure and offer a retry instead.
      metadataLoaded = false;
      errorMetadata = describeError(error);
    }
  }

  function formatFileSize(mb: number): string {
    if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
    return `${Math.round(mb)} MB`;
  }

  function handleDurationSliderChange(e: CustomEvent<{ values: number[] }>) {
    const [min, max] = e.detail.values;
    const isAtMin = min <= durationBounds.min;
    const isAtMax = max >= durationBounds.max;
    durationRange = {
      min: isAtMin ? null : min,
      max: isAtMax ? null : max,
    };
    triggerFiltersDebounced();
  }

  function handleFileSizeSliderChange(e: CustomEvent<{ values: number[] }>) {
    const [min, max] = e.detail.values;
    const isAtMin = min <= fileSizeBounds.min;
    const isAtMax = max >= fileSizeBounds.max;
    fileSizeRange = {
      min: isAtMin ? null : min,
      max: isAtMax ? null : max,
    };
    triggerFiltersDebounced();
  }

  /**
   * Handle ownership filter change
   */
  function setOwnershipFilter(value: 'all' | 'mine' | 'shared') {
    ownershipFilter = value;
    triggerFiltersImmediate();
  }

  /**
   * Toggle a file type in the filter
   * @param {string} fileType - The file type to toggle
   */
  function toggleFileType(fileType: string) {
    const index = selectedFileTypes.indexOf(fileType);

    if (index === -1) {
      selectedFileTypes = [...selectedFileTypes, fileType];
    } else {
      selectedFileTypes = selectedFileTypes.filter(ft => ft !== fileType);
    }
    triggerFiltersImmediate();
  }

  /**
   * Toggle a status in the filter
   * @param {string} status - The status to toggle
   */
  function toggleStatus(status: string) {
    const index = selectedStatuses.indexOf(status);

    if (index === -1) {
      selectedStatuses = [...selectedStatuses, status];
    } else {
      selectedStatuses = selectedStatuses.filter(s => s !== status);
    }
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
      fileSizeRange,
      fileTypes: selectedFileTypes,
      statuses: selectedStatuses,
      ownership: ownershipFilter,
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
    dpStartDate = null;
    dpEndDate = null;
    datePickerOpen = false;
    durationRange = { min: null, max: null };
    fileSizeRange = { min: null, max: null };
    selectedFileTypes = [];
    selectedStatuses = [];
    ownershipFilter = 'all';

    // Reset sliders to full bounds
    durationSliderValues = [durationBounds.min, durationBounds.max];
    fileSizeSliderValues = [fileSizeBounds.min, fileSizeBounds.max];

    // Sync prev values so watchers don't fire on re-enable
    prevSearchQuery = '';
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
    if (scope === 'tags' || scope === 'all') fetchTags();
    // Re-fetch under whatever the user is currently typing, not the unfiltered
    // default — otherwise a push-invalidation while searching would silently
    // discard the in-progress search and repopulate the full list underneath it.
    if (scope === 'speakers' || scope === 'all') fetchSpeakers(speakerSearchQuery);
    if (scope === 'metadata' || scope === 'files' || scope === 'all') fetchMediaMetadata();
  }

  onMount(() => {
    fetchTags();
    fetchSpeakers();
    fetchMediaMetadata();

    // Listen for push-based cache invalidation from WebSocket
    window.addEventListener('cache-invalidated', handleCacheInvalidation);

    // Initialize date picker from dateRange props
    if (dateRange.from instanceof Date) {
      dpStartDate = dateRange.from;
    }
    if (dateRange.to instanceof Date) {
      dpEndDate = dateRange.to;
    }

    // Restore slider positions from filter props
    if (durationRange.min !== null || durationRange.max !== null) {
      durationSliderValues = [
        durationRange.min ?? durationBounds.min,
        durationRange.max ?? durationBounds.max,
      ];
    }
    if (fileSizeRange.min !== null || fileSizeRange.max !== null) {
      fileSizeSliderValues = [
        fileSizeRange.min ?? fileSizeBounds.min,
        fileSizeRange.max ?? fileSizeBounds.max,
      ];
    }

    // Sync prev values and enable reactive watchers after mount
    prevSearchQuery = searchQuery;
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

  <div class="filter-section">
    <h3>{$t('filter.searchFiles')}</h3>
    <input
      type="text"
      bind:value={searchQuery}
      on:keydown={(e) => { if (e.key === 'Enter') e.preventDefault(); }}
      placeholder={$t('filter.searchPlaceholder')}
      class="filter-input"
      title={$t('filter.searchTooltip')}
    />
    <small class="input-help">{$t('filter.searchHelp')}</small>
  </div>

  <div class="filter-section">
    <h3>{$t('filter.tags')}</h3>
    {#if loadingTags}
      <p class="loading-text">{$t('filter.loadingTags')}</p>
    {:else if errorTags}
      <EmptyState
        icon="⚠️"
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
        {#each allTags.slice(0, 6) as tag}
          <button
            class="tag-button {selectedTags.includes(tag.name) ? 'selected' : ''}"
            on:click={() => toggleTag(tag.name)}
            title={$t('filter.tagTooltip', { tag: tag.name, count: tag.usage_count ? $t('filter.tagUsedInFiles', { count: tag.usage_count }) : '' })}
          >
            {tag.name}
            {#if tag.usage_count}
              <span class="tag-count">{tag.usage_count}</span>
            {/if}
          </button>
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
        icon="⚠️"
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
          {#each namedSpeakers.slice(0, 4) as speaker}
            <button
              class="speaker-button {selectedSpeakers.includes(speakerLabel(speaker)) ? 'selected' : ''}"
              on:click={() => toggleSpeaker(speakerLabel(speaker))}
              title={$t('filter.speakerTooltip', { speaker: translateSpeakerLabel(speakerLabel(speaker)), count: speaker.media_count ? $t('filter.speakerAppearsInFiles', { count: speaker.media_count }) : '' })}
            >
              {translateSpeakerLabel(speakerLabel(speaker))}
              {#if speaker.media_count}
                <span class="speaker-count">{speaker.media_count}</span>
              {/if}
            </button>
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
            <div class="speaker-search-status"><Spinner size="small" /></div>
          {/if}
        </div>
      {/if}
      <!-- Unlabeled speakers: ONE facet, never a list of pseudo-people (#743). -->
      {#if unlabeledSpeakers.length > 0}
        <div class="speakers-list">
          <button
            class="speaker-button {unlabeledSelected ? 'selected' : ''}"
            data-testid="unlabeled-speakers-facet"
            aria-pressed={unlabeledSelected}
            on:click={toggleUnlabeledSpeakers}
            title={$t('filter.unlabeledSpeakersTooltip')}
          >
            {$t('filter.unlabeledSpeakers')}
          </button>
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
    <div class="datepicker-wrapper" class:closing={datePickerClosing}>
      <DatePicker
        isRange
        enableFutureDates
        includeFont={false}
        bind:isOpen={datePickerOpen}
        bind:startDate={dpStartDate}
        bind:endDate={dpEndDate}
        onDateChange={handleDatePickerChange}
      >
        <button
          type="button"
          class="date-trigger-btn"
          on:click={() => datePickerOpen = !datePickerOpen}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="date-icon">
            <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
            <line x1="16" y1="2" x2="16" y2="6"></line>
            <line x1="8" y1="2" x2="8" y2="6"></line>
            <line x1="3" y1="10" x2="21" y2="10"></line>
          </svg>
          <span class="date-text">
            {#if dateRange.from && dateRange.to}
              {format(dateRange.from, 'MMM d, yyyy')} — {format(dateRange.to, 'MMM d, yyyy')}
            {:else if dateRange.from}
              {format(dateRange.from, 'MMM d, yyyy')} — ...
            {:else}
              {$t('filter.selectDateRange')}
            {/if}
          </span>
        </button>
      </DatePicker>
    </div>
  </div>

  <!-- File Type -->
  <div class="filter-section">
    <h3>{$t('filter.fileType')}</h3>
    <div class="file-type-list">
      {#each availableFileTypes as fileType}
        <button
          class="file-type-button {selectedFileTypes.includes(fileType) ? 'selected' : ''}"
          on:click={() => toggleFileType(fileType)}
          title={$t('filter.fileTypeTooltip', { type: fileType })}
        >
          {fileType === 'audio' ? $t('common.audio') : $t('common.video')}
        </button>
      {/each}
    </div>
  </div>

  <!-- Transcript language (#453). Rendered only when the library actually holds more
       than one, so a single-language user never sees a filter that can do nothing. -->
  {#if availableLanguages.length > 1}
    <div class="filter-section">
      <h3>{$t('filter.language')}</h3>
      <div class="file-type-list">
        {#each availableLanguages as lang}
          <button
            class="file-type-button {selectedLanguage === lang ? 'selected' : ''}"
            on:click={() => (selectedLanguage = selectedLanguage === lang ? null : lang)}
            title={$t('filter.languageTooltip', { language: lang })}
          >
            {lang.toUpperCase()}
          </button>
        {/each}
      </div>
    </div>
  {/if}

  <!-- Duration + File Size ranges.
       Both are gated on `metadataLoaded` (#744): a range control whose bounds
       are the hardcoded seed rather than the library's real min/max emits a
       filter every file satisfies, which reads as "the slider does nothing". -->
  {#if errorMetadata}
    <div class="filter-section">
      <h3>{$t('filter.duration')}</h3>
      <EmptyState
        icon="⚠️"
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
    <!-- Duration Range -->
    <div class="filter-section">
      <h3>{$t('filter.duration')}</h3>
      <div class="slider-labels">
        <span>{formatClock(durationSliderValues[0])}</span>
        <span>{formatClock(durationSliderValues[1])}</span>
      </div>
      <div class="slider-wrapper">
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

    <!-- File Size Range -->
    <div class="filter-section">
      <h3>{$t('filter.fileSize')}</h3>
      <div class="slider-labels">
        <span>{formatFileSize(fileSizeSliderValues[0])}</span>
        <span>{formatFileSize(fileSizeSliderValues[1])}</span>
      </div>
      <div class="slider-wrapper">
        <RangeSlider
          bind:values={fileSizeSliderValues}
          min={fileSizeBounds.min}
          max={fileSizeBounds.max}
          step={fileSizeBounds.max > 10240 ? 100 : fileSizeBounds.max > 1024 ? 10 : 1}
          range
          pushy
          on:change={handleFileSizeSliderChange}
        />
      </div>
    </div>
  {/if}

  <!-- Processing Status -->
  <div class="filter-section">
    <h3>{$t('filter.processingStatus')}</h3>
    <div class="status-list">
      {#each availableStatuses as status}
        <button
          class="status-button {selectedStatuses.includes(status) ? 'selected' : ''}"
          on:click={() => toggleStatus(status)}
          title={$t('filter.statusTooltip', { status })}
        >
          {status === 'pending' ? $t('common.pending') : status === 'processing' ? $t('common.processing') : status === 'completed' ? $t('common.completed') : status === 'error' ? $t('common.error') : status.charAt(0).toUpperCase() + status.slice(1)}
        </button>
      {/each}
    </div>
  </div>

  <div class="filter-section">
    <h3>{$t('filter.ownership')}</h3>
    <div class="ownership-list">
      <button
        class="ownership-button"
        class:selected={ownershipFilter === 'all'}
        on:click={() => setOwnershipFilter('all')}
      >
        {$t('filter.allFiles')}
      </button>
      <button
        class="ownership-button"
        class:selected={ownershipFilter === 'mine'}
        on:click={() => setOwnershipFilter('mine')}
      >
        {$t('filter.myFiles')}
      </button>
      <button
        class="ownership-button"
        class:selected={ownershipFilter === 'shared'}
        on:click={() => setOwnershipFilter('shared')}
      >
        {$t('filter.sharedWithMe')}
      </button>
    </div>
  </div>
</div>

<style>
  .filter-sidebar {
    background-color: var(--surface-color);
    border-radius: 8px;
    padding: 0.75rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .filter-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
  }

  .filter-header h2 {
    font-size: 1rem;
    font-weight: 600;
    margin: 0;
  }

  .header-buttons {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .reset-button {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-secondary);
    padding: 0.3rem 0.7rem;
    font-size: 0.75rem;
    font-weight: 400;
    cursor: pointer;
    transition: all 0.2s ease;
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
    gap: 0.5rem;
    position: relative;
  }

  .filter-section:not(:first-child) {
    padding-top: 0.25rem;
  }

  .filter-section:not(:first-child)::before {
    content: '';
    position: absolute;
    top: -0.5rem;
    left: 5%;
    right: 5%;
    height: 2px;
    background: var(--border-color);
    opacity: 0.5;
  }

  .filter-section h3 {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    margin: 0;
  }

  .filter-input {
    padding: 0.45rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-size: 0.8rem;
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
    border-radius: 50%;
    background-color: var(--background-color);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .clear-inline-btn:hover {
    background-color: var(--hover-color);
    color: var(--text-color);
  }

  /* Date picker wrapper */
  .datepicker-wrapper {
    position: relative;
  }

  .date-trigger-btn {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    padding: 0.45rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-color);
    color: var(--text-color);
    font-size: 0.8rem;
    cursor: pointer;
    transition: border-color 0.2s ease;
    text-align: left;
  }

  .date-trigger-btn:hover {
    border-color: var(--primary-color-light, #93c5fd);
  }

  .date-icon {
    flex-shrink: 0;
    color: var(--text-secondary);
  }

  .date-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Datepicker theme — inline below trigger, light/dark mode */
  .datepicker-wrapper :global(.datepicker) {
    font-family: inherit;
  }

  /* Core layout: render inline below trigger, fit sidebar width */
  .datepicker-wrapper :global(.datepicker .calendars-container) {
    position: static !important;
    margin-top: 0.5rem;
    width: 100% !important;
    box-shadow: none !important;
    border-radius: 8px;
    opacity: 1;
    transition: opacity 0.3s ease;
    /* Theming */
    --datepicker-container-background: var(--surface-color, #fff);
    --datepicker-container-border: 1px solid var(--border-color, #e8e9ea);
    --datepicker-container-border-radius: 8px;
    --datepicker-container-box-shadow: none;
    --datepicker-container-font-family: inherit;
    --datepicker-container-width: 100%;
    --datepicker-color: var(--text-color, #21333d);
    --datepicker-border-color: var(--border-color, #e8e9ea);
    --datepicker-state-active: var(--primary-color, #3b82f6);
    --datepicker-state-hover: var(--hover-color, #e7f7fc);
    --datepicker-font-size-base: 0.8rem;
    /* Calendar sizing */
    --datepicker-calendar-width: 100%;
    --datepicker-calendar-padding: 4px 4px 12px;
    --datepicker-calendar-day-height: 32px;
    --datepicker-calendar-day-width: 32px;
    --datepicker-calendar-day-padding: 2px;
    --datepicker-calendar-day-font-size: 0.8rem;
    --datepicker-calendar-dow-font-size: 0.75rem;
    --datepicker-calendar-dow-margin-bottom: 6px;
    --datepicker-calendar-header-font-size: 0.95rem;
    --datepicker-calendar-header-padding: 8px 2px;
    --datepicker-calendar-header-margin: 0 0 6px 0;
    /* Colors — light mode */
    --datepicker-calendar-day-color: var(--text-color, #232a32);
    --datepicker-calendar-day-color-hover: var(--text-color, #232a32);
    --datepicker-calendar-day-background-hover: var(--hover-color, #f5f5f5);
    --datepicker-calendar-dow-color: var(--text-secondary, #8b9198);
    --datepicker-calendar-header-color: var(--text-color, #21333d);
    --datepicker-calendar-header-text-color: var(--text-color, #21333d);
    --datepicker-calendar-header-month-nav-color: var(--text-color, #21333d);
    --datepicker-calendar-header-month-nav-background-hover: var(--hover-color, #f5f5f5);
    --datepicker-calendar-today-border: 1px solid var(--text-color, #232a32);
    --datepicker-calendar-day-other-color: var(--text-secondary, #d1d3d6);
  }

  .datepicker-wrapper :global(.datepicker .calendars-container .calendar) {
    width: 100% !important;
    padding: 4px 4px 12px !important;
  }

  .datepicker-wrapper :global(.datepicker .calendars-container .calendar .month) {
    width: 100%;
  }

  .datepicker-wrapper :global(.datepicker .calendars-container .calendar .date span) {
    width: 32px !important;
    height: 32px !important;
    font-size: 0.8rem !important;
    padding: 2px !important;
  }

  .datepicker-wrapper :global(.datepicker .calendars-container .calendar .dow) {
    font-size: 0.75rem !important;
  }

  /* Fade out calendar on close */
  .datepicker-wrapper.closing :global(.datepicker .calendars-container) {
    opacity: 0;
  }

  /* Dark mode overrides */
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .calendars-container) {
    --datepicker-container-background: var(--surface-color, #1e293b);
    --datepicker-color: var(--text-color, #e2e8f0);
    --datepicker-container-border: 1px solid var(--border-color, #334155);
    --datepicker-border-color: var(--border-color, #334155);
    --datepicker-state-active: var(--primary-color, #3b82f6);
    --datepicker-state-hover: rgba(59, 130, 246, 0.15);
    --datepicker-calendar-day-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-day-color-hover: #fff;
    --datepicker-calendar-day-color-disabled: var(--text-secondary, #64748b);
    --datepicker-calendar-day-background-hover: rgba(255, 255, 255, 0.1);
    --datepicker-calendar-dow-color: var(--text-secondary, #94a3b8);
    --datepicker-calendar-header-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-text-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-month-nav-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-month-nav-background-hover: rgba(255, 255, 255, 0.1);
    --datepicker-calendar-today-border: 1px solid var(--text-color, #e2e8f0);
    --datepicker-calendar-day-other-color: var(--text-secondary, #475569);
    /* Range selection colors */
    --datepicker-calendar-range-background: rgba(59, 130, 246, 0.2);
    --datepicker-calendar-range-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-range-start-end-background: #3b82f6;
    --datepicker-calendar-range-start-end-color: #fff;
    --datepicker-calendar-range-included-background: rgba(59, 130, 246, 0.12);
    --datepicker-calendar-range-included-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-range-included-box-shadow: inset 20px 0 0 rgba(59, 130, 246, 0.12);
    /* Box-shadows behind start/end circles */
    --datepicker-calendar-range-start-box-shadow: inset -20px 0 0 rgba(59, 130, 246, 0.15);
    --datepicker-calendar-range-end-box-shadow: inset 20px 0 0 rgba(59, 130, 246, 0.15);
    --datepicker-calendar-range-start-box-shadow-selected: inset -20px 0 0 var(--surface-color, #1e293b);
    --datepicker-calendar-range-end-box-shadow-selected: inset 20px 0 0 var(--surface-color, #1e293b);
  }

  /* Invert nav arrow icons in dark mode (they're base64 black SVGs) */
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .icon-previous-month),
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .icon-next-month),
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .icon-next-year),
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .icon-previous-year) {
    filter: invert(1);
  }

  .dropdown-section {
    margin-top: 0.75rem;
  }

  .loading-text,
  .empty-text {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin: 0;
  }

  .slider-labels {
    display: flex;
    justify-content: space-between;
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-bottom: 0.25rem;
    font-variant-numeric: tabular-nums;
  }

  .slider-wrapper {
    padding: 0 0.25rem;
    --range-slider: var(--border-color, #d7dada);
    --range-handle-inactive: #3b82f6;
    --range-handle: #3b82f6;
    --range-handle-focus: #2563eb;
    --range-range: rgba(59, 130, 246, 0.25);
    --range-pip: var(--border-color, #d7dada);
    --range-pip-active: #3b82f6;
    --range-pip-in-range: #3b82f6;
    font-size: 0.75rem;
  }

  .slider-wrapper :global(.rangeSlider) {
    margin: 0.5rem 0;
  }

  /* Vertical line handles */
  .slider-wrapper :global(.rangeSlider .rangeHandle) {
    width: 6px !important;
    height: 22px !important;
    cursor: pointer !important;
  }

  .slider-wrapper :global(.rangeSlider .rangeNub) {
    width: 6px !important;
    height: 22px !important;
    border-radius: 2px !important;
    border: none !important;
    background-color: #3b82f6 !important;
    box-shadow: 0 1px 3px rgba(59, 130, 246, 0.3) !important;
    transform: none !important;
    transition: height 0.15s ease, width 0.15s ease, margin 0.15s ease !important;
  }

  /* Slightly larger on hover */
  .slider-wrapper :global(.rangeSlider .rangeHandle:hover .rangeNub) {
    width: 8px !important;
    height: 26px !important;
    margin-top: -2px !important;
    margin-left: -1px !important;
  }

  /* Hide the ripple effect */
  .slider-wrapper :global(.rangeSlider .rangeHandle::before) {
    display: none !important;
  }

  /* Range bar — same solid color as handles */
  .slider-wrapper :global(.rangeSlider .rangeBar) {
    background-color: #3b82f6 !important;
  }

  /* Pointer cursor on the track too */
  .slider-wrapper :global(.rangeSlider) {
    cursor: pointer !important;
  }

  /* Status row for the dropdown's server-side speaker search. */
  .speaker-search-status {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.35rem 0;
  }

  /* Retry control for a facet whose fetch failed — an empty facet must never
     be the way an outage looks (#743a). */
  .retry-button {
    margin-top: 0.5rem;
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    padding: 0.3rem 0.8rem;
    font-size: 0.75rem;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .retry-button:hover {
    background-color: var(--hover-color);
    border-color: var(--primary-color);
  }

  /* Tag and Speaker button styles */
  .tags-list,
  .speakers-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .tag-button,
  .speaker-button {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.8rem;
    font-weight: 400;
    padding: 0.35rem 0.7rem;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .tag-button:hover,
  .speaker-button:hover {
    background-color: var(--hover-color);
    border-color: var(--primary-color-light);
  }

  .tag-button.selected,
  .speaker-button.selected {
    background-color: #3b82f6;
    color: white;
    border-color: var(--primary-color);
  }

  /* File Type and Status button styles */
  .file-type-list,
  .status-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .file-type-button,
  .status-button {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.8rem;
    font-weight: 400;
    padding: 0.35rem 0.7rem;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .file-type-button:hover,
  .status-button:hover {
    background-color: var(--hover-color);
    border-color: var(--primary-color-light);
  }

  .file-type-button.selected,
  .status-button.selected {
    background-color: #3b82f6;
    color: white;
    border-color: var(--primary-color);
  }

  /* Ownership filter styles */
  .ownership-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .ownership-button {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.8rem;
    font-weight: 400;
    padding: 0.35rem 0.7rem;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .ownership-button:hover {
    background-color: var(--hover-color);
    border-color: var(--primary-color-light);
  }

  .ownership-button.selected {
    background-color: #3b82f6;
    color: white;
    border-color: var(--primary-color);
  }

  /* Input help text */
  .input-help {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-top: 0.25rem;
    display: block;
    font-style: italic;
  }

</style>
