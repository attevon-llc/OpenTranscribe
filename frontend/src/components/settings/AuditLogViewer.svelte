<script lang="ts">
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import AuditContentAccessToggle from '$components/settings/AuditContentAccessToggle.svelte';
  import AuditLogFilters from '$components/settings/audit/AuditLogFilters.svelte';
  import AuditLogTable from '$components/settings/audit/AuditLogTable.svelte';
  import {
    emptyAuditFilters,
    type AuditFilterState,
  } from '$components/settings/audit/auditFilters';
  import { onMount } from 'svelte';
  import { AdminApi, type AuditLogEntry } from '$lib/api/admin';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  let auditLogs: AuditLogEntry[] = [];
  let loading = false;
  let loadingMore = false;
  let backendNotReady = false; // Backend is fully implemented
  let totalLogs = 0;
  $: hasMore = auditLogs.length < totalLogs;
  const PAGE_SIZE = 50;
  let filters: AuditFilterState = emptyAuditFilters();

  const eventTypeKeys: { value: string; key: string }[] = [
    { value: 'auth.login.success', key: 'settings.auditLog.events.loginSuccess' },
    { value: 'auth.login.failure', key: 'settings.auditLog.events.loginFailure' },
    { value: 'auth.logout', key: 'settings.auditLog.events.logout' },
    { value: 'auth.mfa.setup', key: 'settings.auditLog.events.mfaSetup' },
    { value: 'auth.mfa.verify', key: 'settings.auditLog.events.mfaVerify' },
    { value: 'auth.mfa.disable', key: 'settings.auditLog.events.mfaDisable' },
    { value: 'auth.password.change', key: 'settings.auditLog.events.passwordChange' },
    { value: 'auth.account.lockout', key: 'settings.auditLog.events.accountLockout' },
    { value: 'auth.account.unlock', key: 'settings.auditLog.events.accountUnlock' },
    { value: 'auth.token.refresh', key: 'settings.auditLog.events.tokenRefresh' },
    { value: 'auth.session.created', key: 'settings.auditLog.events.sessionCreated' },
    { value: 'admin.user.create', key: 'settings.auditLog.events.userCreate' },
    { value: 'admin.user.update', key: 'settings.auditLog.events.userUpdate' },
    { value: 'admin.user.delete', key: 'settings.auditLog.events.userDelete' },
    { value: 'admin.role.change', key: 'settings.auditLog.events.roleChange' },
    { value: 'admin.settings.change', key: 'settings.auditLog.events.settingsChange' }
  ];
  $: eventTypes = eventTypeKeys.map(et => ({ value: et.value, label: $t(et.key) }));

  onMount(async () => {
    await loadAuditLogs();
  });

  const queryParams = () => ({
    start_date: filters.startDate || undefined,
    end_date: filters.endDate || undefined,
    event_type: filters.eventType || undefined,
    outcome: filters.outcome || undefined
  });

  async function loadAuditLogs() {
    loading = true;
    try {
      const result = await AdminApi.getAuditLogs({ ...queryParams(), limit: PAGE_SIZE, offset: 0 });
      auditLogs = result.logs;
      totalLogs = result.total;
    } catch (error) {
      console.error('Failed to load audit logs:', error);
      toastStore.error($t('settings.auditLog.loadError'));
    } finally {
      loading = false;
    }
  }

  async function loadMoreLogs() {
    if (loadingMore || !hasMore) return;
    loadingMore = true;
    try {
      const result = await AdminApi.getAuditLogs({
        ...queryParams(),
        limit: PAGE_SIZE,
        offset: auditLogs.length
      });
      auditLogs = [...auditLogs, ...result.logs];
      totalLogs = result.total;
    } catch (error) {
      console.error('Failed to load more audit logs:', error);
    } finally {
      loadingMore = false;
    }
  }

  async function exportLogs(event: CustomEvent<'csv' | 'json'>) {
    const format = event.detail;
    try {
      const blob = await AdminApi.exportAuditLogs(
        format,
        filters.startDate || undefined,
        filters.endDate || undefined
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `audit-logs-${new Date().toISOString().split('T')[0]}.${format}`;
      a.click();
      URL.revokeObjectURL(url);
      toastStore.success($t('settings.auditLog.exportSuccess', { format: format.toUpperCase() }));
    } catch (error) {
      console.error('Failed to export audit logs:', error);
      toastStore.error($t('settings.auditLog.exportError'));
    }
  }
</script>

<div class="audit-log-viewer">
  {#if backendNotReady}
    <div class="coming-soon">
      <div class="coming-soon-icon" aria-hidden="true"><MenuIcon name="clipboard" size="xl" /></div>
      <h3>{$t('settings.auditLog.viewerTitle')}</h3>
      <p>{$t('settings.auditLog.comingSoonDesc')}</p>
      <p class="note">
        <strong>{$t('settings.auditLog.noteLabel')}</strong> {$t('settings.auditLog.comingSoonNote')}
      </p>
    </div>
  {:else}
    <AuditContentAccessToggle />
    <AuditLogFilters
      bind:filters
      {eventTypes}
      on:apply={loadAuditLogs}
      on:clear={loadAuditLogs}
      on:export={exportLogs}
    />
    <AuditLogTable
      logs={auditLogs}
      total={totalLogs}
      {loading}
      {loadingMore}
      on:loadmore={loadMoreLogs}
    />
  {/if}
</div>

<style>
  .audit-log-viewer {
    padding: 0.5rem;
    font-size: 0.8125rem;
  }

  /* Coming Soon Section Styles */
  .coming-soon {
    text-align: center;
    padding: 1.5rem;
    background: var(--color-surface);
    border-radius: 6px;
    border: 1px solid var(--color-border);
  }

  .coming-soon-icon {
    display: flex;
    justify-content: center;
    color: var(--text-secondary);
    margin-bottom: 0.5rem;
  }

  .coming-soon h3 {
    margin: 0 0 0.5rem 0;
    font-size: 1rem;
    color: var(--color-text);
  }

  .coming-soon p {
    color: var(--color-text-secondary);
    margin: 0.25rem 0;
    font-size: 0.8125rem;
  }

  .note {
    background: var(--color-info-bg, rgba(59, 130, 246, 0.1));
    border: 1px solid var(--color-info-border, rgba(59, 130, 246, 0.3));
    border-radius: 4px;
    padding: 0.75rem;
    margin-top: 0.75rem;
    text-align: left;
    font-size: 0.75rem;
  }

  .note strong {
    color: var(--color-info, #3b82f6);
  }
</style>
