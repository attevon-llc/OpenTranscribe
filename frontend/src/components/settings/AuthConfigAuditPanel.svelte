<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AuthConfigApi,
    AUTH_CONFIG_CATEGORIES,
    type AuthConfigAuditResponse,
    type AuthConfigCategory,
  } from '$lib/api/authConfig';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import EmptyState from '$components/ui/EmptyState.svelte';

  /**
   * Who changed which auth setting, when, and from where.
   *
   * This reads `auth_config_audit` (Postgres) — a different source from the
   * "Audit Log" screen, which streams security events out of OpenSearch and does
   * not carry configuration changes at all.
   */

  /** Page size. The endpoint caps `limit` server-side (see AUTH_CONFIG_AUDIT_MAX_LIMIT). */
  const PAGE_SIZE = 100;

  /** Category label keys — the five tab categories reuse the tab labels. */
  const CATEGORY_LABEL_KEYS: Record<AuthConfigCategory, string> = {
    local: 'settings.authentication.tab.local',
    password_policy: 'settings.authAudit.category.passwordPolicy',
    mfa: 'settings.authAudit.category.mfa',
    lockout: 'settings.authAudit.category.lockout',
    session: 'settings.authentication.tab.session',
    ldap: 'settings.authentication.tab.ldap',
    keycloak: 'settings.authentication.tab.keycloak',
    pki: 'settings.authentication.tab.pki',
    banner: 'settings.authAudit.category.banner',
  };

  let category: AuthConfigCategory = 'local';
  let entries: AuthConfigAuditResponse[] = [];
  let loading = false;
  let loadingMore = false;
  let hasMore = false;

  $: categoryOptions = AUTH_CONFIG_CATEGORIES.map((value) => ({
    value,
    label: $t(CATEGORY_LABEL_KEYS[value]),
  }));

  // Render the actor column only once the API actually sends an actor.
  $: hasActor = entries.some((entry) => !!entry.changed_by_email);

  onMount(loadEntries);

  async function loadEntries() {
    loading = true;
    try {
      const result = await AuthConfigApi.getAuditLog(category, PAGE_SIZE, 0);
      entries = result;
      hasMore = result.length === PAGE_SIZE;
    } catch (error: unknown) {
      console.error(`Failed to load auth config audit log for ${category}:`, error);
      toastStore.error(getErrorMessage(error, $t('settings.authAudit.loadFailed')));
      entries = [];
      hasMore = false;
    } finally {
      loading = false;
    }
  }

  async function loadMore() {
    if (loadingMore || !hasMore) return;
    loadingMore = true;
    try {
      const result = await AuthConfigApi.getAuditLog(category, PAGE_SIZE, entries.length);
      entries = [...entries, ...result];
      hasMore = result.length === PAGE_SIZE;
    } catch (error: unknown) {
      console.error('Failed to load more auth config audit entries:', error);
      toastStore.error(getErrorMessage(error, $t('settings.authAudit.loadFailed')));
    } finally {
      loadingMore = false;
    }
  }

  function handleCategoryChange() {
    entries = [];
    loadEntries();
  }

  function formatDateTime(iso: string): string {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    const currentLocale = $locale || 'en';
    return `${date.toLocaleDateString(currentLocale, {
      year: '2-digit',
      month: '2-digit',
      day: '2-digit',
    })} ${date.toLocaleTimeString(currentLocale, { hour12: false })}`;
  }

  function changeTypeLabel(changeType: string): string {
    const key = `settings.authAudit.changeType.${changeType}`;
    const label = $t(key);
    // i18next echoes the key back when it is missing; show the raw value instead
    // of a dotted key if the backend ever adds a new change type.
    return label === key ? changeType : label;
  }

  function displayValue(value: string | null): string {
    return value === null || value === '' ? '—' : value;
  }
</script>

