<!--
  PickerFilesTab.svelte — pick individual recordings.

  Server-paginated with an IntersectionObserver sentinel rather than client-side
  virtualization: VirtualList is coupled to the MediaFile gallery shape, and a
  library can hold far more files than we want to ship to the browser to filter.
-->
<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import SearchBar from '$components/ui/SearchBar.svelte';
  import Spinner from '$components/ui/Spinner.svelte';

  /** Selected file uuids (draft state owned by the modal). */
  export let selected: string[] = [];

  const PAGE_SIZE = 30;

  const dispatch = createEventDispatcher<{ change: string[] }>();

  interface PickerFile {
    uuid: string;
    filename: string;
    title?: string | null;
    duration?: number | null;
    upload_time?: string | null;
  }

  let files: PickerFile[] = [];
  let query = '';
  let page = 1;
  let hasMore = true;
  let loading = false;
  let sentinel: HTMLDivElement;
  let observer: IntersectionObserver | undefined;

  $: selectedSet = new Set(selected);

  async function load(reset = false): Promise<void> {
    if (loading) return;
    if (!reset && !hasMore) return;
    loading = true;
    try {
      const params: Record<string, unknown> = {
        page: reset ? 1 : page,
        page_size: PAGE_SIZE,
        status: 'completed',
      };
      if (query.trim()) params.search = query.trim();

      const { data } = await axiosInstance.get('/files', { params });
      const items: PickerFile[] = data?.items ?? (Array.isArray(data) ? data : []);
      files = reset ? items : [...files, ...items];
      hasMore = data?.has_more ?? items.length === PAGE_SIZE;
      page = (reset ? 1 : page) + 1;
    } catch {
      hasMore = false;
    } finally {
      loading = false;
    }
  }

  function toggle(uuid: string): void {
    const next = selectedSet.has(uuid)
      ? selected.filter((id) => id !== uuid)
      : [...selected, uuid];
    dispatch('change', next);
  }

  async function handleSearch(): Promise<void> {
    page = 1;
    hasMore = true;
    await load(true);
  }

  onMount(() => {
    load(true);
    observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) load();
      },
      { rootMargin: '160px' }
    );
    if (sentinel) observer.observe(sentinel);
  });

  onDestroy(() => observer?.disconnect());

  function displayName(file: PickerFile): string {
    return file.title || file.filename;
  }
</script>

<div class="picker-tab">
  <SearchBar
    bind:value={query}
    placeholder={$t('chat.picker.searchFiles')}
    ariaLabel={$t('chat.picker.searchFiles')}
    on:search={handleSearch}
    size="sm"
  />

  <ul class="picker-list" data-testid="picker-files-list">
    {#each files as file (file.uuid)}
      <li>
        <label class="picker-row">
          <input
            type="checkbox"
            checked={selectedSet.has(file.uuid)}
            on:change={() => toggle(file.uuid)}
            data-testid="picker-file-checkbox"
          />
          <span class="row-label">{displayName(file)}</span>
        </label>
      </li>
    {/each}

    {#if !loading && files.length === 0}
      <li class="empty">{$t('chat.picker.emptyFiles')}</li>
    {/if}
  </ul>

  <div class="sentinel" bind:this={sentinel}>
    {#if loading}
      <Spinner size="small" />
    {/if}
  </div>
</div>

<style>
  .picker-tab {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    min-height: 0;
  }

  .picker-list {
    list-style: none;
    margin: 0;
    padding: 0;
    overflow-y: auto;
    max-height: 42vh;
  }

  .picker-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.5rem 0.4rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.87rem;
    color: var(--text-color);
  }

  .picker-row:hover {
    background-color: var(--button-hover);
  }

  .row-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .empty {
    padding: 1.5rem 0.5rem;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.85rem;
  }

  .sentinel {
    display: flex;
    justify-content: center;
    min-height: 1.5rem;
  }
</style>
