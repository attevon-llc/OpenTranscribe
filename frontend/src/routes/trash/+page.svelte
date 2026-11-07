<!--
  /trash (issue #1101): files moved to the trash, restorable until their purge date.

  Coordinator: fetches `GET /files/trash`, runs restore / delete forever / empty trash, and
  reloads on the `file-trashed` / `file-restored` window events ($lib/websocket/frameRouting).
  Who sees which rows (own trash, an organization's trash for its administrator) and which
  buttons apply are the server's answer; the page never widens them.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { resolve } from '$app/paths';
  import { t } from '$stores/locale';
  import { user } from '$stores/auth';
  import { toastStore } from '$stores/toast';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ListRowSkeleton from '$components/ui/ListRowSkeleton.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import SearchPagination from '$components/search/SearchPagination.svelte';
  import TrashTable from '$components/trash/TrashTable.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import {
    emptyTrash,
    listTrash,
    purgeFile,
    restoreFile,
    TRASH_PAGE_SIZE,
    type TrashedFile,
    type TrashPage,
  } from '$lib/api/trash';
  import { showsOtherOwners } from '$lib/trash/rowState';
  import { trashRefusalMessage } from '$lib/trash/refusal';

  let data: TrashPage | null = null;
  let loading = true;
  let loadFailed = false;
  let page = 1;
  let busy: string[] = [];
  let emptying = false;
  let now = Date.now();

  const confirmDialog = createConfirmDialog();
  const { isOpen, title, message, confirmText } = confirmDialog;

  $: items = data?.items ?? [];
  $: totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  $: anyPurgeable = items.some((f) => f.can_purge);
  $: showOwner = showsOtherOwners(items, $user?.uuid ?? null);

  async function load() {
    try {
      const next = await listTrash(page, TRASH_PAGE_SIZE);
      // A page emptied by the last restore or purge: step back rather than show nothing.
      if (next.items.length === 0 && page > 1 && next.total > 0) {
        page -= 1;
        return load();
      }
      data = next;
      now = Date.now();
      loadFailed = false;
    } catch (err) {
      console.error('Error loading the trash:', err);
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  function setBusy(uuid: string, on: boolean) {
    busy = on ? [...busy, uuid] : busy.filter((id) => id !== uuid);
  }

  const nameOf = (file: TrashedFile) => file.title || file.filename;

  async function restore(file: TrashedFile) {
    setBusy(file.uuid, true);
    try {
      await restoreFile(file.uuid);
      toastStore.success($t('trash.restored', { name: nameOf(file) }));
    } catch (err) {
      toastStore.error(trashRefusalMessage(err, $t, $t('trash.restoreFailed')));
    } finally {
      setBusy(file.uuid, false);
      await load();
    }
  }

  function askPurge(file: TrashedFile) {
    confirmDialog.request(
      $t('trash.deleteForeverTitle'),
      $t('trash.deleteForeverMessage', { name: nameOf(file) }),
      () => purge(file),
      $t('trash.deleteForever')
    );
  }

  async function purge(file: TrashedFile) {
    setBusy(file.uuid, true);
    try {
      await purgeFile(file.uuid);
      toastStore.success($t('trash.purgeQueued'));
    } catch (err) {
      toastStore.error(trashRefusalMessage(err, $t, $t('trash.purgeFailed')));
    } finally {
      setBusy(file.uuid, false);
      await load();
    }
  }

  function askEmpty() {
    confirmDialog.request(
      $t('trash.emptyConfirmTitle'),
      $t('trash.emptyConfirmMessage'),
      doEmpty,
      $t('trash.emptyTrash')
    );
  }

  async function doEmpty() {
    emptying = true;
    try {
      await emptyTrash();
      toastStore.success($t('trash.emptyQueued'));
    } catch (err) {
      toastStore.error(trashRefusalMessage(err, $t, $t('trash.emptyFailed')));
    } finally {
      emptying = false;
      await load();
    }
  }

  function changePage(event: CustomEvent<number>) {
    page = event.detail;
    loading = true;
    void load();
  }

  const reload = () => void load();

  onMount(() => {
    void load();
    window.addEventListener('file-trashed', reload);
    window.addEventListener('file-restored', reload);
  });

  onDestroy(() => {
    if (typeof window === 'undefined') return;
    window.removeEventListener('file-trashed', reload);
    window.removeEventListener('file-restored', reload);
  });
</script>

<svelte:head>
  <title>{$t('trash.title')} - OpenTranscribe</title>
</svelte:head>

<div class="trash-page">
  <div class="page-header">
    <a href={resolve('/')} class="back-to-gallery" title={$t('nav.backToGallery')} aria-label={$t('nav.backToGallery')}>
      <MenuIcon name="chevron-left" size={18} />
    </a>
    <h1>{$t('trash.title')}</h1>
    {#if anyPurgeable}
      <button
        type="button"
        class="btn btn-danger empty-trash-btn"
        data-testid="trash-empty"
        disabled={emptying}
        on:click={askEmpty}
      >
        <MenuIcon name="trash" />
        {$t('trash.emptyTrash')}
      </button>
    {/if}
  </div>

  {#if data}
    <p class="intro" data-testid="trash-intro">
      {$t('trash.intro', { count: data.retention_days })}
    </p>
  {/if}

  {#if loading && !data}
    <ListRowSkeleton />
  {:else if loadFailed && !data}
    <EmptyState icon="warning" title={$t('trash.loadFailed')}>
      <button type="button" class="btn btn-secondary" on:click={reload}>{$t('common.retry')}</button>
    </EmptyState>
  {:else if items.length === 0}
    <EmptyState icon="trash" title={$t('trash.emptyTitle')} description={$t('trash.emptyDescription')} />
  {:else}
    <TrashTable
      {items}
      {showOwner}
      {busy}
      {now}
      currentUserUuid={$user?.uuid ?? null}
      on:restore={(e) => restore(e.detail)}
      on:purge={(e) => askPurge(e.detail)}
    />
    {#if !anyPurgeable}
      <p class="note" data-testid="trash-auto-purge-note">{$t('trash.autoPurgeNote')}</p>
    {/if}
    {#if totalPages > 1}
      <SearchPagination {page} {totalPages} on:pageChange={changePage} />
    {/if}
  {/if}
</div>

<ConfirmationModal
  bind:isOpen={$isOpen}
  title={$title}
  message={$message}
  confirmText={$confirmText}
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .trash-page {
    max-width: 1100px;
    margin: 0 auto;
    padding: var(--space-6) var(--space-4);
    box-sizing: border-box;
  }

  .page-header {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    margin-block-end: var(--space-2);
  }

  .page-header h1 {
    font-size: var(--font-size-2xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin: 0;
  }

  .back-to-gallery {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    inline-size: 32px;
    block-size: 32px;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    text-decoration: none;
  }

  .back-to-gallery:hover {
    background: var(--hover-color);
    color: var(--text-color);
  }

  .empty-trash-btn {
    margin-inline-start: auto;
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
  }

  .intro,
  .note {
    color: var(--text-secondary);
    font-size: var(--font-size-md);
    margin-block: 0 var(--space-4);
    max-inline-size: var(--measure-prose);
  }

  .note {
    margin-block: var(--space-4) 0;
    font-size: var(--font-size-sm);
  }
</style>
