<script lang="ts">
  /**
   * The per-file import history table for one watch source (#489).
   *
   * Presentational: props in, events out, no fetching. Modelled on
   * `$components/fileStatus/TasksGrid.svelte`.
   */
  import { createEventDispatcher } from 'svelte';
  import Badge from '$components/ui/Badge.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { t } from '$stores/locale';
  import { formatDate } from '$lib/utils/formatting';
  import { RETRYABLE_FILE_STATUSES, type WatchSourceFile } from '$lib/api/watchSourcesApi';

  export let files: WatchSourceFile[] = [];
  export let busyUuids: Set<string> = new Set();
  export let selectedUuids: Set<string> = new Set();
  /** Whether the source still limits imports by age — drives the futile-retry hint. */
  export let hasAgeLimit = false;

  const dispatch = createEventDispatcher<{
    retry: WatchSourceFile;
    delete: WatchSourceFile;
    toggleSelect: string;
    toggleSelectAll: boolean;
  }>();

  $: allSelected = files.length > 0 && files.every((f) => selectedUuids.has(f.uuid));

  /**
   * Status label, falling back to the raw value.
   *
   * Deployments carry statuses this UI does not enumerate — `skipped_too_large` is
   * written by the document ingest path and is not an enum member yet (#547) — and a
   * missing translation must degrade to something readable rather than an empty cell
   * that looks like missing data.
   */
  function statusLabel(status: string): string {
    const key = `settings.watchSources.files.status.${status}`;
    const label = $t(key);
    return label === key ? status : label;
  }

  function skipReasonLabel(reason: string): string {
    const key = `settings.watchSources.files.skipReason.${reason}`;
    const label = $t(key);
    return label === key ? reason : label;
  }

  function statusVariant(status: string): 'default' | 'success' | 'warning' | 'error' | 'info' {
    if (status === 'imported') return 'success';
    if (status === 'error') return 'error';
    if (status.startsWith('skipped')) return 'warning';
    if (status === 'pending' || status === 'waiting_for_parts') return 'default';
    return 'info';
  }

  /**
   * `retry_count` means two different things depending on the row's status: failed
   * import ATTEMPTS on an ordinary row, and SCANS WAITED on a multipart row awaiting
   * its siblings. One heading for both would misreport one of them.
   */
  function attemptsLabel(file: WatchSourceFile): string {
    return file.status === 'waiting_for_parts'
      ? $t('settings.watchSources.files.scansWaited', { count: file.retry_count })
      : $t('settings.watchSources.files.attempts', { count: file.retry_count });
  }

  function reasonText(file: WatchSourceFile): string {
    if (file.error_message) return file.error_message;
    if (file.skip_reason) return skipReasonLabel(file.skip_reason);
    return '';
  }

  function libraryHref(file: WatchSourceFile): string | null {
    return file.media_file_uuid ? `/files/${file.media_file_uuid}` : null;
  }
</script>

<table class="files-table">
  <thead>
    <tr>
      <th class="select-col">
        <input
          type="checkbox"
          checked={allSelected}
          aria-label={$t('settings.watchSources.files.selectAll')}
          on:change={(e) => dispatch('toggleSelectAll', e.currentTarget.checked)}
        />
      </th>
      <th>{$t('settings.watchSources.files.columnFile')}</th>
      <th>{$t('settings.watchSources.files.columnStatus')}</th>
      <th>{$t('settings.watchSources.files.columnReason')}</th>
      <th>{$t('settings.watchSources.files.columnAttempts')}</th>
      <th>{$t('settings.watchSources.files.columnSeen')}</th>
      <th class="actions-col"></th>
    </tr>
  </thead>
  <tbody>
    {#each files as file (file.uuid)}
      <tr>
        <td class="select-col">
          <input
            type="checkbox"
            checked={selectedUuids.has(file.uuid)}
            aria-label={file.filename}
            on:change={() => dispatch('toggleSelect', file.uuid)}
          />
        </td>
        <td class="file-cell" title={file.remote_path}>
          {#if libraryHref(file)}
            <a href={libraryHref(file)}>{file.filename}</a>
          {:else}
            {file.filename}
          {/if}
        </td>
        <td>
          <Badge variant={statusVariant(file.status)}>{statusLabel(file.status)}</Badge>
        </td>
        <td class="reason-cell">
          {reasonText(file)}
          {#if file.skip_reason === 'too_old' && hasAgeLimit}
            <div class="row-hint">{$t('settings.watchSources.files.ageLimitHint')}</div>
          {/if}
        </td>
        <td class="attempts-cell">{attemptsLabel(file)}</td>
        <td class="when-cell">{file.created_at ? formatDate(file.created_at) : ''}</td>
        <td class="actions-col">
          {#if RETRYABLE_FILE_STATUSES.has(file.status)}
            <button
              class="btn btn-secondary btn-sm"
              disabled={busyUuids.has(file.uuid)}
              on:click={() => dispatch('retry', file)}
            >
              {#if busyUuids.has(file.uuid)}
                <Spinner size="small" />
              {:else}
                {$t('settings.watchSources.files.retry')}
              {/if}
            </button>
          {/if}
          <button
            class="btn btn-danger btn-sm"
            disabled={busyUuids.has(file.uuid)}
            on:click={() => dispatch('delete', file)}
          >
            {$t('settings.watchSources.files.deleteRecord')}
          </button>
        </td>
      </tr>
    {/each}
  </tbody>
</table>

<style>
  .files-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }
  .files-table th,
  .files-table td {
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border-color);
    vertical-align: top;
  }
  .files-table th {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--text-secondary);
    font-weight: 600;
  }
  .file-cell {
    max-width: 260px;
    overflow-wrap: anywhere;
  }
  .file-cell a {
    color: var(--primary-on-surface);
  }
  .reason-cell {
    max-width: 280px;
    overflow-wrap: anywhere;
    color: var(--text-secondary);
  }
  .row-hint {
    margin-top: 4px;
    font-size: 0.75rem;
    font-style: italic;
    color: var(--warning-color);
  }
  .attempts-cell,
  .when-cell {
    white-space: nowrap;
    color: var(--text-secondary);
  }
  .select-col {
    width: 28px;
  }
  .actions-col {
    white-space: nowrap;
    text-align: right;
  }
  .actions-col .btn {
    margin-left: 4px;
  }
  .btn-sm {
    padding: 4px 10px;
    font-size: 0.8rem;
  }
  /* Override the global `input { width:100% }` base so checkboxes stay square. */
  .files-table input[type='checkbox'] {
    width: 16px;
    height: 16px;
    min-height: 0;
    margin: 0;
    padding: 0;
    flex: none;
    cursor: pointer;
    accent-color: var(--primary-color);
  }
</style>
