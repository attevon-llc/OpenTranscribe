<script lang="ts">
  import { t } from '../../stores/locale';
  import { DatePicker } from '@svelte-plugins/datepicker';
  import { format } from 'date-fns';

  // Two-way bound filter state (parent owns the source of truth + refetch reactivity)
  export let taskFilter: string;
  export let taskTypeFilter: string;
  export let taskAgeFilter: string;
  export let taskDateFrom: string;
  export let taskDateTo: string;
  export let fileStatus: any = null;

  // Date picker state
  let datePickerOpen = false;
  let dpStartDate: Date | string | null = null;
  let dpEndDate: Date | string | null = null;

  function handleDatePickerChange(event: { startDate: Date | string; endDate?: Date | string }) {
    const start = event.startDate ? new Date(event.startDate) : null;
    const end = event.endDate ? new Date(event.endDate) : null;
    if (start && !isNaN(start.getTime())) {
      taskDateFrom = format(start, 'yyyy-MM-dd');
    }
    if (end && !isNaN(end.getTime())) {
      taskDateTo = format(end, 'yyyy-MM-dd');
      datePickerOpen = false;
    }
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

    <div class="date-picker-inline">
      <DatePicker
        isRange
        enableFutureDates
        includeFont={false}
        bind:isOpen={datePickerOpen}
        bind:startDate={dpStartDate}
        bind:endDate={dpEndDate}
        onDateChange={handleDatePickerChange}
      >
        <button
          type="button"
          class="date-trigger-btn"
          on:click={() => datePickerOpen = !datePickerOpen}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
            <line x1="16" y1="2" x2="16" y2="6"></line>
            <line x1="8" y1="2" x2="8" y2="6"></line>
            <line x1="3" y1="10" x2="21" y2="10"></line>
          </svg>
          <span class="date-text">
            {#if taskDateFrom && taskDateTo}
              {taskDateFrom} — {taskDateTo}
            {:else if taskDateFrom}
              {taskDateFrom} — ...
            {:else}
              {$t('filter.selectDateRange')}
            {/if}
          </span>
        </button>
      </DatePicker>
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
          dpStartDate = null;
          dpEndDate = null;
          datePickerOpen = false;
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
    color: #3b82f6;
    cursor: pointer;
    transition: all 0.15s ease;
    white-space: nowrap;
  }

  :global(.dark) .quick-chip,
  :global([data-theme='dark']) .quick-chip {
    background: rgba(96, 165, 250, 0.12);
    color: #93c5fd;
  }

  .quick-chip:hover {
    background: rgba(59, 130, 246, 0.15);
    transform: translateY(-1px);
  }

  :global(.dark) .quick-chip:hover,
  :global([data-theme='dark']) .quick-chip:hover {
    background: rgba(96, 165, 250, 0.2);
  }

  .quick-chip.active {
    background: #3b82f6;
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

  .date-picker-inline {
    position: relative;
  }

  .date-trigger-btn {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.375rem 0.625rem;
    height: 30px;
    box-sizing: border-box;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    font-family: inherit;
    cursor: pointer;
    transition: border-color 0.15s ease;
    white-space: nowrap;
  }

  .date-trigger-btn:hover {
    border-color: var(--primary-color);
  }

  .date-text {
    color: var(--text-secondary);
    font-size: 0.75rem;
  }

  .date-picker-inline :global(.datepicker) {
    font-family: inherit;
  }

  .date-picker-inline :global(.datepicker .calendars-container) {
    position: absolute !important;
    top: calc(100% + 4px);
    right: 0;
    z-index: 100;
    width: 280px !important;
    border-radius: 10px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
    --datepicker-container-background: var(--surface-color, #fff);
    --datepicker-container-border: 1px solid var(--border-color, #e8e9ea);
    --datepicker-container-border-radius: 10px;
    --datepicker-container-box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
    --datepicker-color: var(--text-color, #21333d);
    --datepicker-border-color: var(--border-color, #e8e9ea);
    --datepicker-state-active: var(--primary-color, #3b82f6);
    --datepicker-state-hover: var(--hover-color, #e7f7fc);
    --datepicker-font-size-base: 0.8rem;
    --datepicker-calendar-width: 100%;
    --datepicker-calendar-padding: 4px 4px 12px;
    --datepicker-calendar-day-height: 32px;
    --datepicker-calendar-day-width: 32px;
    --datepicker-calendar-day-font-size: 0.8rem;
    --datepicker-calendar-dow-font-size: 0.75rem;
    --datepicker-calendar-header-font-size: 0.95rem;
    --datepicker-calendar-day-color: var(--text-color, #232a32);
    --datepicker-calendar-day-background-hover: var(--hover-color, #f5f5f5);
    --datepicker-calendar-dow-color: var(--text-secondary, #8b9198);
    --datepicker-calendar-header-color: var(--text-color, #21333d);
    --datepicker-calendar-header-text-color: var(--text-color, #21333d);
    --datepicker-calendar-header-month-nav-color: var(--text-color, #21333d);
    --datepicker-calendar-header-month-nav-background-hover: var(--hover-color, #f5f5f5);
    --datepicker-calendar-today-border: 1px solid var(--text-color, #232a32);
    --datepicker-calendar-day-other-color: var(--text-secondary, #d1d3d6);
  }

  .date-picker-inline :global(.datepicker .calendars-container .calendar) {
    width: 100% !important;
    padding: 4px 4px 12px !important;
  }

  .date-picker-inline :global(.datepicker .calendars-container .calendar .date span) {
    width: 32px !important;
    height: 32px !important;
    font-size: 0.8rem !important;
  }

  :global(.dark) .date-picker-inline :global(.datepicker .calendars-container),
  :global([data-theme='dark']) .date-picker-inline :global(.datepicker .calendars-container) {
    --datepicker-container-background: var(--surface-color, #1e293b);
    --datepicker-container-box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
    --datepicker-color: var(--text-color, #e2e8f0);
    --datepicker-state-hover: rgba(59, 130, 246, 0.15);
    --datepicker-calendar-day-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-day-background-hover: rgba(255, 255, 255, 0.08);
    --datepicker-calendar-dow-color: var(--text-secondary, #94a3b8);
    --datepicker-calendar-header-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-text-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-month-nav-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-month-nav-background-hover: rgba(255, 255, 255, 0.08);
    --datepicker-calendar-today-border: 1px solid var(--text-color, #e2e8f0);
    --datepicker-calendar-day-other-color: var(--text-secondary, #475569);
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
