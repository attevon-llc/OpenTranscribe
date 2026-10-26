<!--
  OrgAuditLogPanel.svelte — an organization admin's audit log (issue #1277).

  The tenant-scoped sibling of AuditLogViewer: the same filter bar and table, fed by
  `/org-admin/audit-logs`, with a member picker, a resource filter (everything that happened to
  one file or collection) and an export. The server decides what the organization may see; this
  renders it. The content-access event types are offered only when the deployment has content
  auditing available at all (`audit.content_access`), since otherwise none exist.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import AuditLogFilters from '$components/settings/audit/AuditLogFilters.svelte';
  import AuditLogTable from '$components/settings/audit/AuditLogTable.svelte';
  import {
    emptyAuditFilters,
    type AuditFilterState,
  } from '$components/settings/audit/auditFilters';
  import type { AuditLogEntry } from '$lib/api/admin';
  import { OrgAdminApi, type OrgAuditQuery } from '$lib/api/orgAdmin';
  import { ORG_AUDIT_EVENT_KEYS } from '$lib/i18n/keyMaps';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  /** Pre-fills the resource filter, e.g. from a file's "access history" link. */
  export let resourceUuid = '';

  const PAGE_SIZE = 50;
  const CONTENT_EVENTS = ['content.file.view', 'content.media.access', 'content.export'];

  let filters: AuditFilterState = { ...emptyAuditFilters(), resourceUuid };
  let logs: AuditLogEntry[] = [];
  let total = 0;
  let loading = false;
  let loadingMore = false;
  let canExport = true;

  $: contentAudited = isCapabilityEnabled($capabilities, 'audit.content_access');
  $: eventTypes = Object.entries(ORG_AUDIT_EVENT_KEYS)
    .filter(([value]) => contentAudited || !CONTENT_EVENTS.includes(value))
    .map(([value, key]) => ({ value, label: $t(key) }));
  $: hasMore = logs.length < total;

  function query(): OrgAuditQuery {
    const pick = (value: string) => value.trim() || undefined;
    return {
      start_date: pick(filters.startDate),
      end_date: pick(filters.endDate),
      event_type: pick(filters.eventType),
      outcome: pick(filters.outcome),
      user_uuid: pick(filters.userUuid),
      resource_uuid: pick(filters.resourceUuid),
    };
  }

  async function load() {
    loading = true;
    try {
      const result = await OrgAdminApi.listAuditLogs({ ...query(), limit: PAGE_SIZE, offset: 0 });
      logs = result.logs;
      total = result.total;
    } catch (error) {
      console.error('Failed to load the organization audit log:', error);
      toastStore.error($t('settings.auditLog.loadError'));
    } finally {
      loading = false;
    }
  }

  async function loadMore() {
    if (loadingMore || !hasMore) return;
    loadingMore = true;
    try {
      const result = await OrgAdminApi.listAuditLogs({
        ...query(),
        limit: PAGE_SIZE,
        offset: logs.length,
      });
      logs = [...logs, ...result.logs];
      total = result.total;
    } catch (error) {
      console.error('Failed to load more audit events:', error);
    } finally {
      loadingMore = false;
    }
  }

  async function exportLogs(event: CustomEvent<'csv' | 'json'>) {
    const format = event.detail;
    try {
      const blob = await OrgAdminApi.exportAuditLogs(format, query());
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `audit-logs-${new Date().toISOString().split('T')[0]}.${format}`;
      a.click();
      URL.revokeObjectURL(url);
      toastStore.success($t('settings.auditLog.exportSuccess', { format: format.toUpperCase() }));
    } catch (error) {
      console.error('Failed to export the organization audit log:', error);
      // A 404 is the deployment withholding the export; hide the buttons rather than retry.
      if ((error as { response?: { status?: number } })?.response?.status === 404) {
        canExport = false;
      }
      toastStore.error($t('settings.auditLog.exportError'));
    }
  }

  onMount(load);
</script>

<div class="org-audit">
  <p class="intro">{$t('orgAudit.description')}</p>
  <AuditLogFilters
    bind:filters
    {eventTypes}
    {canExport}
    orgView
    on:apply={load}
    on:clear={load}
    on:export={exportLogs}
  />
  <AuditLogTable {logs} {total} {loading} {loadingMore} orgView on:loadmore={loadMore} />
</div>

<style>
  .org-audit {
    padding: 0.5rem;
    font-size: 0.8125rem;
  }

  .intro {
    margin: 0 0 0.75rem;
    color: var(--color-text-secondary);
    font-size: 0.8125rem;
  }
</style>
