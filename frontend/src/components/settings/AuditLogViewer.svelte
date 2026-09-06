<script lang="ts">
  import { onMount } from 'svelte';
  import { AdminApi, type AuditLogEntry } from '$lib/api/admin';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';

  let auditLogs: AuditLogEntry[] = [];
  let loading = false;
  let loadingMore = false;
  let backendNotReady = false; // Backend is fully implemented
  let totalLogs = 0;
  let hasMore = false;
  const PAGE_SIZE = 50;
  let filters = {
    startDate: '',
    endDate: '',
    eventType: '',
    outcome: ''
  };
  let tableContainer: HTMLElement | null = null;

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

  async function loadAuditLogs() {
    loading = true;
    try {
      const result = await AdminApi.getAuditLogs({
        start_date: filters.startDate || undefined,
        end_date: filters.endDate || undefined,
        event_type: filters.eventType || undefined,
        outcome: filters.outcome || undefined,
        limit: PAGE_SIZE,
        offset: 0
      });
      auditLogs = result.logs;
      totalLogs = result.total;
      hasMore = auditLogs.length < totalLogs;
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
        start_date: filters.startDate || undefined,
        end_date: filters.endDate || undefined,
        event_type: filters.eventType || undefined,
        outcome: filters.outcome || undefined,
        limit: PAGE_SIZE,
        offset: auditLogs.length
      });
      auditLogs = [...auditLogs, ...result.logs];
      totalLogs = result.total;
      hasMore = auditLogs.length < totalLogs;
    } catch (error) {
      console.error('Failed to load more audit logs:', error);
    } finally {
      loadingMore = false;
    }
  }

  function handleTableScroll(e: Event) {
    const el = e.target as HTMLElement;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 100) {
      loadMoreLogs();
    }
  }

  async function exportLogs(format: 'csv' | 'json') {
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

  function formatDateTime(dateString: string): string {
    const date = new Date(dateString);
    // Compact format using current locale: MM/DD HH:MM:SS
    const currentLocale = $locale || 'en';
    return date.toLocaleDateString(currentLocale, { month: '2-digit', day: '2-digit' }) +
           ' ' + date.toLocaleTimeString(currentLocale, { hour12: false });
  }

  function getOutcomeClass(outcome: string): string {
    const lower = outcome.toLowerCase();
    if (lower === 'success') return 'success';
    if (lower === 'failure') return 'failure';
    return 'partial';
  }

  let detailsModalOpen = false;
  let detailsJson = '';

  function showDetails(details: any) {
    detailsJson = JSON.stringify(details, null, 2);
    detailsModalOpen = true;
  }

  function closeDetails() {
    detailsModalOpen = false;
    detailsJson = '';
  }

  function handleDetailsKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      closeDetails();
    }
  }

  function handleDetailsBackdrop(event: MouseEvent) {
    if (event.target === event.currentTarget) {
      closeDetails();
    }
  }
</script>

