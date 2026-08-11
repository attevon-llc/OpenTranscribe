<script lang="ts">
  /**
   * Tag manager — the coordinator for `$components/tags/*`, shown as a modal.
   *
   * Tags are metadata on the library, not a place to navigate to, so this lives
   * behind the gallery's Tags button beside Collections rather than a top-level
   * route. The gallery decides which mode to open by selection, mirroring
   * `CollectionsPanel`'s `viewMode`: nothing selected opens this manager;
   * a selection opens the bulk apply/remove flow instead.
   *
   * It owns every fetch, the selection, and the mutation lifecycle; the
   * children are presentational and dispatch intent up. Nothing here recomputes
   * what the backend already ships (usage counts, collision clustering, ranked
   * near matches, the suggested survivor) — those are
   * rendered as received.
   */
  import { createEventDispatcher, onMount } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    deleteTags,
    getTagImpact,
    createTag,
    listTagCollisions,
    listTags,
    promoteTags,
    mergeTags,
    renameTag,
  } from '$lib/api/tags';
  import type { TagCollisionCluster, TagImpact } from '$lib/types/tag';
  import TagBulkSummary from '$components/tags/TagBulkSummary.svelte';
  import TagDetailPanel from '$components/tags/TagDetailPanel.svelte';
  import TagFilterBar from '$components/tags/TagFilterBar.svelte';
  import type { TagFilterId } from '$components/tags/TagFilterBar.svelte';
  import type { TagScope } from '$lib/types/tag';
  import { user } from '$stores/auth';
  import TagList from '$components/tags/TagList.svelte';
  import type { TagListEntry, TagSelectDetail } from '$components/tags/TagList.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ListRowSkeleton from '$components/ui/ListRowSkeleton.svelte';

  const dispatch = createEventDispatcher<{ close: void }>();

  // Creating a tag here is the missing third of add/edit/delete. Until now a
  // tag could only be born by tagging a file, which meant the tool called "tag
  // management" could not make one — you had to leave, tag something, come back.
  // Search and sort are client-side: the list is already fully loaded for the
  // active filter, so round-tripping for a substring match would add latency
  // for nothing. CollectionsPanel filters the same way.
  let searchQuery = '';
  let sortBy: 'usage' | 'name' = 'usage';

  $: visibleTags = (() => {
    const q = searchQuery.trim().toLowerCase();
    const matched = q ? tags.filter((tag) => tag.name.toLowerCase().includes(q)) : tags;
    return sortBy === 'name'
      ? [...matched].sort((a, b) => a.name.localeCompare(b.name))
      : matched; // the backend already orders by usage desc, then name
  })();

  let newTagName = '';
  let creating = false;

  async function submitCreate() {
    const name = newTagName.trim();
    if (!name || creating) return;
    creating = true;
    try {
      const tag = await createTag(name);
      // Resolution is normalized-exact, so a typed name may land on a tag that
      // already exists. Say which one rather than reporting a create that was
      // really a no-op.
      const resolved = tag.name !== name;
      toastStore.success(
        resolved
          ? $t('tags.manager.create.resolved', { name: tag.name })
          : $t('tags.manager.create.created', { name: tag.name })
      );
      newTagName = '';
      await loadTags();
      selectedUuids = [tag.uuid];
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('tags.manager.create.failed')));
    } finally {
      creating = false;
    }
  }

  function onCreateKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      submitCreate();
    }
  }

  type Busy = 'rename' | 'merge' | 'delete' | 'promote' | null;

  let filter: TagFilterId = 'all';
  // Ownership scope, a separate axis from the view above so "my unused tags"
  // is one request rather than an unreachable combination.
  let scope: TagScope = 'all';
  // Cosmetic gate only: POST /tags/promote enforces the admin check server-side.
  // `role` is the authorization truth in this app; `is_superuser` is its mirror.
  $: isAdmin = $user?.role === 'admin' || $user?.role === 'super_admin';
  // Promotion is admin-only, same gate — named separately because the button
  // and the write-rights check answer different questions.
  $: canPromote = isAdmin;
  let tags: TagListEntry[] = [];
  let clusters: TagCollisionCluster[] = [];
  let loading = true;
  let loadError = '';

  let selectedUuids: string[] = [];
  let pendingUuids: string[] = [];
  let busy: Busy = null;
  let previewLoading = false;

  let mergePreview: TagImpact | null = null;
  let deletePreview: TagImpact | null = null;
  let renameMergeImpact: TagImpact | null = null;

  /**
   * Every tag the current view knows about, cluster members and near-match
   * suggestions included — the selection can reach all three.
   */
  $: tagIndex = buildIndex(tags, clusters);
  $: selectedEntries = selectedUuids
    .map((uuid) => tagIndex.get(uuid))
    .filter((entry): entry is TagListEntry => entry !== undefined);
  $: rowCount = clusters.length > 0 ? clusters.length : visibleTags.length;
  $: isEmpty = !loading && !loadError && rowCount === 0;
  $: suggestedSurvivorUuid = findSuggestedSurvivor(clusters, selectedUuids);

  function buildIndex(
    flatTags: TagListEntry[],
    tagClusters: TagCollisionCluster[]
  ): Map<string, TagListEntry> {
    const index = new Map<string, TagListEntry>();
    for (const tag of flatTags) index.set(tag.uuid, tag);
    for (const cluster of tagClusters) {
      for (const member of [...cluster.members, ...cluster.suggestions]) {
        index.set(member.uuid, {
          uuid: member.uuid,
          name: member.name,
          source: member.source,
          usage_count: member.usage_count,
        });
      }
    }
    return index;
  }

  /** The backend's survivor pick, when the selection is exactly one cluster. */
  function findSuggestedSurvivor(
    tagClusters: TagCollisionCluster[],
    selection: string[]
  ): string | null {
    if (selection.length < 2) return null;
    for (const cluster of tagClusters) {
      if (!cluster.suggested_survivor_uuid) continue;
      if (selection.includes(cluster.suggested_survivor_uuid)) {
        return cluster.suggested_survivor_uuid;
      }
    }
    return null;
  }

  function clearPreviews() {
    mergePreview = null;
    deletePreview = null;
    renameMergeImpact = null;
  }

  async function loadTags() {
    loading = true;
    loadError = '';
    try {
      if (filter === 'colliding') {
        clusters = await listTagCollisions();
        tags = [];
      } else {
        tags = await listTags({
          unused: filter === 'unused' || undefined,
          scope,
        });
        clusters = [];
      }
    } catch (error: unknown) {
      // The backend deliberately stopped swallowing query errors here, so a
      // failure must read as a failure — never as "you have no tags".
      loadError = getErrorMessage(error, $t('tags.manager.loadFailed'));
      tags = [];
      clusters = [];
    } finally {
      loading = false;
    }
  }

  onMount(loadTags);

  function handleScopeChange(event: CustomEvent<TagScope>) {
    scope = event.detail;
    selectedUuids = [];
    clearPreviews();
    loadTags();
  }

  function promoteSelection() {
    const uuids = [...selectedUuids];
    mutate(
      'promote',
      uuids,
      () => promoteTags(uuids).then(() => undefined),
      'tags.manager.promote.done'
    );
  }

  function handleFilterChange(event: CustomEvent<TagFilterId>) {
    filter = event.detail;
    selectedUuids = [];
    clearPreviews();
    loadTags();
  }

  function handleSelect(event: CustomEvent<TagSelectDetail>) {
    const { mode, uuids } = event.detail;
    if (mode === 'replace' || mode === 'range') {
      selectedUuids = [...uuids];
    } else {
      const next = new Set(selectedUuids);
      if (mode === 'add') {
        for (const uuid of uuids) next.add(uuid);
      } else if (mode === 'group') {
        const allSelected = uuids.every((uuid) => next.has(uuid));
        for (const uuid of uuids) {
          if (allSelected) next.delete(uuid);
          else next.add(uuid);
        }
      } else {
        for (const uuid of uuids) {
          if (next.has(uuid)) next.delete(uuid);
          else next.add(uuid);
        }
      }
      selectedUuids = [...next];
    }
    clearPreviews();
  }

  function clearSelection() {
    selectedUuids = [];
    clearPreviews();
  }

  // ── Impact previews (nothing is applied until the user confirms) ──────────

  async function runPreview(load: () => Promise<void>) {
    previewLoading = true;
    try {
      await load();
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('tags.manager.toast.previewFailed')));
    } finally {
      previewLoading = false;
    }
  }

  function previewDelete() {
    clearPreviews();
    runPreview(async () => {
      deletePreview = await getTagImpact(selectedUuids);
    });
  }


  function previewMerge(event: CustomEvent<{ survivorUuid: string }>) {
    const sources = selectedUuids.filter((uuid) => uuid !== event.detail.survivorUuid);
    if (sources.length === 0) return;
    clearPreviews();
    runPreview(async () => {
      mergePreview = await getTagImpact(sources);
    });
  }

  // ── Mutations ────────────────────────────────────────────────────────────

  /**
   * Merge and reject fan out a search-refresh task per affected file, so the
   * gap between confirming and seeing a result is seconds. The affected rows
   * stay dimmed and non-interactive for the whole round trip — re-clicking
   * merge on a selection already merging is exactly the concurrent case the
   * backend takes row locks to defend against.
   */
  async function mutate(
    kind: Exclude<Busy, null>,
    uuids: string[],
    run: () => Promise<void>,
    successKey: string
  ) {
    busy = kind;
    pendingUuids = uuids;
    try {
      await run();
      toastStore.success($t(successKey));
      selectedUuids = [];
      clearPreviews();
      await loadTags();
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('tags.manager.toast.actionFailed')));
    } finally {
      busy = null;
      pendingUuids = [];
    }
  }

  function confirmDelete() {
    const uuids = [...selectedUuids];
    mutate('delete', uuids, () => deleteTags(uuids).then(() => undefined), 'tags.manager.toast.deleted');
  }



  function confirmMerge(event: CustomEvent<{ survivorUuid: string }>) {
    const survivorUuid = event.detail.survivorUuid;
    const sources = selectedUuids.filter((uuid) => uuid !== survivorUuid);
    if (sources.length === 0) return;
    mutate(
      'merge',
      [...selectedUuids],
      () => mergeTags(survivorUuid, sources).then(() => undefined),
      'tags.manager.toast.merged'
    );
  }

  /**
   * A rename whose new name resolves to a *different* existing tag comes back
   * with `requires_confirmation` and applies nothing — surface the impact and
   * wait rather than silently merging.
   */
  async function submitRename(name: string, confirmMerge = false) {
    const uuid = selectedUuids[0];
    if (!uuid) return;
    busy = 'rename';
    pendingUuids = [uuid];
    try {
      const result = await renameTag(uuid, { name, confirm_merge: confirmMerge });
      if (result.requires_confirmation) {
        renameMergeImpact = result.impact;
        return;
      }
      renameMergeImpact = null;
      toastStore.success(
        $t(result.merged ? 'tags.manager.toast.merged' : 'tags.manager.toast.renamed')
      );
      selectedUuids = [];
      clearPreviews();
      await loadTags();
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('tags.manager.toast.actionFailed')));
    } finally {
      busy = null;
      pendingUuids = [];
    }
  }