<div class="audit-panel">
  <p class="panel-description">{$t('settings.authAudit.description')}</p>

  <div class="controls">
    <label class="category-label" for="auth-audit-category">
      <span class="label-text">{$t('settings.authAudit.category.label')}</span>
      <select id="auth-audit-category" bind:value={category} on:change={handleCategoryChange}>
        {#each categoryOptions as option (option.value)}
          <option value={option.value}>{option.label}</option>
        {/each}
      </select>
    </label>
    <button type="button" class="btn-secondary" on:click={loadEntries} disabled={loading}>
      {$t('settings.authAudit.refresh')}
    </button>
  </div>

  {#if loading}
    <p class="panel-status">{$t('settings.authAudit.loading')}</p>
  {:else if entries.length === 0}
    <EmptyState
      title={$t('settings.authAudit.empty')}
      description={$t('settings.authAudit.emptyDescription')}
      padding="1.5rem"
    />
  {:else}
    <div class="table-scroll">
      <table class="audit-table">
        <thead>
          <tr>
            <th>{$t('settings.authAudit.time')}</th>
            <th>{$t('settings.authAudit.key')}</th>
            <th>{$t('settings.authAudit.change')}</th>
            {#if hasActor}
              <th>{$t('settings.authAudit.actor')}</th>
            {/if}
            <th>{$t('settings.authAudit.oldValue')}</th>
            <th>{$t('settings.authAudit.newValue')}</th>
            <th>{$t('settings.authAudit.ip')}</th>
          </tr>
        </thead>
        <tbody>
          {#each entries as entry (entry.uuid)}
            <tr>
              <td class="mono">{formatDateTime(entry.created_at)}</td>
              <td class="mono">{entry.config_key}</td>
              <td>
                <span class="change-type {entry.change_type}">
                  {changeTypeLabel(entry.change_type)}
                </span>
              </td>
              {#if hasActor}
                <td>{entry.changed_by_email || '—'}</td>
              {/if}
              <td class="value">{displayValue(entry.old_value)}</td>
              <td class="value">{displayValue(entry.new_value)}</td>
              <td class="mono">{entry.ip_address || '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    {#if hasMore}
      <div class="more-actions">
        <button type="button" class="btn-secondary" on:click={loadMore} disabled={loadingMore}>
          {loadingMore ? $t('settings.authAudit.loading') : $t('settings.authAudit.loadMore')}
        </button>
      </div>
    {/if}
  {/if}

  <p class="panel-note">{$t('settings.authAudit.redactionNote')}</p>
</div>

<style>
  .audit-panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .panel-description,
  .panel-status,
  .panel-note {
    font-size: 0.75rem;
    color: var(--color-text-secondary, var(--text-secondary));
    margin: 0;
  }

  .panel-note {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--color-border, var(--border-color));
    border-radius: 6px;
    background: var(--color-bg-secondary, var(--background-color));
  }

  .controls {
    display: flex;
    align-items: flex-end;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .category-label {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
  }

  .label-text {
    color: var(--color-text-secondary, var(--text-secondary));
    font-size: 0.6875rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
  }

  select {
    padding: 0.25rem 0.375rem;
    border: 1px solid var(--color-border, var(--border-color));
    border-radius: 6px;
    background: var(--color-bg, var(--surface-color));
    color: var(--color-text, var(--text-color));
    font-size: 0.75rem;
    min-width: 12rem;
    height: 1.75rem;
  }

  .btn-secondary {
    padding: 0.25rem 0.625rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.75rem;
    height: 1.75rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-color);
    transition: all 0.2s ease;
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--button-hover);
  }

  .btn-secondary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .table-scroll {
    overflow-x: auto;
    max-height: 420px;
    overflow-y: auto;
    border: 1px solid var(--color-border, var(--border-color));
    border-radius: 6px;
    -webkit-overflow-scrolling: touch;
  }

  .audit-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.75rem;
  }

  .audit-table th,
  .audit-table td {
    padding: 0.375rem 0.5rem;
    text-align: left;
    border-bottom: 1px solid var(--color-border, var(--border-color));
    color: var(--color-text, var(--text-color));
    white-space: nowrap;
  }

  .audit-table th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: var(--color-bg-secondary, var(--table-header-bg));
    font-weight: 600;
    font-size: 0.6875rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    color: var(--color-text-secondary, var(--text-secondary));
  }

  .audit-table tbody tr:hover {
    background: var(--color-bg-secondary, var(--table-row-hover));
  }

  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.6875rem;
    color: var(--color-text-secondary, var(--text-secondary));
  }

  .value {
    max-width: 18rem;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .change-type {
    padding: 0.125rem 0.375rem;
    border-radius: 3px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    background: rgba(59, 130, 246, 0.15);
    color: rgb(37, 99, 235);
  }

  .change-type.create {
    background: rgba(34, 197, 94, 0.15);
    color: rgb(22, 163, 74);
  }

  .change-type.delete {
    background: rgba(239, 68, 68, 0.15);
    color: rgb(220, 38, 38);
  }

  :global([data-theme='dark']) .change-type {
    background: rgba(59, 130, 246, 0.2);
    color: rgb(96, 165, 250);
  }

  :global([data-theme='dark']) .change-type.create {
    background: rgba(34, 197, 94, 0.2);
    color: rgb(74, 222, 128);
  }

  :global([data-theme='dark']) .change-type.delete {
    background: rgba(239, 68, 68, 0.2);
    color: rgb(248, 113, 113);
  }

  .more-actions {
    display: flex;
    justify-content: center;
  }

  @media (max-width: 768px) {
    .controls {
      flex-direction: column;
      align-items: stretch;
    }

    select,
    .btn-secondary {
      width: 100%;
      min-height: 44px;
      height: auto;
      font-size: 1rem;
    }
  }
</style>