<div class="audit-log-viewer">
  {#if backendNotReady}
    <div class="coming-soon">
      <div class="coming-soon-icon">📋</div>
      <h3>{$t('settings.auditLog.viewerTitle')}</h3>
      <p>{$t('settings.auditLog.comingSoonDesc')}</p>
      <p class="note">
        <strong>{$t('settings.auditLog.noteLabel')}</strong> {$t('settings.auditLog.comingSoonNote')}
      </p>
    </div>
  {:else}
    <div class="filters">
      <div class="filter-row">
        <label>
          <span class="label-text">{$t('settings.auditLog.startDate')}</span>
          <input type="date" bind:value={filters.startDate} />
        </label>
        <label>
          <span class="label-text">{$t('settings.auditLog.endDate')}</span>
          <input type="date" bind:value={filters.endDate} />
        </label>
        <label>
          <span class="label-text">{$t('settings.auditLog.event')}</span>
          <select bind:value={filters.eventType}>
            <option value="">{$t('settings.auditLog.all')}</option>
            {#each eventTypes as type}
              <option value={type.value}>{type.label}</option>
            {/each}
          </select>
        </label>
        <label>
          <span class="label-text">{$t('settings.auditLog.outcome')}</span>
          <select bind:value={filters.outcome}>
            <option value="">{$t('settings.auditLog.all')}</option>
            <option value="success">{$t('settings.auditLog.success')}</option>
            <option value="failure">{$t('settings.auditLog.failure')}</option>
          </select>
        </label>
        <button class="btn-apply" on:click={loadAuditLogs}>{$t('settings.auditLog.apply')}</button>
        <button class="btn-clear" on:click={() => { filters = { startDate: '', endDate: '', eventType: '', outcome: '' }; loadAuditLogs(); }}>{$t('common.clear')}</button>
        <div class="export-actions">
          <button class="btn-secondary" on:click={() => exportLogs('csv')}>CSV</button>
          <button class="btn-secondary" on:click={() => exportLogs('json')}>JSON</button>
        </div>
      </div>
    </div>

    {#if loading}
      <div class="loading">{$t('settings.auditLog.loadingShort')}</div>
    {:else}
      <div class="audit-table-container" bind:this={tableContainer} on:scroll={handleTableScroll}>
        {#if totalLogs > 0}
          <div class="log-count">{auditLogs.length} / {totalLogs}</div>
        {/if}
        <table class="audit-table">
          <thead>
            <tr>
              <th>{$t('settings.auditLog.time')}</th>
              <th>{$t('settings.auditLog.event')}</th>
              <th>{$t('settings.auditLog.user')}</th>
              <th>{$t('settings.auditLog.status')}</th>
              <th>{$t('settings.auditLog.ip')}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each auditLogs as event}
              <tr class:failure={event.outcome?.toLowerCase() === 'failure'}>
                <td>{formatDateTime(event.timestamp)}</td>
                <td><span class="event-type">{event.event_type}</span></td>
                <td>{event.username || '-'}</td>
                <td>
                  <span class="outcome {getOutcomeClass(event.outcome)}">
                    {event.outcome?.toLowerCase() === 'success' ? $t('settings.auditLog.statusOk') : $t('settings.auditLog.statusFail')}
                  </span>
                </td>
                <td>{event.source_ip}</td>
                <td>
                  <button class="details-btn" on:click={() => showDetails(event.details)}>
                    ...
                  </button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if loadingMore}
        <div class="loading-more">{$t('settings.auditLog.loadingShort')}</div>
      {/if}
      {#if auditLogs.length === 0}
        <div class="loading">{$t('settings.auditLog.noLogs')}</div>
      {/if}
    {/if}
  {/if}
</div>

{#if detailsModalOpen}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div
    class="details-modal-backdrop"
    on:click={handleDetailsBackdrop}
    on:wheel|preventDefault|self
    on:touchmove|preventDefault|self
    on:keydown={handleDetailsKeydown}
    tabindex="-1"
    role="dialog"
    aria-modal="true"
    aria-label={$t('settings.auditLog.eventDetails')}
  >
    <div class="details-modal">
      <div class="details-modal-header">
        <h3>{$t('settings.auditLog.eventDetails')}</h3>
        <button class="details-modal-close" on:click={closeDetails} aria-label={$t('settings.auditLog.close')}>
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <pre class="details-modal-body">{detailsJson}</pre>
    </div>
  </div>
{/if}

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
    font-size: 2rem;
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

  /* Compact Filters */
  .filters {
    background: var(--color-bg-secondary);
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    margin-bottom: 0.5rem;
  }

  .filter-row {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
    align-items: flex-end;
  }

  .filter-row label {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    font-size: 0.75rem;
  }

  .label-text {
    color: var(--color-text-secondary);
    font-size: 0.6875rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
  }

  .filter-row input,
  .filter-row select {
    padding: 0.25rem 0.375rem;
    border: 1px solid var(--color-border);
    border-radius: 3px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: 0.75rem;
    height: 1.75rem;
  }

  .filter-row input[type="date"] {
    width: 7.5rem;
  }

  .filter-row select {
    min-width: 6rem;
  }

  .btn-apply {
    padding: 0.25rem 0.625rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.75rem;
    font-weight: 500;
    height: 1.75rem;
    white-space: nowrap;
    background: var(--primary-color);
    color: white;
    border: none;
    align-self: flex-end;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-apply:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.3);
  }

  .btn-apply:active {
    transform: scale(1);
  }

  .btn-clear {
    padding: 0.25rem 0.625rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.75rem;
    font-weight: 500;
    height: 1.75rem;
    white-space: nowrap;
    background: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
  }

  .btn-clear:hover {
    background: var(--button-hover);
  }

  .export-actions {
    display: flex;
    gap: 0.25rem;
    margin-left: auto;
  }

  .btn-secondary {
    padding: 0.25rem 0.5rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.75rem;
    height: 1.75rem;
    white-space: nowrap;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-color);
    transition: all 0.2s ease;
  }

  .btn-secondary:hover {
    background: var(--button-hover);
    transform: scale(1.02);
  }

  .btn-secondary:active {
    transform: scale(1);
  }

  /* Compact Table */
  .audit-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.75rem;
  }

  .audit-table th,
  .audit-table td {
    padding: 0.375rem 0.5rem;
    text-align: left;
    border-bottom: 1px solid var(--color-border);
    white-space: nowrap;
  }

  .audit-table th {
    background: var(--color-bg-secondary);
    font-weight: 600;
    font-size: 0.6875rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    color: var(--color-text-secondary);
    position: sticky;
    top: 0;
    z-index: 1;
  }

  .audit-table tbody tr:hover {
    background: var(--color-bg-secondary);
  }

  .audit-table tr.failure {
    background: rgba(239, 68, 68, 0.08);
  }

  .audit-table tr.failure:hover {
    background: rgba(239, 68, 68, 0.12);
  }

  /* Compact timestamp */
  .audit-table td:first-child {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.6875rem;
    color: var(--color-text-secondary);
  }

  .event-type {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.6875rem;
    background: var(--color-bg-secondary);
    padding: 0.125rem 0.375rem;
    border-radius: 3px;
    display: inline-block;
  }

  .outcome {
    padding: 0.125rem 0.375rem;
    border-radius: 3px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.025em;
  }

  .outcome.success {
    background: rgba(34, 197, 94, 0.15);
    color: rgb(22, 163, 74);
  }

  :global([data-theme='dark']) .outcome.success {
    background: rgba(34, 197, 94, 0.2);
    color: rgb(74, 222, 128);
  }

  .outcome.failure {
    background: rgba(239, 68, 68, 0.15);
    color: rgb(220, 38, 38);
  }

  :global([data-theme='dark']) .outcome.failure {
    background: rgba(239, 68, 68, 0.2);
    color: rgb(248, 113, 113);
  }

  /* Source IP - compact monospace */
  .audit-table td:nth-child(5) {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.6875rem;
    color: var(--color-text-secondary);
  }

  .details-btn {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    padding: 0.125rem 0.375rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.625rem;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.025em;
    transition: all 0.2s ease;
  }

  .details-btn:hover {
    background: var(--button-hover);
    color: var(--text-color);
    transform: scale(1.02);
  }

  .details-btn:active {
    transform: scale(1);
  }

  .loading {
    text-align: center;
    padding: 1rem;
    color: var(--color-text-secondary);
    font-size: 0.8125rem;
  }

  /* Scrollable table container for many logs */
  .audit-table-container {
    max-height: 400px;
    overflow-y: auto;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    position: relative;
  }

  .log-count {
    position: sticky;
    top: 0;
    right: 0;
    text-align: right;
    font-size: 0.6875rem;
    color: var(--text-secondary);
    padding: 4px 8px;
    background: var(--surface-color);
    z-index: 2;
  }

  .loading-more {
    text-align: center;
    padding: 0.75rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  /* Details Modal */
  .details-modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1200;
    padding: 1rem;
    overflow: hidden;
    overscroll-behavior: none;
  }

  :global([data-theme='dark']) .details-modal-backdrop {
    background: rgba(0, 0, 0, 0.7);
  }

  .details-modal {
    background: var(--color-bg, #fff);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    max-width: 600px;
    width: 100%;
    max-height: 70vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
  }

  .details-modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--color-border);
  }

  .details-modal-header h3 {
    margin: 0;
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--color-text);
  }

  .details-modal-close {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    cursor: pointer;
    padding: 0.25rem;
    color: var(--text-secondary);
    border-radius: 6px;
    display: flex;
    align-items: center;
    transition: all 0.2s ease;
  }

  .details-modal-close:hover {
    color: var(--text-color);
    background: var(--button-hover);
    transform: scale(1.02);
  }

  .details-modal-close:active {
    transform: scale(1);
  }

  .details-modal-body {
    padding: 1rem;
    margin: 0;
    overflow: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.75rem;
    line-height: 1.5;
    color: var(--color-text);
    white-space: pre-wrap;
    word-break: break-word;
    background: var(--color-bg-secondary, #f9fafb);
    border-radius: 0 0 8px 8px;
  }

  @media (max-width: 768px) {
    .filter-row {
      flex-direction: column;
      gap: 0.5rem;
    }

    .filter-row label {
      width: 100%;
    }

    .filter-row input,
    .filter-row select {
      height: auto;
      min-height: 44px;
      font-size: 1rem;
      padding: 0.5rem 0.75rem;
    }

    .filter-row input[type="date"] {
      width: 100%;
    }

    .filter-row select {
      width: 100%;
    }

    .btn-apply,
    .btn-clear {
      width: 100%;
      min-height: 44px;
      height: auto;
    }

    .export-actions {
      margin-left: 0;
      width: 100%;
    }

    .export-actions .btn-secondary {
      flex: 1;
    }

    .audit-table-container {
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }
  }
</style>