</script>

<BaseModal
  isOpen
  maxWidth="1100px"
  title={$t('tags.manager.title')}
  onClose={() => dispatch('close')}
>
  <div class="tags-manager">
  <form class="create-row" on:submit|preventDefault={submitCreate}>
    <label class="create-label" for="new-tag-name">{$t('tags.manager.create.label')}</label>
    <input
      id="new-tag-name"
      type="text"
      class="form-input create-input"
      bind:value={newTagName}
      maxlength="50"
      placeholder={$t('tags.manager.create.placeholder')}
      disabled={creating}
      on:keydown={onCreateKeydown}
    />
    <button
      type="submit"
      class="btn btn-primary"
      disabled={creating || newTagName.trim() === ''}
    >
      {creating ? $t('tags.manager.create.creating') : $t('tags.manager.create.submit')}
    </button>
  </form>

  {#if !loading && !loadError && clusters.length === 0}
    <div class="list-controls">
      <div class="search-wrapper">
        <svg class="search-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="11" cy="11" r="8"></circle>
          <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
        </svg>
        <input
          type="text"
          class="search-input"
          placeholder={$t('tags.manager.searchPlaceholder')}
          aria-label={$t('tags.manager.searchLabel')}
          bind:value={searchQuery}
        />
        {#if searchQuery}
          <button
            type="button"
            class="search-clear"
            on:click={() => (searchQuery = '')}
            title={$t('tags.manager.clearSearch')}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        {/if}
      </div>

      <label class="sort-picker">
        <span class="sort-label">{$t('tags.manager.sortLabel')}</span>
        <select class="form-select sort-select" bind:value={sortBy}>
          <option value="usage">{$t('tags.manager.sort.usage')}</option>
          <option value="name">{$t('tags.manager.sort.name')}</option>
        </select>
      </label>
    </div>
  {/if}

  <TagFilterBar
    {filter}
    {scope}
    count={loading || loadError ? null : rowCount}
    on:change={handleFilterChange}
    on:scopeChange={handleScopeChange}
  />

  <div class="tags-layout" class:has-selection={selectedUuids.length > 0}>
    <div class="list-pane">
      {#if loading}
        <ListRowSkeleton count={6} size="compact" />
      {:else if loadError}
        <div class="error-state" role="alert">
          <p class="error-message">{loadError}</p>
          <button type="button" class="btn btn-primary" on:click={loadTags}>
            {$t('tags.manager.retry')}
          </button>
        </div>
      {:else if isEmpty}
        {#if filter === 'colliding'}
          <EmptyState
            title={$t('tags.manager.empty.collisionsTitle')}
            description={$t('tags.manager.empty.collisionsDescription')}
          >
            <svg slot="icon" xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="18" cy="18" r="3"></circle><circle cx="6" cy="6" r="3"></circle><path d="M6 21V9a9 9 0 0 0 9 9"></path></svg>
          </EmptyState>
        {:else if filter === 'unused'}
          <EmptyState
            title={$t('tags.manager.empty.unusedTitle')}
            description={$t('tags.manager.empty.unusedDescription')}
          >
            <svg slot="icon" xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"></polyline><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path></svg>
          </EmptyState>
        {:else}
          <EmptyState
            title={$t('tags.manager.empty.allTitle')}
            description={$t('tags.manager.empty.allDescription')}
          >
            <svg slot="icon" xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"></path><line x1="7" y1="7" x2="7.01" y2="7"></line></svg>
          </EmptyState>
        {/if}
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
      {#if selectedUuids.length > 0}
        <button type="button" class="detail-back" on:click={clearSelection}>
          {$t('tags.manager.back')}
        </button>
      {/if}

      {#if selectedEntries.length > 1}
        <TagBulkSummary
          tags={selectedEntries}
          {suggestedSurvivorUuid}
          busy={busy === 'rename' ? null : busy}
          {previewLoading}
          {mergePreview}
          {deletePreview}
          {canPromote}
          on:previewMerge={previewMerge}
          on:promote={promoteSelection}
          on:confirmMerge={confirmMerge}
          on:cancelMerge={clearPreviews}
          on:previewDelete={previewDelete}
          on:confirmDelete={confirmDelete}
          on:cancelDelete={clearPreviews}
          on:clear={clearSelection}
        />
      {:else if selectedEntries.length === 1}
        <TagDetailPanel
          tag={selectedEntries[0]}
          busy={busy === 'merge' ? null : busy}
          {previewLoading}
          {deletePreview}
          {renameMergeImpact}
          {canPromote}
          {isAdmin}
          on:rename={(event) => submitRename(event.detail.name)}
          on:promote={promoteSelection}
          on:confirmRenameMerge={(event) => submitRename(event.detail.name, true)}
          on:cancelRenameMerge={() => (renameMergeImpact = null)}
          on:previewDelete={previewDelete}
          on:confirmDelete={confirmDelete}
          on:cancelDelete={clearPreviews}
        />
      {:else}
        <div class="select-prompt">
          <svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"></path>
            <line x1="7" y1="7" x2="7.01" y2="7"></line>
          </svg>
          <p>{$t('tags.manager.selectPrompt')}</p>
        </div>
      {/if}
    </div>
  </div>
  </div>
</BaseModal>

<style>
  .create-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border-color);
  }

  .create-label {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .create-input {
    flex: 1;
    min-width: 0;
  }

  @media (max-width: 640px) {
    .create-row {
      flex-wrap: wrap;
    }

    .create-input {
      flex-basis: 100%;
      order: 1;
    }
  }

  .list-controls {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  /* Copied from CollectionsPanel so the two panels read as one component. */
  .search-wrapper {
    position: relative;
    display: flex;
    align-items: center;
    flex: 1;
    min-width: 0;
  }

  .search-icon {
    position: absolute;
    left: 10px;
    color: var(--text-secondary);
    pointer-events: none;
  }

  .search-input {
    width: 100%;
    padding: 0.45rem 2rem 0.45rem 2rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 0.875rem;
  }

  .search-input:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  .search-clear {
    position: absolute;
    right: 6px;
    display: flex;
    padding: 4px;
    border: none;
    border-radius: 4px;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .search-clear:hover {
    color: var(--text-color);
  }

  .sort-picker {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .sort-label {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .sort-select {
    width: auto;
    min-width: 7rem;
    padding: 0.35rem 0.5rem;
    font-size: 0.8125rem;
  }

  @media (max-width: 640px) {
    .list-controls {
      flex-wrap: wrap;
    }

    .search-wrapper {
      flex-basis: 100%;
    }
  }

  .tags-manager {
    /* The modal owns the outer padding; this only bounds the working area so
       the two panes keep their proportions on a wide screen. */
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-height: min(60vh, 520px);
  }






  .tags-layout {
    display: grid;
    /* The list is the working surface, the detail pane is the inspector — a
       50/50 split wasted half the dialog on a short card. */
    grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
    gap: 20px;
    margin-top: 16px;
    align-items: start;
    /* Bound the panes so the list scrolls inside the dialog instead of the
       dialog growing past the viewport — with 99 tags it pushed the detail
       pane out of reach entirely. */
    min-height: 0;
  }

  .list-pane {
    /* The list scrolls; the dialog stays put. */
    max-height: min(58vh, 520px);
    overflow-y: auto;
    /* Room for the scrollbar so rows do not shift under the cursor. */
    padding-right: 4px;
  }

  .detail-pane {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px;
    /* Follows the list while you scroll it, so clicking a row 60 tags down
       does not leave the inspector off-screen above you. */
    position: sticky;
    top: 0;
    max-height: min(58vh, 520px);
    overflow-y: auto;
    border: 1px solid var(--border-color);
    border-radius: 12px;
    background: var(--surface-color);
  }

  .select-prompt {
    margin: 0;
    font-size: 13px;
    color: var(--text-secondary);
  }

  /* Shown only below the 768px breakpoint, where the panes swap instead of
     sitting side by side (the GroupsOverview behaviour). */
  .detail-back {
    display: none;
    align-self: flex-start;
    padding: 6px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: transparent;
    color: var(--text-color);
    font-size: 13px;
    cursor: pointer;
    box-shadow: none;
  }

  .error-state {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
    padding: 16px;
    border: 1px solid var(--error-color, #dc2626);
    border-radius: 10px;
    background: rgba(var(--error-color-rgb, 239, 68, 68), 0.06);
  }

  .error-message {
    margin: 0;
    font-size: 13px;
    color: var(--error-color, #dc2626);
  }

  .btn {
    padding: 7px 14px;
    border-radius: 8px;
    border: 1px solid transparent;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    box-shadow: none;
  }

  .btn:hover:not(:disabled) {
    transform: none;
    box-shadow: none;
  }

  .btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .btn-primary {
    background: var(--primary-color);
    color: #fff;
  }

  @media (max-width: 768px) {
    .create-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border-color);
  }

  .create-label {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .create-input {
    flex: 1;
    min-width: 0;
  }

  @media (max-width: 640px) {
    .create-row {
      flex-wrap: wrap;
    }

    .create-input {
      flex-basis: 100%;
      order: 1;
    }
  }

  .list-controls {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  /* Copied from CollectionsPanel so the two panels read as one component. */
  .search-wrapper {
    position: relative;
    display: flex;
    align-items: center;
    flex: 1;
    min-width: 0;
  }

  .search-icon {
    position: absolute;
    left: 10px;
    color: var(--text-secondary);
    pointer-events: none;
  }

  .search-input {
    width: 100%;
    padding: 0.45rem 2rem 0.45rem 2rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 0.875rem;
  }

  .search-input:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  .search-clear {
    position: absolute;
    right: 6px;
    display: flex;
    padding: 4px;
    border: none;
    border-radius: 4px;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .search-clear:hover {
    color: var(--text-color);
  }

  .sort-picker {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .sort-label {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .sort-select {
    width: auto;
    min-width: 7rem;
    padding: 0.35rem 0.5rem;
    font-size: 0.8125rem;
  }

  @media (max-width: 640px) {
    .list-controls {
      flex-wrap: wrap;
    }

    .search-wrapper {
      flex-basis: 100%;
    }
  }

  .tags-manager {
      padding: 16px 12px;
    }

    .tags-layout {
      grid-template-columns: 1fr;
    }

    .tags-layout.has-selection .list-pane {
      display: none;
    }

    .tags-layout:not(.has-selection) .detail-pane {
      display: none;
    }

    .detail-back {
      display: block;
    }
  }
</style>
