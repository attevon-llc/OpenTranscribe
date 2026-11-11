<!--
  AuditLogTable.svelte — the audit event table, its details dialog and infinite scroll.

  Shared by the platform viewer and the organization panel. `orgView` swaps the User column for
  the resolved name the server sends and adds a Resource column; the event type is shown as a
  translated label there (the raw identifier stays in the tooltip and the details dialog).
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import type { AuditLogEntry } from '$lib/api/admin';
  import { labelFor } from '$lib/i18n/keyMaps';
  import { ORG_AUDIT_EVENT_KEYS } from '$lib/i18n/auditEventKeys';
  import { t, locale } from '$stores/locale';
  import { formatAuditTimestamp } from '$lib/utils/formatting';

  export let logs: AuditLogEntry[];
  export let total: number;
  export let loading: boolean;
  export let loadingMore = false;
  export let orgView = false;

  const dispatch = createEventDispatcher<{ loadmore: void }>();

  let detailsModalOpen = false;
  let detailsJson = '';

  function handleScroll(e: Event) {
    const el = e.target as HTMLElement;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 100) dispatch('loadmore');
  }

  function outcomeClass(outcome: string): string {
    const lower = outcome.toLowerCase();
    if (lower === 'success') return 'success';
    if (lower === 'failure') return 'failure';
    return 'partial';
  }

  function showDetails(details: unknown) {
    detailsJson = JSON.stringify(details, null, 2);
    detailsModalOpen = true;
  }

  function closeDetails() {
    detailsModalOpen = false;
    detailsJson = '';
  }

  const shortUuid = (uuid: string | null | undefined) => (uuid ? uuid.slice(0, 8) : '');
</script>

{#if loading}
  <SettingsSkeleton variant="form" count={4} label={$t('settings.auditLog.loadingShort')} />
{:else}
  <div class="audit-table-container" on:scroll={handleScroll}>
    {#if total > 0}
      <div class="log-count" dir="ltr">{logs.length} / {total}</div>
    {/if}
    <table class="audit-table" class:org-view={orgView}>
      <thead>
        <tr>
          <th>{$t('settings.auditLog.time')}</th>
          <th>{$t('settings.auditLog.event')}</th>
          <th>{$t('settings.auditLog.user')}</th>
          {#if orgView}<th>{$t('orgAudit.resource')}</th>{/if}
          <th>{$t('settings.auditLog.status')}</th>
          <th>{$t('settings.auditLog.ip')}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each logs as event (event.id)}
          <tr class:failure={event.outcome?.toLowerCase() === 'failure'}>
            <td dir="ltr">{formatAuditTimestamp(event.timestamp, $locale)}</td>
            <td>
              {#if orgView}
                <span class="event-label" title={event.event_type}>
                  {labelFor($t, ORG_AUDIT_EVENT_KEYS, event.event_type)}
                </span>
              {:else}
                <span class="event-type">{event.event_type}</span>
              {/if}
            </td>
            <td>
              {#if orgView}
                {event.actor_display || event.username || '-'}
              {:else}
                {event.username || '-'}
              {/if}
            </td>
            {#if orgView}
              <td>
                {#if event.resource_display}
                  {event.resource_display}
                {:else if event.resource_uuid}
                  <span class="uuid" dir="ltr" title={event.resource_uuid}>
                    {shortUuid(event.resource_uuid)}
                  </span>
                {:else}
                  -
                {/if}
              </td>
            {/if}
            <td>
              <span class="outcome {outcomeClass(event.outcome)}">
                {event.outcome?.toLowerCase() === 'success'
                  ? $t('settings.auditLog.statusOk')
                  : $t('settings.auditLog.statusFail')}
              </span>
            </td>
            <td class="ip" dir="ltr">{event.source_ip}</td>
            <td>
              <button class="btn btn-secondary btn-sm" on:click={() => showDetails(event.details)}>...</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  {#if loadingMore}
    <div class="loading-more">{$t('settings.auditLog.loadingShort')}</div>
  {/if}
  {#if logs.length === 0}
    <div class="no-logs">{$t('settings.auditLog.noLogs')}</div>
  {/if}
{/if}

<BaseModal
  isOpen={detailsModalOpen}
  title={$t('settings.auditLog.eventDetails')}
  size="md"
  onClose={closeDetails}
>
  <pre class="details-modal-body" dir="ltr">{detailsJson}</pre>
</BaseModal>

<style>
  .audit-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-xs);
  }

  .audit-table th,
  .audit-table td {
    padding: var(--space-1-5) var(--space-2);
    text-align: start;
    border-bottom: 1px solid var(--color-border);
    white-space: nowrap;
  }

  .audit-table th {
    background: var(--color-bg-secondary);
    font-weight: var(--font-weight-semibold);
    font-size: var(--font-size-2xs);
    text-transform: uppercase;
    letter-spacing: 0.025em;
    color: var(--color-text-secondary);
    position: sticky;
    top: 0;
    z-index: var(--z-base);
  }

  .audit-table tbody tr:hover {
    background: var(--color-bg-secondary);
  }

  .audit-table tr.failure {
    background: rgba(var(--error-color-rgb), 0.08);
  }

  .audit-table tr.failure:hover {
    background: rgba(var(--error-color-rgb), 0.12);
  }

  .audit-table td:first-child {
    unicode-bidi: isolate;
  }

  .audit-table td:first-child,
  .uuid,
  .ip {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: var(--font-size-2xs);
    color: var(--color-text-secondary);
  }

  .event-type {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: var(--font-size-2xs);
    background: var(--color-bg-secondary);
    padding: var(--space-0-5) var(--space-1-5);
    border-radius: var(--radius-sm);
    display: inline-block;
  }

  .event-label {
    font-size: var(--font-size-xs);
  }

  .outcome {
    padding: var(--space-0-5) var(--space-1-5);
    border-radius: var(--radius-sm);
    font-size: 0.625rem;
    font-weight: var(--font-weight-semibold);
    text-transform: uppercase;
    letter-spacing: 0.025em;
  }

  .outcome.success {
    background: rgba(var(--success-color-rgb), 0.15);
    color: var(--success-text);
  }

  .outcome.failure {
    background: rgba(var(--error-color-rgb), 0.15);
    color: var(--error-text);
  }

  .no-logs {
    text-align: center;
    padding: var(--space-4);
    color: var(--color-text-secondary);
    font-size: var(--font-size-sm);
  }

  /* Scrollable table container for many logs */
  .audit-table-container {
    max-height: 400px;
    overflow-y: auto;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-sm);
    position: relative;
  }

  .log-count {
    position: sticky;
    top: 0;
    text-align: end;
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    padding: var(--space-1) var(--space-2);
    background: var(--surface-color);
    z-index: calc(var(--z-base) + 1);
  }

  .loading-more {
    text-align: center;
    padding: var(--space-3);
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  /* Details modal: chrome comes from ui/BaseModal; only the JSON block is styled here. */
  .details-modal-body {
    padding: var(--space-4);
    margin: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: var(--font-size-xs);
    line-height: 1.5;
    color: var(--color-text);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    background: var(--color-bg-secondary);
    border-radius: var(--radius-md);
  }

  @media (max-width: 768px) {
    .audit-table-container {
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }
  }
</style>
