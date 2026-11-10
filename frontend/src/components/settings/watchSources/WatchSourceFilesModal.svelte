<script lang="ts">
  import { SvelteSet } from 'svelte/reactivity';
  import { WATCH_FILE_STATUS_KEYS } from '$lib/i18n/keyMaps';
  /**
   * Per-file import history for one watch source, with retry and delete-record (#489).
   *
   * The coordinator for this feature: it owns the fetch, the filters, the paging and
   * the in-flight state; `WatchSourceFilesTable` renders and dispatches.
   *
   * **Retry queues, it does not import.** The backend resets the rows and dispatches
   * one scan; that scan may find another already running (a Redis lock per source),
   * may not reach this file within `max_imports_per_scan`, and only re-imports files
   * still present at their remote path. So the copy says "queued" and the row shows
   * `pending` — and this modal subscribes to the `watch-source-scan` event so the
   * REAL outcome replaces it as soon as a scan completes.
   */
  import { createEventDispatcher, onDestroy } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import SearchPagination from '$components/search/SearchPagination.svelte';
  import WatchSourceFilesTable from './WatchSourceFilesTable.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { createDebouncedHandler } from '$lib/utils/debounce';
  import {
    getWatchSourceFiles,
    retryWatchSourceFiles,
    deleteWatchSourceFile,
    bulkDeleteWatchSourceFiles,
    WATCH_FILE_STATUSES,
    type WatchSource,
    type WatchSourceFile,
    type WatchSourceFileActionResponse,
  } from '$lib/api/watchSourcesApi';

  export let show = false;
  export let source: WatchSource | null = null;

  const PAGE_SIZE = 50;
  const dispatch = createEventDispatcher<{ close: void; changed: void }>();

  let files: WatchSourceFile[] = [];
  let total = 0;
  let page = 1;
  let statusFilter = '';
  let query = '';
  let loading = false;
  let busyUuids = new Set<string>();
  let selectedUuids = new Set<string>();
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    variant: confirmVariant,
  } = confirmDialog;
  /** Set once the user has changed something, so the parent refreshes its counts. */
  let dirty = false;
  const debouncedSearch = createDebouncedHandler(() => {
    page = 1;
    void load();
  }, 300);
  let loadedFor: string | null = null;

  $: totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // Load when the modal opens for a source, and reset per-source state so a second
  // source never inherits the first's filters or selection.
  function syncSource(isShown: boolean, current: typeof source) {
    if (!isShown) {
      loadedFor = null;
    } else if (current && loadedFor !== current.uuid) {
      loadedFor = current.uuid;
      page = 1;
      statusFilter = '';
      query = '';
      selectedUuids = new Set();
      dirty = false;
      void load();
    }
  }
  $: syncSource(show, source);

  async function load() {
    if (!source) return;
    loading = true;
    try {
      const data = await getWatchSourceFiles(
        source.uuid,
        page,
        PAGE_SIZE,
        statusFilter || undefined,
        query.trim() || undefined
      );
      files = data.files ?? [];
      total = data.total ?? 0;
      // Drop selections for rows no longer on screen, so a bulk action can never
      // apply to something the user cannot see.
      const visible = new Set(files.map((f) => f.uuid));
      selectedUuids = new Set([...selectedUuids].filter((u) => visible.has(u)));
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.files.loadFailed')));
    } finally {
      loading = false;
    }
  }

  /**
   * A scan finished somewhere in the backend — if it was ours, the rows that were
   * left `pending` by a retry now have real outcomes worth showing.
   */
  function onScanEvent(event: Event) {
    const detail = (event as CustomEvent<{ source_uuid?: string }>).detail;
    if (show && source && detail?.source_uuid === source.uuid) void load();
  }

  if (typeof window !== 'undefined') {
    window.addEventListener('watch-source-scan', onScanEvent);
  }
  onDestroy(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('watch-source-scan', onScanEvent);
    }
    debouncedSearch.cleanup();
  });

  function onFilterChange() {
    page = 1;
    void load();
  }

  function onSearchInput() {
    debouncedSearch.trigger();
  }

  function onPageChange(event: CustomEvent<number>) {
    page = event.detail;
    selectedUuids = new Set();
    void load();
  }

  function markBusy(uuids: string[], busy: boolean) {
    const next = new SvelteSet(busyUuids);
    for (const uuid of uuids) {
      if (busy) next.add(uuid);
      else next.delete(uuid);
    }
    busyUuids = next;
  }

  /**
   * Report a batch honestly: some rows can be refused while others succeed, and
   * saying "done" over a partial result is how an operator comes to believe a file
   * was re-queued when it was not.
   */
  function reportBatch(response: WatchSourceFileActionResponse, successKey: string) {
    const succeeded = response.results.filter((r) => r.success);
    const failed = response.results.filter((r) => !r.success);
    if (succeeded.length) {
      toastStore.success($t(successKey, { count: succeeded.length }));
    }
    for (const result of succeeded) {
      if (result.warning) toastStore.warning(result.warning, 10000);
    }
    for (const result of failed) {
      toastStore.error(result.error || $t('settings.watchSources.files.actionFailed'), 8000);
    }
  }

  async function retry(uuids: string[]) {
    if (!source || uuids.length === 0) return;
    markBusy(uuids, true);
    try {
      const response = await retryWatchSourceFiles(source.uuid, uuids);
      reportBatch(response, 'settings.watchSources.files.retryQueued');
      dirty = true;
      selectedUuids = new Set();
      await load();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.files.retryFailed')));
    } finally {
      markBusy(uuids, false);
    }
  }

  function askDeleteOne(target: WatchSourceFile) {
    confirmDialog.request(
      $t('settings.watchSources.files.deleteConfirmTitle'),
      $t('settings.watchSources.files.deleteConfirmMessage', { name: target.filename }),
      () => doDeleteOne(target),
      { confirmText: $t('common.delete'), variant: 'danger' }
    );
  }

  function askDeleteSelected() {
    confirmDialog.request(
      $t('settings.watchSources.files.deleteSelectedConfirmTitle'),
      $t('settings.watchSources.files.deleteSelectedConfirmMessage', {
        count: selectedUuids.size,
      }),
      doDeleteSelected,
      { confirmText: $t('common.delete'), variant: 'danger' }
    );
  }

  async function doDeleteOne(target: WatchSourceFile) {
    if (!source) return;
    markBusy([target.uuid], true);
    try {
      await deleteWatchSourceFile(source.uuid, target.uuid);
      toastStore.success($t('settings.watchSources.files.recordDeleted'));
      dirty = true;
      // Deleting the last row on a page would otherwise leave an empty table under a
      // live pager, which reads as "the search broke".
      if (files.length === 1 && page > 1) page -= 1;
      await load();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.files.deleteFailed')));
    } finally {
      markBusy([target.uuid], false);
    }
  }

  async function doDeleteSelected() {
    if (!source) return;
    const uuids = [...selectedUuids];
    if (uuids.length === 0) return;
    markBusy(uuids, true);
    try {
      const response = await bulkDeleteWatchSourceFiles(source.uuid, uuids);
      reportBatch(response, 'settings.watchSources.files.recordsDeleted');
      dirty = true;
      selectedUuids = new Set();
      if (uuids.length >= files.length && page > 1) page -= 1;
      await load();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.files.deleteFailed')));
    } finally {
      markBusy(uuids, false);
    }
  }

  function toggleSelect(event: CustomEvent<string>) {
    const next = new SvelteSet(selectedUuids);
    if (next.has(event.detail)) next.delete(event.detail);
    else next.add(event.detail);
    selectedUuids = next;
  }

  function toggleSelectAll(event: CustomEvent<boolean>) {
    selectedUuids = event.detail ? new Set(files.map((f) => f.uuid)) : new Set();
  }

  function handleClose() {
    if (dirty) dispatch('changed');
    dispatch('close');
  }
