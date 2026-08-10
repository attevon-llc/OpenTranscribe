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
   * near matches, the suggested survivor, `awaiting_review`) — those are
   * rendered as received.
   */
  import { createEventDispatcher, onMount } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    acceptTags,
    deleteTags,
    getTagImpact,
    getTagReviewImpact,
    listTagCollisions,
    listTags,
    promoteTags,
    mergeTags,
    rejectTags,
    renameTag,
  } from '$lib/api/tags';
  import type { TagCollisionCluster, TagImpact, TagReviewResult } from '$lib/types/tag';
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

  type Busy = 'rename' | 'merge' | 'delete' | 'accept' | 'reject' | 'promote' | null;

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
  let rejectPreview: TagReviewResult | null = null;
  let renameMergeImpact: TagImpact | null = null;

  /**
   * Every tag the current view knows about, cluster members and near-match
   * suggestions included — the selection can reach all three.
   */
  $: tagIndex = buildIndex(tags, clusters);
  $: selectedEntries = selectedUuids
    .map((uuid) => tagIndex.get(uuid))
    .filter((entry): entry is TagListEntry => entry !== undefined);
  $: rowCount = clusters.length > 0 ? clusters.length : tags.length;
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
    rejectPreview = null;
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
          awaiting_review: filter === 'awaiting_review' || undefined,
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

  function previewReject() {
    clearPreviews();
    runPreview(async () => {
      rejectPreview = await getTagReviewImpact(selectedUuids, 'reject');
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

  function confirmReject() {
    const uuids = [...selectedUuids];
    mutate('reject', uuids, () => rejectTags(uuids).then(() => undefined), 'tags.manager.toast.rejected');
  }

  function acceptSelection() {
    const uuids = [...selectedUuids];
    mutate('accept', uuids, () => acceptTags(uuids).then(() => undefined), 'tags.manager.toast.accepted');
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
        {#if filter === 'awaiting_review'}
          <EmptyState
            icon="✅"
            title={$t('tags.manager.empty.reviewTitle')}
            description={$t('tags.manager.empty.reviewDescription')}
          />
        {:else if filter === 'colliding'}
          <EmptyState
            icon="✨"
            title={$t('tags.manager.empty.collisionsTitle')}
            description={$t('tags.manager.empty.collisionsDescription')}
          />
        {:else if filter === 'unused'}
          <EmptyState
            icon="📄"
            title={$t('tags.manager.empty.unusedTitle')}
            description={$t('tags.manager.empty.unusedDescription')}
          />
        {:else}
          <EmptyState
            icon="🏷️"
            title={$t('tags.manager.empty.allTitle')}
            description={$t('tags.manager.empty.allDescription')}
          />
        {/if}
      {:else}
        <TagList
          {tags}
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
          {rejectPreview}
          {canPromote}
          on:previewMerge={previewMerge}
          on:promote={promoteSelection}
          on:confirmMerge={confirmMerge}
          on:cancelMerge={clearPreviews}
          on:previewDelete={previewDelete}
          on:confirmDelete={confirmDelete}
          on:cancelDelete={clearPreviews}
          on:accept={acceptSelection}
          on:previewReject={previewReject}
          on:confirmReject={confirmReject}
          on:cancelReject={clearPreviews}
          on:clear={clearSelection}
        />
      {:else if selectedEntries.length === 1}
        <TagDetailPanel
          tag={selectedEntries[0]}
          busy={busy === 'merge' ? null : busy}
          {previewLoading}
          {deletePreview}
          {rejectPreview}
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
          on:accept={acceptSelection}
          on:previewReject={previewReject}
          on:confirmReject={confirmReject}
          on:cancelReject={clearPreviews}
        />
      {:else}
        <p class="select-prompt">{$t('tags.manager.selectPrompt')}</p>
      {/if}
    </div>
  </div>
  </div>

  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-ghost" on:click={() => dispatch('close')}>
      {$t('tags.manager.close')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
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
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 20px;
    margin-top: 20px;
    align-items: start;
  }

  .detail-pane {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px;
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
