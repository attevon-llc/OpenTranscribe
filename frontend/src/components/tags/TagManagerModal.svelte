<script lang="ts">
  import { SvelteMap } from 'svelte/reactivity';
  /**
   * The tag manager: create, browse, select and change tags.
   *
   * A dialog BODY, not a dialog: `routes/+page.svelte` wraps it in
   * `ui/BaseModal`. This is the only component of the manager that talks to
   * the API; the list, the panes and the filter bar render props and emit
   * intent. Nothing is optimistic: every mutation is followed by a reload.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { user } from '$stores/auth';
  import { capabilities } from '$stores/capabilities';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { clampTagNameInput } from '$lib/utils/tagName';
  import {
    createTag,
    deleteTags,
    getTagImpact,
    listFilesForTag,
    listTagCollisions,
    listTags,
    mergeTags,
    promoteTags,
    renameTag,
  } from '$lib/api/tags';
  import {
    canWriteSystemTags,
    type TagCollisionCluster,
    type TagImpact,
    type TagScope,
    type TaggedFile,
  } from '$lib/types/tag';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ListRowSkeleton from '$components/ui/ListRowSkeleton.svelte';
  import TagList, { type TagListEntry, type TagSelectDetail } from './TagList.svelte';
  import TagFilterBar, { type TagFilterId } from './TagFilterBar.svelte';
  import TagDetailPanel from './TagDetailPanel.svelte';
  import TagBulkSummary from './TagBulkSummary.svelte';
  import TagShareModal from './TagShareModal.svelte';

  type Busy = 'rename' | 'delete' | 'promote' | 'merge';
  type SortKey = 'usage' | 'name';

  let filter: TagFilterId = 'all';
  let scope: TagScope = 'all';

  let loading = true;
  let loadError: string | null = null;
  let flatTags: TagListEntry[] = [];
  let clusters: TagCollisionCluster[] = [];

  let search = '';
  let sort: SortKey = 'usage';

  let createName = '';
  let creating = false;

  let selectedUuids: string[] = [];

  let previewLoading = false;
  let deletePreview: TagImpact | null = null;
  let mergePreview: TagImpact | null = null;
  let renameMergeImpact: TagImpact | null = null;

  let busy: Busy | null = null;
  let pendingUuids: string[] = [];

  let files: TaggedFile[] = [];
  let fileTotal = 0;
  let filesLoading = false;

  let shareTarget: { uuid: string; name: string } | null = null;

  // Counters that let a slower, superseded response be dropped on arrival.
  let loadRequest = 0;
  let previewRequest = 0;
  let filesRequest = 0;

  $: canPromote = canWriteSystemTags($user?.role, $capabilities.tenancyMode);

  $: entryIndex = buildIndex(flatTags, clusters);
  $: selectedSet = new Set(selectedUuids);
  // List order, which is the order the summary shows them in.
  $: selectedEntries = [...entryIndex.values()].filter((entry) => selectedSet.has(entry.uuid));
  $: single = selectedEntries.length === 1 ? selectedEntries[0] : null;

  $: query = search.trim().toLowerCase();
  $: visibleTags = sortTags(
    query ? flatTags.filter((tag) => tag.name.toLowerCase().includes(query)) : flatTags,
    sort
  );
  $: clustered = clusters.length > 0;
  $: showControls = !loading && !loadError && filter !== 'colliding';
  $: libraryEmpty = !loading && !loadError && !clustered && flatTags.length === 0;
  $: noMatch = !loading && !loadError && !clustered && flatTags.length > 0 && !visibleTags.length;
  $: count = loading || loadError ? null : clustered ? clusters.length : visibleTags.length;

  $: suggestedSurvivorUuid =
    selectedUuids.length >= 2
      ? (clusters.find(
          (cluster) =>
            cluster.suggested_survivor_uuid !== null &&
            selectedSet.has(cluster.suggested_survivor_uuid)
        )?.suggested_survivor_uuid ?? null)
      : null;

  $: loadFiles(single?.uuid ?? null);

  const EMPTY_COPY: Record<TagFilterId, { title: string; description: string }> = {
    all: { title: 'tags.manager.empty.allTitle', description: 'tags.manager.empty.allDescription' },
    unused: {
      title: 'tags.manager.empty.unusedTitle',
      description: 'tags.manager.empty.unusedDescription',
    },
    colliding: {
      title: 'tags.manager.empty.collisionsTitle',
      description: 'tags.manager.empty.collisionsDescription',
    },
  };

  /** Every tag the view knows, by uuid, carrying what each pane gates on. */
  function buildIndex(
    tags: TagListEntry[],
    groups: TagCollisionCluster[]
  ): Map<string, TagListEntry> {
    const index = new SvelteMap<string, TagListEntry>();
    const add = (tag: TagListEntry) => {
      if (index.has(tag.uuid)) return;
      index.set(tag.uuid, {
        uuid: tag.uuid,
        name: tag.name,
        source: tag.source,
        usage_count: tag.usage_count,
        ownership: tag.ownership,
        can_write: tag.can_write,
      });
    };
    tags.forEach(add);
    for (const group of groups) {
      group.members.forEach(add);
      group.suggestions.forEach(add);
    }
    return index;
  }

  function sortTags(tags: TagListEntry[], key: SortKey): TagListEntry[] {
    if (key === 'usage') return tags;
    return [...tags].sort((a, b) => a.name.localeCompare(b.name));
  }

  async function load() {
    const request = ++loadRequest;
    loading = true;
    loadError = null;
    try {
      if (filter === 'colliding') {
        const result = await listTagCollisions();
        if (request !== loadRequest) return;
        clusters = result;
        flatTags = [];
      } else {
        const result = await listTags({ unused: filter === 'unused' ? true : undefined, scope });
        if (request !== loadRequest) return;
        flatTags = result;
        clusters = [];
      }
    } catch (error: unknown) {
      if (request !== loadRequest) return;
      flatTags = [];
      clusters = [];
      loadError = getErrorMessage(error, $t('tags.manager.loadFailed'));
    } finally {
      if (request === loadRequest) loading = false;
    }
  }

  async function loadFiles(uuid: string | null) {
    const request = ++filesRequest;
    files = [];
    fileTotal = 0;
    filesLoading = uuid !== null;
    if (uuid === null) return;
    try {
      const result = await listFilesForTag(uuid);
      if (request !== filesRequest) return;
      files = result.files;
      fileTotal = result.total;
    } catch {
      // Non-fatal: the pane says there are no files.
    } finally {
      if (request === filesRequest) filesLoading = false;
    }
  }

  function clearPreviews() {
    previewRequest++;
    previewLoading = false;
    deletePreview = null;
    mergePreview = null;
    renameMergeImpact = null;
  }

  function setSelection(next: string[]) {
    selectedUuids = next;
    clearPreviews();
  }

  function handleSelect(event: CustomEvent<TagSelectDetail>) {
    const { mode, uuids } = event.detail;
    const current = new Set(selectedUuids);
    if (mode === 'replace' || mode === 'range') {
      setSelection([...uuids]);
    } else if (mode === 'add') {
      setSelection([...selectedUuids, ...uuids.filter((uuid) => !current.has(uuid))]);
    } else if (mode === 'toggle') {
      const flipped = new Set(uuids);
      const kept = selectedUuids.filter((uuid) => !flipped.has(uuid));
      setSelection([...kept, ...uuids.filter((uuid) => !current.has(uuid))]);
    } else {
      const everySelected = uuids.every((uuid) => current.has(uuid));
      const group = new Set(uuids);
      setSelection(
        everySelected
          ? selectedUuids.filter((uuid) => !group.has(uuid))
          : [...selectedUuids, ...uuids.filter((uuid) => !current.has(uuid))]
      );
    }
  }

  function changeView(next: { filter?: TagFilterId; scope?: TagScope }) {
    filter = next.filter ?? filter;
    scope = next.scope ?? scope;
    setSelection([]);
    void load();
  }

  async function showPreview(uuids: string[], apply: (impact: TagImpact) => void) {
    clearPreviews();
    const request = previewRequest;
    previewLoading = true;
    try {
      const impact = await getTagImpact(uuids);
      if (request === previewRequest) apply(impact);
    } catch (error: unknown) {
      if (request === previewRequest) {
        toastStore.error(getErrorMessage(error, $t('tags.manager.toast.previewFailed')));
      }
    } finally {
      if (request === previewRequest) previewLoading = false;
    }
  }

  function previewDelete() {
    void showPreview([...selectedUuids], (impact) => (deletePreview = impact));
  }

  function previewMerge(event: CustomEvent<{ survivorUuid: string }>) {
    const sources = selectedUuids.filter((uuid) => uuid !== event.detail.survivorUuid);
    if (sources.length === 0) return;
    void showPreview(sources, (impact) => (mergePreview = impact));
  }

  /**
   * One mutation at a time. `action` resolves to the success message, or to
   * null when it applied nothing and the selection must stay as it is.
   */
  async function mutate(kind: Busy, affected: string[], action: () => Promise<string | null>) {
    if (busy) return;
    busy = kind;
    pendingUuids = affected;
    let applied = false;
    try {
      const message = await action();
      if (message !== null) {
        toastStore.success(message);
        applied = true;
      }
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('tags.manager.toast.actionFailed')));
    } finally {
      busy = null;
      pendingUuids = [];
    }
    if (applied) {
      setSelection([]);
      await load();
    }
  }

  function confirmDelete() {
    const uuids = [...selectedUuids];
    void mutate('delete', uuids, async () => {
      await deleteTags(uuids);
      return $t('tags.manager.toast.deleted');
    });
  }

  function confirmMerge(event: CustomEvent<{ survivorUuid: string }>) {
    const survivor = event.detail.survivorUuid;
    const all = [...selectedUuids];
    const sources = all.filter((uuid) => uuid !== survivor);
    if (sources.length === 0) return;
    void mutate('merge', all, async () => {
      await mergeTags(survivor, sources);
      return $t('tags.manager.toast.merged');
    });
  }

  function promote() {
    const uuids = [...selectedUuids];
    void mutate('promote', uuids, async () => {
      await promoteTags(uuids);
      return $t('tags.manager.promote.done');
    });
  }

  function rename(name: string, confirmMergeName: boolean) {
    if (!single) return;
    const uuid = single.uuid;
    void mutate('rename', [uuid], async () => {
      const result = await renameTag(uuid, { name, confirm_merge: confirmMergeName });
      if (result.requires_confirmation) {
        renameMergeImpact = result.impact;
        return null;
      }
      renameMergeImpact = null;
      return $t(result.merged ? 'tags.manager.toast.merged' : 'tags.manager.toast.renamed');
    });
  }

  /** Uuids of every tag the caller can already see, to tell a resolved name from a new tag. */
  async function knownTagUuids(): Promise<Set<string>> {
    if (filter === 'all' && scope === 'all') return new Set(entryIndex.keys());
    try {
      return new Set((await listTags()).map((tag) => tag.uuid));
    } catch {
      return new Set(entryIndex.keys());
    }
  }

  async function handleCreate() {
    const name = createName.trim();
    if (!name || creating) return;
    creating = true;
    try {
      const known = await knownTagUuids();
      const tag = await createTag(name);
      const resolved = known.has(tag.uuid) || tag.name !== name;
      toastStore.success(
        $t(resolved ? 'tags.manager.create.resolved' : 'tags.manager.create.created', {
          name: tag.name,
        })
      );
      createName = '';
      await load();
      // Only select what the view can show, or the detail pane would be blank.
      if (buildIndex(flatTags, clusters).has(tag.uuid)) setSelection([tag.uuid]);
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('tags.manager.create.failed')));
    } finally {
      creating = false;
    }
  }

  function handleCreateKeydown(event: KeyboardEvent) {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    void handleCreate();
  }

  function handleSortChange(event: Event) {
    const value = (event.currentTarget as HTMLSelectElement).value;
    sort = value === 'name' ? 'name' : 'usage';
  }

  function openShare() {
    if (single) shareTarget = { uuid: single.uuid, name: single.name };
  }

  onMount(() => {
    void load();
  });
