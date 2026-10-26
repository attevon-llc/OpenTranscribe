<!--
  AuditLogFilters.svelte — the filter bar of an audit screen.

  Shared by the platform viewer (dates, event, outcome) and the organization panel, which adds a
  member picker and a resource uuid (`orgView`). It owns no data: it edits `filters` and tells the
  parent when to apply, clear or export.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import AuditUserPicker from '$components/settings/audit/AuditUserPicker.svelte';
  import {
    emptyAuditFilters,
    isAuditResourceUuid,
    type AuditFilterState,
  } from '$components/settings/audit/auditFilters';
  import { t } from '$stores/locale';

  export let filters: AuditFilterState;
  export let eventTypes: { value: string; label: string }[];
  /** Adds the member picker and the resource uuid field. */
  export let orgView = false;
  export let canExport = true;

  const dispatch = createEventDispatcher<{
    apply: void;
    clear: void;
    export: 'csv' | 'json';
  }>();

  $: resourceInvalid = !isAuditResourceUuid(filters.resourceUuid);

  function clear() {
    filters = emptyAuditFilters();
    dispatch('clear');
  }
</script>

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
    {#if orgView}
      <AuditUserPicker bind:userUuid={filters.userUuid} bind:userLabel={filters.userLabel} />
      <label>
        <span class="label-text">{$t('orgAudit.resourceFilter')}</span>
        <input
          class="uuid-input"
          type="text"
          dir="ltr"
          spellcheck="false"
          placeholder={$t('orgAudit.resourcePlaceholder')}
          aria-invalid={resourceInvalid}
          bind:value={filters.resourceUuid}
        />
      </label>
    {/if}
    <button class="btn-apply" disabled={resourceInvalid} on:click={() => dispatch('apply')}>
      {$t('settings.auditLog.apply')}
    </button>
    <button class="btn-clear" on:click={clear}>{$t('common.clear')}</button>
    {#if canExport}
      <div class="export-actions">
        <button class="btn-secondary" on:click={() => dispatch('export', 'csv')}>CSV</button>
        <button class="btn-secondary" on:click={() => dispatch('export', 'json')}>JSON</button>
      </div>
    {/if}
  </div>
</div>

<style>
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
    font-size: var(--font-size-xs);
  }

  .filter-row input,
  .filter-row select {
    padding: 0.25rem 0.375rem;
    font-size: 0.75rem;
    height: 1.75rem;
  }

  .filter-row input[type='date'] {
    width: 7.5rem;
  }

  .filter-row select {
    min-width: 6rem;
  }

  .uuid-input {
    width: 17rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
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

  .btn-apply:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.3);
  }

  .btn-apply:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-apply:disabled {
    opacity: 0.5;
    cursor: not-allowed;
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
    margin-inline-start: auto;
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
    }

    .filter-row input[type='date'],
    .filter-row select,
    .uuid-input {
      width: 100%;
    }

    .btn-apply,
    .btn-clear {
      width: 100%;
      min-height: 44px;
      height: auto;
    }

    .export-actions {
      margin-inline-start: 0;
      width: 100%;
    }

    .export-actions .btn-secondary {
      flex: 1;
    }
  }
</style>
