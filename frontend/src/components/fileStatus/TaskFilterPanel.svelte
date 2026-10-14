<script lang="ts">
  import { t } from '../../stores/locale';
  import DateRangePicker from '$components/ui/DateRangePicker.svelte';
  import FilterChipButton from '$components/ui/FilterChipButton.svelte';
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

<!-- Quick filter chips: the same toggle chip the gallery filters use. -->
<div class="quick-filters" role="group" aria-label={$t('fileStatus.allStatuses')}>
  <FilterChipButton selected={taskFilter === 'all'} on:click={() => { taskFilter = 'all'; }}>{$t('fileStatus.allStatuses')}</FilterChipButton>
  <FilterChipButton
    selected={taskFilter === 'needs_attention'}
    count={fileStatus?.has_problems ? fileStatus.problem_files.count : null}
    on:click={() => { taskFilter = 'needs_attention'; }}
  >{$t('fileStatus.filesNeedAttention')}</FilterChipButton>
  <FilterChipButton selected={taskFilter === 'in_progress'} on:click={() => { taskFilter = 'in_progress'; }}>{$t('fileStatus.inProgress')}</FilterChipButton>
  <FilterChipButton selected={taskFilter === 'pending'} on:click={() => { taskFilter = 'pending'; }}>{$t('common.pending')}</FilterChipButton>
  <FilterChipButton selected={taskFilter === 'failed'} on:click={() => { taskFilter = 'failed'; }}>{$t('common.error')}</FilterChipButton>
  <FilterChipButton selected={taskFilter === 'completed'} on:click={() => { taskFilter = 'completed'; }}>{$t('common.completed')}</FilterChipButton>
</div>

<div class="compact-filters">
  <select bind:value={taskTypeFilter} class="filter-select" aria-label={$t('fileStatus.allTypes')}>
    <option value="all">{$t('fileStatus.allTypes')}</option>
    <option value="transcription">{$t('fileStatus.transcription')}</option>
    <option value="summarization">{$t('fileStatus.summarization')}</option>
    <option value="search_indexing">{$t('search.settingsTitle')}</option>
  </select>

  <select bind:value={taskAgeFilter} class="filter-select" aria-label={$t('fileStatus.allAges')}>
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
      type="button"
      class="compact-clear-btn"
      on:click={() => {
        taskFilter = 'all';
        taskTypeFilter = 'all';
        taskAgeFilter = 'all';
        taskDateFrom = '';
        taskDateTo = '';
      }}
      title={$t('fileStatus.clearFilters')}
      aria-label={$t('fileStatus.clearFilters')}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <line x1="18" y1="6" x2="6" y2="18"></line>
        <line x1="6" y1="6" x2="18" y2="18"></line>
      </svg>
    </button>
  {/if}
</div>

<style>
  .quick-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }

  /* One row of fields that share the app's field height, radius and focus ring
     (styles/form-elements.css). Only layout is decided here. */
  .compact-filters {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 1.5rem;
  }

  .filter-select {
    width: auto;
    min-width: 9.5rem;
  }

  .date-picker-host {
    flex: 0 1 15rem;
    min-width: 12rem;
  }

  .compact-clear-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2.375rem;
    height: 2.375rem;
    padding: 0;
    border: 1px solid var(--field-border);
    border-radius: var(--field-radius);
    background: var(--field-bg);
    color: var(--text-secondary);
    box-shadow: none;
    transform: none;
  }

  .compact-clear-btn:hover:not(:disabled) {
    border-color: var(--error-color);
    background: var(--field-bg);
    color: var(--error-color);
    box-shadow: none;
    transform: none;
  }

  @media (max-width: 768px) {
    .filter-select,
    .date-picker-host {
      flex: 1 1 100%;
      width: 100%;
    }
  }
</style>
