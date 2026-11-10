<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import {
    getSearchOrphanCounts,
    getStorageOrphanCounts,
    getStorageSweepStatus,
    removeSearchOrphans,
    removeStorageOrphans,
    type SearchOrphanReport,
    type StorageOrphanReport,
  } from '$lib/api/dataIntegrity';
  import { formatFileSize } from '$lib/utils/formatting';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import Spinner from '$components/ui/Spinner.svelte';

  const LISTED_FILE_IDS = 8;
  const POLL_MS = 4000;

  let scanning = true;
  let scanFailed = false;
  let removing = false;
  let search: SearchOrphanReport | null = null;
  let storage: StorageOrphanReport | null = null;

  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    variant: confirmVariant,
  } = confirmDialog;
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  // What can go without anyone overriding a safety limit, and what is held back by one.
  $: searchRemovable = (search?.indices ?? [])
    .filter((i) => !i.refused)
    .reduce((n, i) => n + i.orphaned_docs, 0);
  $: searchHeld = (search?.indices ?? [])
    .filter((i) => i.refused === 'ratio_guard')
    .reduce((n, i) => n + i.orphaned_docs, 0);
  $: storageRemovable = storage && !storage.refused ? storage.orphan_files : 0;
  $: storageHeld = storage?.refused === 'ratio_guard';
  $: emptyDatabase =
    storage?.refused === 'empty_valid_set' ||
    (search?.indices ?? []).some((i) => i.refused === 'empty_valid_set');
  $: anyOrphans = searchRemovable + searchHeld > 0 || (storage?.orphan_files ?? 0) > 0;
  $: canRemove = !removing && searchRemovable + storageRemovable > 0;
  $: canOverride = !removing && (searchHeld > 0 || storageHeld);
  $: storageObjects = storageRemovable > 0 ? (storage?.orphan_objects ?? 0) : 0;
  $: storageBytes = storageRemovable > 0 ? (storage?.orphan_bytes ?? 0) : 0;
  $: heldFiles = storageHeld ? (storage?.orphan_files ?? 0) : 0;
  $: heldBytes = storageHeld ? (storage?.orphan_bytes ?? 0) : 0;
  $: shownFileIds = (storage?.orphans ?? []).slice(0, LISTED_FILE_IDS).map((o) => o.file_id);
  $: moreFiles = Math.max(0, (storage?.orphan_files ?? 0) - shownFileIds.length);

  function describe(docs: number, files: number, objects: number, bytes: number): string {
    const params = { docs, files, objects, size: formatFileSize(bytes) };
    if (docs > 0 && files > 0) return $t('settings.dataIntegrity.orphans.confirmBoth', params);
    if (docs > 0) return $t('settings.dataIntegrity.orphans.confirmSearchOnly', params);
    return $t('settings.dataIntegrity.orphans.confirmStorageOnly', params);
  }

  function askRemove() {
    confirmDialog.request(
      $t('settings.dataIntegrity.orphans.confirmTitle'),
      describe(searchRemovable, storageRemovable, storageObjects, storageBytes),
      () => run(false),
      { confirmText: $t('settings.dataIntegrity.orphans.confirmAction'), variant: 'danger' }
    );
  }

  // Overriding a safety limit takes two confirmations; the second is requested from the first's
  // callback, which the helper runs only after it has closed and cleared the first.
  function askOverride() {
    confirmDialog.request(
      $t('settings.dataIntegrity.orphans.overrideTitle'),
      $t('settings.dataIntegrity.orphans.overrideMessage', {
        docs: searchHeld,
        files: heldFiles,
        size: formatFileSize(heldBytes),
      }),
      askFinal,
      { confirmText: $t('settings.dataIntegrity.orphans.overrideConfirm'), variant: 'danger' }
    );
  }

  function askFinal() {
    confirmDialog.request(
      $t('settings.dataIntegrity.orphans.finalTitle'),
      $t('settings.dataIntegrity.orphans.finalMessage'),
      () => run(true),
      { confirmText: $t('settings.dataIntegrity.orphans.finalConfirm'), variant: 'danger' }
    );
  }

  async function scan(quiet = false) {
    if (!quiet) scanning = true;
    scanFailed = false;
    try {
      [search, storage] = await Promise.all([getSearchOrphanCounts(), getStorageOrphanCounts()]);
    } catch (err) {
      console.error('Failed to scan for orphaned data:', err);
      scanFailed = true;
    } finally {
      scanning = false;
    }
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  function pollUntilDone() {
    stopPolling();
    pollTimer = setInterval(() => void checkSweepDone(), POLL_MS);
  }

  async function checkSweepDone(): Promise<void> {
    try {
      const status = await getStorageSweepStatus();
      if (!status.running) {
        stopPolling();
        removing = false;
        await scan(true);
        toastStore.success($t('settings.dataIntegrity.orphans.completed'));
      }
    } catch (err) {
      console.error('Failed to read the storage sweep status:', err);
    }
  }

  async function run(force: boolean) {
    removing = true;
    try {
      const wantSearch = force ? searchHeld + searchRemovable > 0 : searchRemovable > 0;
      const wantStorage = force ? (storage?.orphan_files ?? 0) > 0 : storageRemovable > 0;
      const results = await Promise.all([
        wantSearch ? removeSearchOrphans(force) : null,
        wantStorage ? removeStorageOrphans(force) : null,
      ]);
      if (results.some((r) => r?.status === 'already_running')) {
        toastStore.info($t('settings.dataIntegrity.orphans.alreadyRunning'));
      } else {
        toastStore.success($t('settings.dataIntegrity.orphans.started'));
      }
      pollUntilDone();
    } catch (err) {
      console.error('Failed to start the orphaned-data removal:', err);
      toastStore.error($t('settings.dataIntegrity.orphans.startFailed'));
      removing = false;
    }
  }

  function handleComplete(event: Event) {
    // The search sweep reports over the WebSocket; the storage sweep also polls (above).
    const detail = (event as CustomEvent).detail;
    if (detail?.scope === 'storage') return;
    void scan(true);
  }

  onMount(() => {
    void scan();
    window.addEventListener('data-integrity-complete', handleComplete);
  });

  onDestroy(() => {
    stopPolling();
    window.removeEventListener('data-integrity-complete', handleComplete);
  });
</script>

<div class="orphans" data-testid="orphaned-data-panel">
  <div class="orphans-head">
    <h4 class="orphans-title">{$t('settings.dataIntegrity.orphans.title')}</h4>
    <button class="btn btn-secondary btn-sm" on:click={() => scan()} disabled={scanning || removing}>
      {$t('settings.dataIntegrity.orphans.rescan')}
    </button>
  </div>
  <p class="orphans-desc">{$t('settings.dataIntegrity.orphans.description')}</p>

  {#if scanning}
    <div class="orphans-state"><Spinner size="small" /> {$t('settings.dataIntegrity.orphans.scanning')}</div>
  {:else if scanFailed}
    <p class="orphans-error" role="alert">{$t('settings.dataIntegrity.orphans.scanFailed')}</p>
  {:else}
    <div class="orphans-grid">
      <div class="orphan-card">
        <div class="orphan-card-title">{$t('settings.dataIntegrity.orphans.searchTitle')}</div>
        {#if search?.unavailable}
          <p class="muted">{$t('settings.dataIntegrity.orphans.searchUnavailable')}</p>
        {:else}
          <div class="orphan-row">
            <span>{$t('settings.dataIntegrity.orphans.docsLabel')}</span>
            <span class="orphan-val" data-testid="orphan-search-docs">{searchRemovable + searchHeld}</span>
          </div>
          {#each (search?.indices ?? []).filter((i) => i.orphaned_docs > 0) as idx (idx.name)}
            <div class="orphan-row sub">
              <span class="mono">{idx.name}</span>
              <span class="orphan-val">{idx.orphaned_docs}</span>
            </div>
          {/each}
        {/if}
      </div>

      <div class="orphan-card">
        <div class="orphan-card-title">{$t('settings.dataIntegrity.orphans.storageTitle')}</div>
        <div class="orphan-row">
          <span>{$t('settings.dataIntegrity.orphans.filesLabel')}</span>
          <span class="orphan-val" data-testid="orphan-storage-files">{storage?.orphan_files ?? 0}</span>
        </div>
        <div class="orphan-row">
          <span>{$t('settings.dataIntegrity.orphans.objectsLabel')}</span>
          <span class="orphan-val">{storage?.orphan_objects ?? 0}</span>
        </div>
        <div class="orphan-row">
          <span>{$t('settings.dataIntegrity.orphans.sizeLabel')}</span>
          <span class="orphan-val" data-testid="orphan-storage-size">{formatFileSize(storage?.orphan_bytes ?? 0)}</span>
        </div>
        {#if shownFileIds.length > 0}
          <p class="muted ids">
            {$t('settings.dataIntegrity.orphans.fileIds', { ids: shownFileIds.join(', ') })}
            {#if moreFiles > 0}
              {$t('settings.dataIntegrity.orphans.fileIdsMore', { count: moreFiles })}
            {/if}
          </p>
        {/if}
        {#if (storage?.skipped_recent_files ?? 0) > 0}
          <p class="muted">
            {$t('settings.dataIntegrity.orphans.skippedRecent', { count: storage?.skipped_recent_files ?? 0 })}
          </p>
        {/if}
      </div>
    </div>

    {#if emptyDatabase}
      <div class="notice notice-warn" role="status" data-testid="orphan-empty-db">
        {$t('settings.dataIntegrity.orphans.emptyDb')}
      </div>
    {/if}

    {#if searchHeld > 0 || storageHeld}
      <div class="notice notice-warn" role="status" data-testid="orphan-guard">
        <strong>{$t('settings.dataIntegrity.orphans.guardTitle')}</strong>
        {#if searchHeld > 0}
          <p>{$t('settings.dataIntegrity.orphans.guardSearch', { docs: searchHeld })}</p>
        {/if}
        {#if storageHeld}
          <p>{$t('settings.dataIntegrity.orphans.guardStorage', { objects: storage?.orphan_objects ?? 0 })}</p>
        {/if}
        <p>{$t('settings.dataIntegrity.orphans.guardExplain')}</p>
        <button
          class="btn btn-secondary btn-sm"
          on:click={askOverride}
          disabled={!canOverride}
        >
          {$t('settings.dataIntegrity.orphans.guardOverrideButton')}
        </button>
      </div>
    {/if}

    <div class="orphans-actions">
      <button
        class="btn btn-danger"
        on:click={askRemove}
        disabled={!canRemove}
        aria-describedby="orphans-hint"
      >
        {#if removing}
          <Spinner size="small" color="white" />
          {$t('settings.dataIntegrity.orphans.removing')}
        {:else}
          {$t('settings.dataIntegrity.orphans.removeButton')}
        {/if}
      </button>
      {#if !anyOrphans}
        <span id="orphans-hint" class="muted" data-testid="orphan-none">
          {$t('settings.dataIntegrity.orphans.nothingToRemove')}
        </span>
      {:else if !canRemove && !removing}
        <span id="orphans-hint" class="muted">{$t('settings.dataIntegrity.orphans.onlyHeldBack')}</span>
      {/if}
    </div>
  {/if}
</div>

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
  .orphans {
    margin-bottom: var(--space-4);
    padding: var(--space-4);
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
  }

  .orphans-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .orphans-title {
    margin: 0;
    font-size: 0.9rem;
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .orphans-desc {
    margin: var(--space-1) 0 var(--space-3);
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .orphans-state {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .orphans-error {
    margin: 0;
    font-size: 0.8rem;
    color: var(--error-color);
  }

  .orphans-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: var(--space-3);
  }

  .orphan-card {
    padding: var(--space-3);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .orphan-card-title {
    margin-bottom: var(--space-1-5);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .orphan-row {
    display: flex;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-0-5) 0;
  }

  .orphan-row.sub {
    font-size: 0.72rem;
    color: var(--text-muted);
  }

  .orphan-val {
    font-weight: var(--font-weight-semibold);
    font-family: monospace;
    color: var(--text-color);
  }

  .mono {
    font-family: monospace;
    overflow-wrap: anywhere;
  }

  .muted {
    margin: var(--space-1-5) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .ids {
    overflow-wrap: anywhere;
  }

  .notice {
    margin-top: var(--space-3);
    padding: var(--space-3);
    border-radius: var(--radius-md);
    font-size: 0.8rem;
    color: var(--text-color);
  }

  .notice p {
    margin: var(--space-1) 0;
  }

  .notice-warn {
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(217, 119, 6, 0.5);
  }

  .orphans-actions {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-3);
    margin-top: var(--space-3);
  }
</style>
