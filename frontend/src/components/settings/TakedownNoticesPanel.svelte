<script lang="ts">
  /**
   * Takedown notice records (issue #1254): the durable list of legal notices (and operator
   * actions with none), the files each covers and what became of them, plus the compliance
   * export. Platform administrators only; the endpoints enforce that, this panel only mirrors it.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Spinner from '$components/ui/Spinner.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import {
    TakedownNoticesApi,
    type ExportFormat,
    type TakedownNotice,
  } from '$lib/api/takedownNotices';
  import { emptyDraft, isDraftValid, toNewNotice } from '$lib/takedownBasis';
  import TakedownNoticeFields from '$components/takedown/TakedownNoticeFields.svelte';
  import TakedownNoticeDetail from '$components/takedown/TakedownNoticeDetail.svelte';
  import TakedownStatusChip from '$components/takedown/TakedownStatusChip.svelte';
  import { TAKEDOWN_NOTICE_TYPE_KEYS, labelFor } from '$lib/i18n/keyMaps';

  const PAGE_SIZE = 50;

  let notices: TakedownNotice[] = [];
  let total = 0;
  let offset = 0;
  let loading = false;
  let loadFailed = false;
  let filter: '' | 'open' | 'resolved' = '';
  let expanded: string | null = null;
  let recording = false;
  let saving = false;
  let draft = emptyDraft();

  async function load(newOffset = 0) {
    loading = true;
    loadFailed = false;
    try {
      const result = await TakedownNoticesApi.list({
        status: filter || undefined,
        limit: PAGE_SIZE,
        offset: newOffset,
      });
      notices = result.notices;
      total = result.total;
      offset = newOffset;
    } catch (err) {
      loadFailed = true;
      toastStore.error(getErrorMessage(err, $t('takedown.admin.loadFailed')));
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void load();
  });

  async function saveNotice() {
    if (!isDraftValid(draft) || saving) return;
    saving = true;
    try {
      await TakedownNoticesApi.create(toNewNotice(draft));
      toastStore.success($t('takedown.admin.recorded'));
      draft = emptyDraft();
      recording = false;
      await load(0);
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('takedown.admin.recordFailed')));
    } finally {
      saving = false;
    }
  }

  function replace(updated: TakedownNotice) {
    notices = notices.map((n) => (n.uuid === updated.uuid ? updated : n));
  }

  /** Opening a row re-reads it: the list can be minutes old, and a file may have been released. */
  async function toggle(uuid: string) {
    if (expanded === uuid) {
      expanded = null;
      return;
    }
    expanded = uuid;
    try {
      replace(await TakedownNoticesApi.get(uuid));
    } catch {
      // The list's copy is still shown; the next action reports any real failure.
    }
  }

  async function download(format: ExportFormat) {
    try {
      const { blob, filename, truncated } = await TakedownNoticesApi.export(format);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
      if (truncated) toastStore.warning($t('takedown.admin.exportTruncated'));
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('takedown.admin.exportFailed')));
    }
  }
</script>

