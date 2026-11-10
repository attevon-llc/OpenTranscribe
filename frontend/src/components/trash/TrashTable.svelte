<!--
  TrashTable.svelte: the rows of the trash page (issue #1101). Presentational: the page owns
  fetching and the actions; this renders what the server sent and dispatches `restore` /
  `purge` with the row. A button shows only when the server says the caller may use it
  (`can_restore`, `can_purge`), and Restore is not offered once the file is being deleted
  (the server would answer `NOT_RESTORABLE`).
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import HoldBadge from '$components/ui/HoldBadge.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { t, locale } from '$stores/locale';
  import { formatDateTime } from '$lib/utils/formatting';
  import type { TrashedFile } from '$lib/api/trash';
  import { canOfferRestore, trashRowState } from '$lib/trash/rowState';

  export let items: TrashedFile[] = [];
  /** Show the owner column (an organization or instance-wide trash). */
  export let showOwner = false;
  export let currentUserUuid: string | null = null;
  /** Rows with an action in flight: their buttons are disabled. */
  export let busy: readonly string[] = [];
  /** Milliseconds since the epoch; passed in so a test can pin it. */
  export let now: number = Date.now();

  const dispatch = createEventDispatcher<{ restore: TrashedFile; purge: TrashedFile }>();

  const HELD = { held: true, held_by: [], hold_uuids: [] };

  function displayName(file: TrashedFile): string {
    return file.title || file.filename;
  }

  function deletedBy(file: TrashedFile): string {
    if (file.deleted_by_uuid && file.deleted_by_uuid === currentUserUuid) return $t('trash.you');
    return file.deleted_by_name || $t('trash.unknownPerson');
  }
</script>

<div class="trash-table-wrap">
  <table class="trash-table" data-testid="trash-table">
    <thead>
      <tr>
        <th scope="col">{$t('trash.column.file')}</th>
        {#if showOwner}
          <th scope="col">{$t('trash.column.owner')}</th>
        {/if}
        <th scope="col">{$t('trash.column.deletedBy')}</th>
        <th scope="col">{$t('trash.column.deletedAt')}</th>
        <th scope="col">{$t('trash.column.remaining')}</th>
        <th scope="col" class="col-actions">{$t('trash.column.actions')}</th>
      </tr>
    </thead>
    <tbody>
      {#each items as file (file.uuid)}
        {@const state = trashRowState(file, now)}
        <tr data-testid="trash-row" data-file-uuid={file.uuid} data-state={state.kind}>
          <td class="col-file" dir="auto">{displayName(file)}</td>
          {#if showOwner}
            <td dir="auto">{file.owner_name || $t('trash.unknownPerson')}</td>
          {/if}
          <td dir="auto">{deletedBy(file)}</td>
          <td>{formatDateTime(file.deleted_at, '', $locale)}</td>
          <td class="col-remaining">
            {#if state.kind === 'held'}
              <span class="remaining-held">
                <HoldBadge hold={HELD} />
                <span class="remaining-text">{$t('trash.heldKept')}</span>
              </span>
            {:else if state.kind === 'due'}
              <span class="remaining-text remaining-due">{$t('trash.due')}</span>
            {:else}
              <span
                class="remaining-text"
                title={formatDateTime(file.purge_after, '', $locale)}
              >
                {$t('trash.remaining', { count: state.days })}
              </span>
            {/if}
          </td>
          <td class="col-actions">
            <div class="row-actions">
              {#if canOfferRestore(file, now)}
                <button
                  type="button"
                  class="btn btn-secondary btn-sm"
                  data-testid="trash-restore"
                  aria-label={$t('trash.restoreLabel', { name: displayName(file) })}
                  disabled={busy.includes(file.uuid)}
                  on:click={() => dispatch('restore', file)}
                >
                  <MenuIcon name="restore" />
                  {$t('trash.restore')}
                </button>
              {/if}
              {#if file.can_purge && state.kind !== 'due'}
                <button
                  type="button"
                  class="btn btn-danger btn-sm"
                  data-testid="trash-purge"
                  aria-label={$t('trash.deleteForeverLabel', { name: displayName(file) })}
                  disabled={busy.includes(file.uuid)}
                  on:click={() => dispatch('purge', file)}
                >
                  <MenuIcon name="trash" />
                  {$t('trash.deleteForever')}
                </button>
              {/if}
            </div>
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  .trash-table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--surface-color);
  }

  .trash-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-md);
  }

  th,
  td {
    padding-block: var(--space-2-5);
    padding-inline: var(--space-3-5);
    text-align: start;
    vertical-align: middle;
    border-block-end: 1px solid var(--border-color);
  }

  tbody tr:last-child td {
    border-block-end: none;
  }

  th {
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    background: var(--background-color);
    white-space: nowrap;
  }

  td {
    color: var(--text-color);
  }

  .col-file {
    font-weight: var(--font-weight-medium);
    overflow-wrap: anywhere;
    min-inline-size: 12rem;
  }

  .col-actions {
    text-align: end;
    white-space: nowrap;
  }

  .row-actions {
    display: inline-flex;
    gap: var(--space-2);
    justify-content: flex-end;
  }

  .row-actions .btn {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
  }

  .remaining-held {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }

  .remaining-text {
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .remaining-due {
    color: var(--error-text);
  }
</style>
