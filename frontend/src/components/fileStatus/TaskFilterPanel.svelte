<script lang="ts">
  import { t } from '../../stores/locale';
  import DateRangePicker from '$components/ui/DateRangePicker.svelte';
  import { format } from 'date-fns';

  // Two-way bound filter state (parent owns the source of truth + refetch reactivity)
  export let taskFilter: string;
  export let taskTypeFilter: string;
  export let taskAgeFilter: string;
  export let taskDateFrom: string;
  export let taskDateTo: string;
  export let fileStatus: any = null;

  /* The task query is keyed on `yyyy-MM-dd` strings, so the Date-based shared
     control is converted at this boundary. Parsed at an explicit local
     midnight: `new Date('2026-09-05')` is parsed as UTC and lands on the
     previous day for anyone west of Greenwich. */
  $: dateFrom = taskDateFrom ? new Date(`${taskDateFrom}T00:00:00`) : null;
  $: dateTo = taskDateTo ? new Date(`${taskDateTo}T00:00:00`) : null;

  function handleDateRangeChange(event: CustomEvent<{ from: Date | null; to: Date | null }>) {
    taskDateFrom = event.detail.from ? format(event.detail.from, 'yyyy-MM-dd') : '';
    taskDateTo = event.detail.to ? format(event.detail.to, 'yyyy-MM-dd') : '';
  }
</script>

<!-- Quick filter chips -->
<div class="quick-filters">
  <button class="quick-chip" class:active={taskFilter === 'all'} on:click={() => { taskFilter = 'all'; }}>{$t('fileStatus.allStatuses')}</button>
  <button class="quick-chip attention" class:active={taskFilter === 'needs_attention'} on:click={() => { taskFilter = 'needs_attention'; }}>
    {$t('fileStatus.filesNeedAttention')}
    {#if fileStatus?.has_problems}
      <span class="chip-badge">{fileStatus.problem_files.count}</span>
    {/if}
  </button>
  <button class="quick-chip" class:active={taskFilter === 'in_progress'} on:click={() => { taskFilter = 'in_progress'; }}>{$t('fileStatus.inProgress')}</button>
  <button class="quick-chip" class:active={taskFilter === 'pending'} on:click={() => { taskFilter = 'pending'; }}>{$t('common.pending')}</button>
  <button class="quick-chip" class:active={taskFilter === 'failed'} on:click={() => { taskFilter = 'failed'; }}>{$t('common.error')}</button>
  <button class="quick-chip" class:active={taskFilter === 'completed'} on:click={() => { taskFilter = 'completed'; }}>{$t('common.completed')}</button>
</div>

<div class="compact-filters">
    <select bind:value={taskTypeFilter} class="compact-filter-select">
      <option value="all">{$t('fileStatus.allTypes')}</option>
      <option value="transcription">{$t('fileStatus.transcription')}</option>
      <option value="summarization">{$t('fileStatus.summarization')}</option>
      <option value="search_indexing">{$t('search.settingsTitle')}</option>
    </select>

    <select bind:value={taskAgeFilter} class="compact-filter-select">
      <option value="all">{$t('fileStatus.allAges')}</option>
      <option value="today">{$t('fileStatus.last24h')}</option>
      <option value="week">{$t('fileStatus.lastWeek')}</option>
      <option value="month">{$t('fileStatus.lastMonth')}</option>
      <option value="older">{$t('fileStatus.older')}</option>
    </select>

    <div class="date-picker-host">
      <DateRangePicker from={dateFrom} to={dateTo} on:change={handleDateRangeChange} />
    </div>

    {#if taskFilter !== 'all' || taskTypeFilter !== 'all' || taskAgeFilter !== 'all' || taskDateFrom || taskDateTo}
      <button
        class="compact-clear-btn"
        on:click={() => {
          taskFilter = 'all';
          taskTypeFilter = 'all';
          taskAgeFilter = 'all';
          taskDateFrom = '';
          taskDateTo = '';
        }}
        title={$t('fileStatus.clearFilters')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    {/if}
  </div>

<style>
  /* Quick filter chips */
  .quick-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    margin-bottom: 0.75rem;
  }

  .quick-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.375rem 0.75rem;
    height: 30px;
    box-sizing: border-box;
    font-size: 0.8125rem;
    font-weight: 500;
    border: none;
    border-radius: 10px;
    background: rgba(59, 130, 246, 0.08);
    color: var(--primary-on-surface);
    cursor: pointer;
    transition: all 0.15s ease;
    white-space: nowrap;
  }

  :global([data-theme='dark']) .quick-chip,
  :global([data-theme='dark']) .quick-chip {
    background: rgba(96, 165, 250, 0.12);
    color: #93c5fd;
  }

  .quick-chip:hover {
    background: rgba(59, 130, 246, 0.15);
    transform: translateY(-1px);
  }

  :global([data-theme='dark']) .quick-chip:hover,
  :global([data-theme='dark']) .quick-chip:hover {
    background: rgba(96, 165, 250, 0.2);
  }

  .quick-chip.active {
    background: var(--primary-color);
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .quick-chip.active:hover {
    background: #2563eb;
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .quick-chip.attention .chip-badge {
    background: rgba(239, 68, 68, 0.15);
    color: #ef4444;
    font-size: 0.6875rem;
    padding: 1px 6px;
    border-radius: 10px;
    font-weight: 600;
  }

  .quick-chip.attention.active .chip-badge {
    background: rgba(255, 255, 255, 0.25);
    color: white;
  }

  .compact-filters {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.75rem;
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    margin-bottom: 1.5rem;
    width: fit-content;
  }

  .compact-filter-select {
    padding: 0.375rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    cursor: pointer;
    width: auto;
    min-width: 0;
    height: 30px;
    transition: border-color 0.15s ease;
  }

  .compact-filter-select:hover {
    border-color: var(--primary-color);
  }

  .compact-filter-select:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  /* Sizing only — the control's appearance is `ui/DateRangePicker`'s, which is
     the point of sharing it. This panel is a horizontal toolbar rather than a
     sidebar column, so it caps the width instead of filling one. */
  .date-picker-host {
    min-width: 190px;
  }

  .compact-clear-btn {
    padding: 0.35rem;
    background: var(--error-color);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
  }

  .compact-clear-btn:hover {
    background: var(--error-hover);
    transform: scale(1.1);
  }

  @media (max-width: 768px) {
    .compact-filters {
      padding: 0.6rem;
      gap: 0.4rem;
      width: 100%;
    }

    .compact-filter-select {
      font-size: 0.75rem;
      padding: 0.3rem 0.4rem;
    }

    .compact-clear-btn {
      width: 24px;
      height: 24px;
      padding: 0.3rem;
    }
  }
</style>