</script>

<div class="tags-manager" class:has-selection={selectedEntries.length > 0}>
  <div class="create-row">
    <div class="field-with-icon">
      <svg
        class="field-icon"
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <line x1="12" y1="5" x2="12" y2="19" />
        <line x1="5" y1="12" x2="19" y2="12" />
      </svg>
      <label class="sr-only" for="tags-manager-create">{$t('tags.manager.create.label')}</label>
      <input
        id="tags-manager-create"
        type="text"
        value={createName}
        placeholder={$t('tags.manager.create.placeholder')}
        title={$t('tags.manager.create.tooltip')}
        disabled={creating}
        on:input={(event) => (createName = clampTagNameInput(event))}
        on:keydown={handleCreateKeydown}
      />
    </div>
    <button
      type="button"
      class="btn btn-primary"
      title={$t('tags.manager.create.tooltip')}
      disabled={creating || !createName.trim()}
      on:click={handleCreate}
    >
      {creating ? $t('tags.manager.create.creating') : $t('tags.manager.create.submit')}
    </button>
  </div>

  {#if showControls}
    <div class="list-controls">
      <div class="field-with-icon">
        <svg
          class="field-icon"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <circle cx="11" cy="11" r="7" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          type="search"
          class="search-input"
          bind:value={search}
          placeholder={$t('tags.manager.searchPlaceholder')}
          aria-label={$t('tags.manager.searchLabel')}
        />
        {#if search}
          <button
            type="button"
            class="clear-search"
            title={$t('tags.manager.clearSearch')}
            aria-label={$t('tags.manager.clearSearch')}
            on:click={() => (search = '')}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              aria-hidden="true"
            >
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        {/if}
      </div>
      <label class="sort-label">
        <span>{$t('tags.manager.sortLabel')}</span>
        <select class="sort-select" value={sort} on:change={handleSortChange}>
          <option value="usage">{$t('tags.manager.sort.usage')}</option>
          <option value="name">{$t('tags.manager.sort.name')}</option>
        </select>
      </label>
    </div>
  {/if}

  <TagFilterBar
    {filter}
    {count}
    {scope}
    on:change={(event) => changeView({ filter: event.detail })}
    on:scopeChange={(event) => changeView({ scope: event.detail })}
  />

  <div class="panes">
    <div class="list-pane">
      {#if loading}
        <ListRowSkeleton count={6} size="compact" />
      {:else if loadError}
        <div class="load-error" role="alert">
          <p>{loadError}</p>
          <button type="button" class="btn btn-secondary" on:click={() => void load()}>
            {$t('tags.manager.retry')}
          </button>
        </div>
      {:else if libraryEmpty}
        <EmptyState
          icon="tag"
          title={$t(EMPTY_COPY[filter].title)}
          description={$t(EMPTY_COPY[filter].description)}
        />
      {:else if noMatch}
        <EmptyState title={$t('tags.manager.empty.noMatchTitle', { query: search.trim() })}>
          <svg
            slot="icon"
            width="40"
            height="40"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            aria-hidden="true"
          >
            <circle cx="11" cy="11" r="7" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
        </EmptyState>
      {:else}
        <TagList
          tags={visibleTags}
          {clusters}
          {selectedUuids}
          {pendingUuids}
          label={$t('tags.manager.listLabel')}
          on:select={handleSelect}
        />
      {/if}
    </div>

    <div class="detail-pane">
      {#if selectedEntries.length > 0}
        <button
          type="button"
          class="btn btn-secondary btn-small back-button"
          on:click={() => setSelection([])}
        >
          {$t('tags.manager.back')}
        </button>
      {/if}
      {#if single}
        <TagDetailPanel
          tag={single}
          busy={busy === 'merge' ? null : busy}
          {canPromote}
          {files}
          {fileTotal}
          {filesLoading}
          {previewLoading}
          {deletePreview}
          {renameMergeImpact}
          on:rename={(event) => rename(event.detail.name, false)}
          on:confirmRenameMerge={(event) => rename(event.detail.name, true)}
          on:cancelRenameMerge={() => (renameMergeImpact = null)}
          on:previewDelete={previewDelete}
          on:confirmDelete={confirmDelete}
          on:cancelDelete={() => (deletePreview = null)}
          on:promote={promote}
          on:share={openShare}
        />
      {:else if selectedEntries.length > 1}
        <TagBulkSummary
          tags={selectedEntries}
          {suggestedSurvivorUuid}
          busy={busy === 'rename' ? null : busy}
          {canPromote}
          {previewLoading}
          {mergePreview}
          {deletePreview}
          on:previewMerge={previewMerge}
          on:confirmMerge={confirmMerge}
          on:cancelMerge={() => (mergePreview = null)}
          on:previewDelete={previewDelete}
          on:confirmDelete={confirmDelete}
          on:cancelDelete={() => (deletePreview = null)}
          on:promote={promote}
          on:clear={() => setSelection([])}
        />
      {:else}
        <div class="select-prompt">
          <svg
            width="32"
            height="32"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path
              d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"
            />
            <circle cx="7" cy="7" r="1" />
          </svg>
          <p>{$t('tags.manager.selectPrompt')}</p>
        </div>
      {/if}
    </div>
  </div>
</div>

{#if shareTarget}
  <TagShareModal
    tagUuid={shareTarget.uuid}
    tagName={shareTarget.name}
    on:close={() => (shareTarget = null)}
    on:changed={() => void load()}
  />
{/if}

<style>
  .tags-manager {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-height: 0;
    color: var(--text-color);
  }

  .create-row,
  .list-controls {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .field-with-icon {
    position: relative;
    flex: 1 1 auto;
    min-width: 0;
  }

  .field-with-icon input {
    width: 100%;
    padding-inline-start: var(--space-8);
    padding-inline-end: var(--space-8);
  }

  .field-icon {
    position: absolute;
    inset-block-start: 50%;
    inset-inline-start: var(--space-2-5);
    transform: translateY(-50%);
    color: var(--text-secondary);
    pointer-events: none;
  }

  .clear-search {
    position: absolute;
    inset-block-start: 50%;
    inset-inline-end: var(--space-1-5);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: var(--space-1);
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-secondary);
    box-shadow: none;
    transform: translateY(-50%);
  }

  .clear-search:hover {
    color: var(--text-color);
    background-color: var(--hover-color);
  }

  .sort-label {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .sort-select {
    width: auto;
  }

  .panes {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr);
    gap: var(--space-4);
    align-items: start;
    min-height: 0;
  }

  .list-pane,
  .detail-pane {
    max-height: 60vh;
    overflow-y: auto;
  }

  .list-pane {
    padding-inline-end: var(--space-1);
  }

  .detail-pane {
    position: sticky;
    inset-block-start: 0;
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background-color: var(--surface-color);
  }

  .back-button {
    display: none;
    margin-block-end: var(--space-3);
  }

  .select-prompt {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
    padding-block: var(--space-8);
    color: var(--text-secondary);
    text-align: center;
  }

  .select-prompt p {
    margin: 0;
  }

  .load-error {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-2);
    padding: var(--space-3);
    border: 1px solid var(--error-border);
    border-radius: var(--radius-lg);
    background-color: var(--error-bg);
    color: var(--error-text);
  }

  .load-error p {
    margin: 0;
  }

  .sr-only {
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

  @media (max-width: 768px) {
    .panes {
      grid-template-columns: minmax(0, 1fr);
    }

    .tags-manager.has-selection .list-pane,
    .tags-manager:not(.has-selection) .detail-pane {
      display: none;
    }

    .back-button {
      display: inline-flex;
    }

    .detail-pane {
      position: static;
    }
  }

  @media (max-width: 640px) {
    .create-row,
    .list-controls {
      flex-wrap: wrap;
    }

    .field-with-icon {
      flex-basis: 100%;
    }
  }
</style>
