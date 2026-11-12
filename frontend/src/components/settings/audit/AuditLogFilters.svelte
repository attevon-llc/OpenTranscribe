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
        {#each eventTypes as type (type.value)}
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
    <button class="btn btn-primary btn-sm btn-apply" disabled={resourceInvalid} on:click={() => dispatch('apply')}>
      {$t('settings.auditLog.apply')}
    </button>
    <button class="btn btn-secondary btn-sm btn-clear" on:click={clear}>{$t('common.clear')}</button>
    {#if canExport}
      <div class="export-actions">
        <button class="btn btn-secondary btn-sm" on:click={() => dispatch('export', 'csv')}>CSV</button>
        <button class="btn btn-secondary btn-sm" on:click={() => dispatch('export', 'json')}>JSON</button>
      </div>
    {/if}
  </div>
</div>

<style>
  .filters {
    background: var(--color-bg-secondary);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    margin-bottom: var(--space-2);
  }

  .filter-row {
    display: flex;
    gap: var(--space-3);
    flex-wrap: wrap;
    align-items: flex-end;
  }

  .filter-row label {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    font-size: var(--font-size-xs);
  }

  .label-text {
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .filter-row input,
  .filter-row select {
    padding: var(--space-1) var(--space-1-5);
    font-size: var(--font-size-xs);
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
    align-self: flex-end;
  }

  .export-actions {
    display: flex;
    gap: var(--space-1);
    margin-inline-start: auto;
  }

  @media (max-width: 768px) {
    .filter-row {
      flex-direction: column;
      gap: var(--space-2);
    }

    .filter-row label {
      width: 100%;
    }

    .filter-row input,
    .filter-row select {
      height: auto;
      min-height: 44px;
      font-size: var(--font-size-base);
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
