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
  import { formatFileSize } from '$lib/utils/metadataMapper';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import Spinner from '../ui/Spinner.svelte';

  const LISTED_FILE_IDS = 8;
  const POLL_MS = 4000;

  let scanning = true;
  let scanFailed = false;
  let removing = false;
  let search: SearchOrphanReport | null = null;
  let storage: StorageOrphanReport | null = null;

  let showConfirm = false;
  let showOverride = false;
  let showFinal = false;
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

  $: confirmMessage = describe(searchRemovable, storageRemovable, storageObjects, storageBytes);
  $: overrideMessage = $t('settings.dataIntegrity.orphans.overrideMessage', {
    docs: searchHeld,
    files: heldFiles,
    size: formatFileSize(heldBytes),
  });

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

  async function pollUntilDone() {
    stopPolling();
    pollTimer = setInterval(async () => {
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
    }, POLL_MS);
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
      await pollUntilDone();
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
    scan(true);
  }

  onMount(() => {
    scan();
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
    <button class="btn btn-secondary btn-small" on:click={() => scan()} disabled={scanning || removing}>
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
          class="btn btn-secondary btn-small"
          on:click={() => (showOverride = true)}
          disabled={!canOverride}
        >
          {$t('settings.dataIntegrity.orphans.guardOverrideButton')}
        </button>
      </div>
    {/if}

    <div class="orphans-actions">
      <button
        class="btn btn-danger"
        on:click={() => (showConfirm = true)}
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
  bind:isOpen={showConfirm}
  title={$t('settings.dataIntegrity.orphans.confirmTitle')}
  message={confirmMessage}
  confirmText={$t('settings.dataIntegrity.orphans.confirmAction')}
  confirmButtonClass="confirm-button delete-confirm"
  on:confirm={() => run(false)}
/>

<ConfirmationModal
  bind:isOpen={showOverride}
  title={$t('settings.dataIntegrity.orphans.overrideTitle')}
  message={overrideMessage}
  confirmText={$t('settings.dataIntegrity.orphans.overrideConfirm')}
  confirmButtonClass="confirm-button delete-confirm"
  on:confirm={() => (showFinal = true)}
/>

<ConfirmationModal
  bind:isOpen={showFinal}
  title={$t('settings.dataIntegrity.orphans.finalTitle')}
  message={$t('settings.dataIntegrity.orphans.finalMessage')}
  confirmText={$t('settings.dataIntegrity.orphans.finalConfirm')}
  confirmButtonClass="confirm-button delete-confirm"
  on:confirm={() => run(true)}
/>

<style>
  .orphans {
    margin-bottom: 1rem;
    padding: 1rem;
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .orphans-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .orphans-title {
    margin: 0;
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .orphans-desc {
    margin: 0.25rem 0 0.75rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .orphans-state {
    display: flex;
    align-items: center;
    gap: 0.5rem;
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
    gap: 0.75rem;
  }

  .orphan-card {
    padding: 0.75rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .orphan-card-title {
    margin-bottom: 0.375rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .orphan-row {
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.125rem 0;
  }

  .orphan-row.sub {
    font-size: 0.72rem;
    color: var(--text-muted);
  }

  .orphan-val {
    font-weight: 600;
    font-family: monospace;
    color: var(--text-color);
  }

  .mono {
    font-family: monospace;
    overflow-wrap: anywhere;
  }

  .muted {
    margin: 0.375rem 0 0;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .ids {
    overflow-wrap: anywhere;
  }

  .notice {
    margin-top: 0.75rem;
    padding: 0.75rem;
    border-radius: 6px;
    font-size: 0.8rem;
    color: var(--text-color);
  }

  .notice p {
    margin: 0.25rem 0;
  }

  .notice-warn {
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(217, 119, 6, 0.5);
  }

  .orphans-actions {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.75rem;
    margin-top: 0.75rem;
  }
</style>
