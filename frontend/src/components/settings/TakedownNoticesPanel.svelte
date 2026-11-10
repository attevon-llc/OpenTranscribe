<script lang="ts">
  import OffsetPager from '$components/ui/OffsetPager.svelte';
  import { formatDate } from '$lib/utils/formatting';
  /**
   * Takedown notice records (issue #1254): the durable list of legal notices (and operator
   * actions with none), the files each covers and what became of them, plus the compliance
   * export. Platform administrators only; the endpoints enforce that, this panel only mirrors it.
   */
  import { onMount } from 'svelte';
  import { t, locale } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Spinner from '$components/ui/Spinner.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ExportFormatMenu, { type ExportFormatOption } from '$components/ui/ExportFormatMenu.svelte';
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

  // Technical format names, never translated.
  const EXPORT_FORMATS: ExportFormatOption[] = [
    { value: 'csv', label: 'CSV', icon: 'format-csv' },
    { value: 'json', label: 'JSON', icon: 'format-json' },
  ];

  // The export covers every notice, so only an unfiltered, loaded, empty list is "nothing to export".
  $: exportDisabled = loading ? notices.length === 0 : total === 0 && filter === '' && !loadFailed;

  function cancelRecording() {
    draft = emptyDraft();
    recording = false;
  }

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
    <button
      class="btn btn-secondary"
      type="button"
      on:click={() => (recording ? cancelRecording() : (recording = true))}
    >
      {recording ? $t('common.cancel') : $t('takedown.admin.record')}
    </button>
    <ExportFormatMenu
      formats={EXPORT_FORMATS}
      label={$t('takedown.admin.export')}
      disabled={exportDisabled}
      disabledReason={$t('takedown.admin.exportEmpty')}
      on:select={(e) => download(e.detail.format === 'json' ? 'json' : 'csv')}
    />
  </div>

  {#if recording}
    <form class="record-form" on:submit|preventDefault={saveNotice}>
      <TakedownNoticeFields bind:draft idPrefix="panel-notice" disabled={saving} />
      <div class="record-actions">
        <button class="btn btn-secondary" type="button" disabled={saving} on:click={cancelRecording}>
          {$t('common.cancel')}
        </button>
        <button class="btn btn-primary" type="submit" disabled={!isDraftValid(draft) || saving}>
          {#if saving}<Spinner size="small" />{/if}
          {$t('takedown.admin.recordSubmit')}
        </button>
      </div>
    </form>
  {/if}

  {#if loading && notices.length === 0}
    <div class="settings-state-row"><Spinner size="small" /><span>{$t('common.loading')}</span></div>
  {:else if loadFailed}
    <div class="settings-state-row error">
      <span>{$t('takedown.admin.loadFailed')}</span>
      <button class="btn btn-secondary" on:click={() => load(offset)}>{$t('common.retry')}</button>
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
                {formatDate(notice.received_at, $locale)}
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

    <OffsetPager {offset} {total} pageSize={PAGE_SIZE} {loading} onpage={load} />
  {/if}
</div>

<style>
  .notices-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .filter {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    white-space: nowrap;
  }

  .record-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .record-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
  }

  .settings-state-row.error {
    color: var(--color-error-text);
    justify-content: space-between;
  }

  .notices {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-sm);
  }

  .notices th,
  .notices td {
    padding: var(--space-2);
    border-bottom: 1px solid var(--border-color);
    text-align: start;
    vertical-align: top;
  }

  .row:hover,
  .row.open {
    background: var(--hover-color);
  }

  .row-toggle {
    border: none;
    background: none;
    padding: 0;
    color: var(--primary-on-surface);
    cursor: pointer;
    font-size: var(--font-size-sm);
  }

</style>
