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
  import { user } from '$stores/auth';
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
    /** Owner's uuid — compared against `$user` to badge shared-with-me recordings. */
    user_id?: string | null;
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
        // Without this the backend defaults to 'mine' — the #385 shape recurring
        // here: a recording shared with the caller (but not owned by them) simply
        // never appeared in this list, and chat could not be scoped to it at all.
        ownership: 'all',
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

  function isOwned(file: PickerFile): boolean {
    return !file.user_id || file.user_id === $user?.uuid;
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
          {#if !isOwned(file)}
            <span
              class="ownership-badge"
              data-testid="picker-file-shared-badge"
              aria-label={$t('chat.picker.sharedBadgeLabel')}
              >{$t('chat.picker.sharedBadge')}</span
            >
          {/if}
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

  /* --- selection affordance ---
     A bare native checkbox against a list of similar-looking rows makes the
     selected set genuinely hard to read at a glance. accent-color paints the
     control itself, and the row tint + weight make the selection legible
     without scanning the checkbox column. */
  .picker-row input[type='checkbox'] {
    flex: none;
    width: 1rem;
    height: 1rem;
    accent-color: var(--primary-color);
    cursor: pointer;
  }

  .picker-row:has(input:checked) {
    background-color: rgba(var(--primary-color-rgb), 0.12);
    font-weight: 600;
  }

  .picker-row:focus-within {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
  }

  .row-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Badges a recording that is shared-with-me rather than owned (#385 shape) —
     without `ownership: all` on the list request these never appeared here at
     all, so their presence needs a visible cue distinguishing them from the
     caller's own recordings. */
  .ownership-badge {
    flex: none;
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--text-secondary);
    background-color: var(--button-hover);
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