</script>

<BaseModal isOpen={show} onClose={handleClose} size="xl">
  <svelte:fragment slot="header">
    <h2 class="modal-title">
      {$t('settings.watchSources.files.title', { name: source?.name ?? '' })}
    </h2>
  </svelte:fragment>

  <div class="files-body">
    <div class="filters">
      <input
        class="form-input search"
        type="search"
        bind:value={query}
        on:input={onSearchInput}
        placeholder={$t('settings.watchSources.files.searchPlaceholder')}
        aria-label={$t('settings.watchSources.files.searchPlaceholder')}
      />
      <select
        class="form-input status-select"
        bind:value={statusFilter}
        on:change={onFilterChange}
        aria-label={$t('settings.watchSources.files.columnStatus')}
      >
        <option value="">{$t('settings.watchSources.files.allStatuses')}</option>
        {#each WATCH_FILE_STATUSES as status (status)}
          <option value={status}>
            {$t(WATCH_FILE_STATUS_KEYS[status])}
          </option>
        {/each}
      </select>
    </div>

    {#if selectedUuids.size > 0}
      <div class="bulk-bar">
        <span>{$t('settings.watchSources.files.selectedCount', { count: selectedUuids.size })}</span>
        <button class="btn btn-secondary" on:click={() => retry([...selectedUuids])}>
          {$t('settings.watchSources.files.retrySelected')}
        </button>
        <button class="btn btn-danger" on:click={askDeleteSelected}>
          {$t('settings.watchSources.files.deleteSelected')}
        </button>
      </div>
    {/if}

    {#if loading}
      <div class="files-loading"><Spinner label={$t('common.loading')} /></div>
    {:else if files.length === 0}
      <EmptyState
        title={$t('settings.watchSources.files.emptyTitle')}
        description={statusFilter || query.trim()
          ? $t('settings.watchSources.files.emptyFiltered')
          : $t('settings.watchSources.files.emptyDescription')}
      />
    {:else}
      <WatchSourceFilesTable
        {files}
        {busyUuids}
        {selectedUuids}
        hasAgeLimit={source?.skip_files_older_than_days != null}
        on:retry={(e) => retry([e.detail.uuid])}
        on:delete={(e) => askDeleteOne(e.detail)}
        on:toggleSelect={toggleSelect}
        on:toggleSelectAll={toggleSelectAll}
      />
      {#if totalPages > 1}
        <SearchPagination {page} {totalPages} on:pageChange={onPageChange} />
      {/if}
    {/if}
  </div>

  <svelte:fragment slot="footer">
    <button class="btn btn-secondary" on:click={handleClose}>{$t('common.close')}</button>
  </svelte:fragment>
</BaseModal>

<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmLabel}
  variant={$confirmVariant}
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .files-body {
    min-height: 220px;
  }
  .filters {
    display: flex;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }
  .search {
    flex: 1;
  }
  .status-select {
    width: auto;
    min-width: 180px;
  }
  .bulk-bar {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-2-5);
    margin-bottom: var(--space-2-5);
    border-radius: var(--radius-md);
    background: var(--button-hover);
    font-size: 0.85rem;
  }
  .files-loading {
    display: flex;
    justify-content: center;
    padding: var(--space-8);
  }
  .modal-title {
    margin: 0;
    font-size: 1.1rem;
  }
</style>
