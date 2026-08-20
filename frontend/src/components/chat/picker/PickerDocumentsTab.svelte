<!--
  PickerDocumentsTab.svelte — pick individual documents (#362 lane C3).

  Mirrors PickerFilesTab.svelte's shape exactly (server-paginated list, IntersectionObserver
  sentinel, same selection affordance) but reads from `GET /documents` rather than
  `GET /files`, and only offers `completed` documents — a pending/error document has no
  indexed chunks to retrieve from yet.

  ⚠️ Selected uuids are dispatched into the SAME `file_uuids` array `PickerFilesTab` writes
  to (`ChatScope.file_uuids` has no separate `document_uuids` field — see
  `$lib/types/chat.ts`). That is deliberate, not a shortcut: a document is a
  `transcript_chunks` row exactly like a media file's chunks are, so one scope array is the
  right shape once the resolver on the other side accepts both. As of this writing
  `backend/app/services/chat/context_resolver.py::_resolve_explicit_files` only resolves a
  uuid against `MediaFile` — a selected document uuid here is silently dropped by that
  resolver today (the general library-wide retrieval path already reaches document
  chunks; only EXPLICIT per-document scoping does not yet). That resolver lives outside
  this lane's file set (`backend/app/services/chat/**` is owned by the chat lane) and needs
  a small extension — try `Document` when `MediaFile` lookup misses — to close the loop.
  Built now so the frontend needs no further change the day that lands.
-->
<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import SearchBar from '$components/ui/SearchBar.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { listDocuments } from '$lib/api/documents';

  /** Selected document uuids (draft state owned by the modal). */
  export let selected: string[] = [];

  const PAGE_SIZE = 30;

  const dispatch = createEventDispatcher<{ change: string[] }>();

  interface PickerDocument {
    uuid: string;
    filename: string;
  }

  let documents: PickerDocument[] = [];
  let query = '';
  let skip = 0;
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
      const offset = reset ? 0 : skip;
      const response = await listDocuments(offset, PAGE_SIZE, {
        status: ['completed'],
        search: query.trim() || undefined,
        sortBy: 'filename',
        sortOrder: 'asc',
      });
      const items: PickerDocument[] = response.documents;
      documents = reset ? items : [...documents, ...items];
      skip = offset + items.length;
      hasMore = skip < response.total;
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
    skip = 0;
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
</script>

<div class="picker-tab">
  <SearchBar
    bind:value={query}
    placeholder={$t('chat.picker.searchDocuments')}
    ariaLabel={$t('chat.picker.searchDocuments')}
    on:search={handleSearch}
    size="sm"
  />

  <ul class="picker-list" data-testid="picker-documents-list">
    {#each documents as doc (doc.uuid)}
      <li>
        <label class="picker-row">
          <input
            type="checkbox"
            checked={selectedSet.has(doc.uuid)}
            on:change={() => toggle(doc.uuid)}
            data-testid="picker-document-checkbox"
          />
          <span class="row-label">{doc.filename}</span>
        </label>
      </li>
    {/each}

    {#if !loading && documents.length === 0}
      <li class="empty">{$t('chat.picker.emptyDocuments')}</li>
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