<div class="notices-panel">
  <h3 class="section-title">{$t('takedown.admin.title')}</h3>
  <p class="section-description">{$t('takedown.admin.description')}</p>

  <div class="toolbar">
    <label class="filter">
      {$t('takedown.admin.filterLabel')}
      <select bind:value={filter} on:change={() => load(0)}>
        <option value="">{$t('takedown.admin.filterAll')}</option>
        <option value="open">{$t('takedown.status.open')}</option>
        <option value="resolved">{$t('takedown.status.resolved')}</option>
      </select>
    </label>
    <button class="btn" on:click={() => (recording = !recording)}>
      {$t('takedown.admin.record')}
    </button>
    <button class="btn" on:click={() => download('csv')}>{$t('takedown.admin.exportCsv')}</button>
    <button class="btn" on:click={() => download('json')}>{$t('takedown.admin.exportJson')}</button>
  </div>

  {#if recording}
    <form class="record-form" on:submit|preventDefault={saveNotice}>
      <TakedownNoticeFields bind:draft idPrefix="panel-notice" disabled={saving} />
      <button class="btn primary" type="submit" disabled={!isDraftValid(draft) || saving}>
        {#if saving}<Spinner size="small" />{/if}
        {$t('takedown.admin.recordSubmit')}
      </button>
    </form>
  {/if}

  {#if loading && notices.length === 0}
    <div class="state-row"><Spinner size="small" /><span>{$t('common.loading')}</span></div>
  {:else if loadFailed}
    <div class="state-row error">
      <span>{$t('takedown.admin.loadFailed')}</span>
      <button class="btn" on:click={() => load(offset)}>{$t('common.retry')}</button>
    </div>
  {:else if total === 0}
    <EmptyState
      title={$t('takedown.admin.emptyTitle')}
      description={$t('takedown.admin.emptyMessage')}
    />
  {:else}
    <table class="notices">
      <thead>
        <tr>
          <th>{$t('takedown.admin.columnReceived')}</th>
          <th>{$t('takedown.fields.type')}</th>
          <th>{$t('takedown.fields.reference')}</th>
          <th>{$t('takedown.admin.columnFiles')}</th>
          <th>{$t('takedown.admin.columnStatus')}</th>
        </tr>
      </thead>
      <tbody>
        {#each notices as notice (notice.uuid)}
          <tr class="row" class:open={expanded === notice.uuid}>
            <td>
              <button
                class="row-toggle"
                aria-expanded={expanded === notice.uuid}
                on:click={() => toggle(notice.uuid)}
              >
                {new Date(notice.received_at).toLocaleDateString()}
              </button>
            </td>
            <td>{labelFor($t, TAKEDOWN_NOTICE_TYPE_KEYS, notice.notice_type)}</td>
            <td>{notice.requester_reference ?? '—'}</td>
            <td>{notice.links.length}</td>
            <td><TakedownStatusChip status={notice.status} /></td>
          </tr>
          {#if expanded === notice.uuid}
            <tr>
              <td colspan="5">
                <TakedownNoticeDetail {notice} on:changed={(e) => replace(e.detail)} />
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>

    <div class="pagination">
      <button
        disabled={offset === 0 || loading}
        on:click={() => load(Math.max(0, offset - PAGE_SIZE))}
      >
        {$t('common.previous')}
      </button>
      <span>{offset + 1}-{Math.min(offset + PAGE_SIZE, total)} / {total}</span>
      <button disabled={offset + PAGE_SIZE >= total || loading} on:click={() => load(offset + PAGE_SIZE)}>
        {$t('common.next')}
      </button>
    </div>
  {/if}
</div>

<style>
  .notices-panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
  }

  .filter {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8125rem;
    white-space: nowrap;
  }

  .btn,
  .pagination button {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.375rem 0.875rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-color);
    color: var(--text-color);
    cursor: pointer;
    font-size: 0.8125rem;
  }

  .btn.primary {
    align-self: flex-start;
    background: var(--accent-color, #2563eb);
    border-color: var(--accent-color, #2563eb);
    color: white;
  }

  .btn:disabled,
  .pagination button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .record-form {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
  }

  .state-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem;
    border-radius: 6px;
    background: var(--bg-secondary, rgba(0, 0, 0, 0.03));
    font-size: 0.8125rem;
  }

  .state-row.error {
    color: #dc2626;
    justify-content: space-between;
  }

  .notices {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .notices th,
  .notices td {
    padding: 0.5rem;
    border-bottom: 1px solid var(--border-color);
    text-align: left;
    vertical-align: top;
  }

  .row:hover,
  .row.open {
    background: var(--hover-color, rgba(0, 0, 0, 0.04));
  }

  .row-toggle {
    border: none;
    background: none;
    padding: 0;
    color: var(--accent-color, #2563eb);
    cursor: pointer;
    font-size: 0.8125rem;
  }

  .pagination {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    font-size: 0.8125rem;
  }
</style>
