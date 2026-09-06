<script lang="ts">
  import { DatePicker } from '@svelte-plugins/datepicker';
  import { format } from 'date-fns';
  import { createEventDispatcher, tick } from 'svelte';
  import { t } from '$stores/locale';

  /**
   * The app's date-range control.
   *
   * Both the gallery filter sidebar and the File Status task filter wrap
   * `@svelte-plugins/datepicker`, and each had written its own trigger button
   * and its own ~150 lines of theming for the plugin's DOM. They diverged: the
   * sidebar renders the calendar inline, themes it for light and dark, and
   * shows "Sep 5, 2026"; the File Status copy inherited none of that and
   * printed a raw `2026-09-05`. Same component, two appearances.
   *
   * This is the sidebar's version, extracted. Consumers supply the value and
   * receive a `change` event; nothing about how it looks is theirs to set.
   */

  /** Start of the range. `null` when unset. */
  export let from: Date | null = null;
  /** End of the range. `null` when unset. */
  export let to: Date | null = null;
  /** Whether dates after today can be picked. */
  export let enableFutureDates = true;
  /** Overrides the "select a range" placeholder. */
  export let placeholder = '';

  const dispatch = createEventDispatcher<{ change: { from: Date | null; to: Date | null } }>();

  let isOpen = false;
  let closing = false;
  let wrapper: HTMLDivElement;
  let dpStartDate: Date | string | null = from;
  let dpEndDate: Date | string | null = to;

  // Keep the plugin's internal state in step when a parent resets the range
  // (the sidebar's "clear" button does exactly that).
  $: if (from === null && to === null) {
    dpStartDate = null;
    dpEndDate = null;
  }

  // The calendar renders inline BELOW the trigger, so in a scrolled panel it
  // can open entirely out of view. Scroll it into range on open.
  $: if (isOpen && !closing) {
    tick().then(() => {
      const cal = wrapper?.querySelector('.calendars-container');
      if (cal) (cal as HTMLElement).scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  }

  function handleDateChange(event: { startDate: Date | string; endDate?: Date | string }) {
    const start = event.startDate ? new Date(event.startDate) : null;
    const end = event.endDate ? new Date(event.endDate) : null;

    from = start && !isNaN(start.getTime()) ? start : null;
    to = end && !isNaN(end.getTime()) ? end : null;

    // A range is only complete once both ends exist; closing on the first
    // click would make picking a range impossible. The delay lets the
    // `.closing` fade run before the calendar is removed.
    if (from && to) {
      closing = true;
      setTimeout(() => {
        isOpen = false;
        closing = false;
      }, 350);
    }
    dispatch('change', { from, to });
  }

  const label = (d: Date) => format(d, 'MMM d, yyyy');
</script>

<div class="datepicker-wrapper" class:closing bind:this={wrapper}>
  <DatePicker
    isRange
    {enableFutureDates}
    includeFont={false}
    bind:isOpen
    bind:startDate={dpStartDate}
    bind:endDate={dpEndDate}
    onDateChange={handleDateChange}
  >
    <button type="button" class="date-trigger-btn" on:click={() => (isOpen = !isOpen)}>
      <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="date-icon">
        <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
        <line x1="16" y1="2" x2="16" y2="6"></line>
        <line x1="8" y1="2" x2="8" y2="6"></line>
        <line x1="3" y1="10" x2="21" y2="10"></line>
      </svg>
      <span class="date-text">
        {#if from && to}
          {label(from)} — {label(to)}
        {:else if from}
          {label(from)} — ...
        {:else}
          {placeholder || $t('filter.selectDateRange')}
        {/if}
      </span>
    </button>
  </DatePicker>
</div>

<style>

  /* Date picker wrapper */
  .datepicker-wrapper {
    position: relative;
  }

  .date-trigger-btn {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    padding: 0.45rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-color);
    color: var(--text-color);
    font-size: 0.8rem;
    cursor: pointer;
    transition: border-color 0.2s ease;
    text-align: left;
  }

  .date-trigger-btn:hover {
    border-color: var(--primary-color-light, #93c5fd);
  }

  .date-icon {
    flex-shrink: 0;
    color: var(--text-secondary);
  }

  .date-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Datepicker theme — inline below trigger, light/dark mode */
  .datepicker-wrapper :global(.datepicker) {
    font-family: inherit;
  }

  /* Core layout: render inline below trigger, fit sidebar width */
  .datepicker-wrapper :global(.datepicker .calendars-container) {
    position: static !important;
    margin-top: 0.5rem;
    width: 100% !important;
    box-shadow: none !important;
    border-radius: 8px;
    opacity: 1;
    transition: opacity 0.3s ease;
    /* Theming */
    --datepicker-container-background: var(--surface-color, #fff);
    --datepicker-container-border: 1px solid var(--border-color, #e8e9ea);
    --datepicker-container-border-radius: 8px;
    --datepicker-container-box-shadow: none;
    --datepicker-container-font-family: inherit;
    --datepicker-container-width: 100%;
    --datepicker-color: var(--text-color, #21333d);
    --datepicker-border-color: var(--border-color, #e8e9ea);
    --datepicker-state-active: var(--primary-color, #3b82f6);
    --datepicker-state-hover: var(--hover-color, #e7f7fc);
    --datepicker-font-size-base: 0.8rem;
    /* Calendar sizing */
    --datepicker-calendar-width: 100%;
    --datepicker-calendar-padding: 4px 4px 12px;
    --datepicker-calendar-day-height: 32px;
    --datepicker-calendar-day-width: 32px;
    --datepicker-calendar-day-padding: 2px;
    --datepicker-calendar-day-font-size: 0.8rem;
    --datepicker-calendar-dow-font-size: 0.75rem;
    --datepicker-calendar-dow-margin-bottom: 6px;
    --datepicker-calendar-header-font-size: 0.95rem;
    --datepicker-calendar-header-padding: 8px 2px;
    --datepicker-calendar-header-margin: 0 0 6px 0;
    /* Colors — light mode */
    --datepicker-calendar-day-color: var(--text-color, #232a32);
    --datepicker-calendar-day-color-hover: var(--text-color, #232a32);
    --datepicker-calendar-day-background-hover: var(--hover-color, #f5f5f5);
    --datepicker-calendar-dow-color: var(--text-secondary, #8b9198);
    --datepicker-calendar-header-color: var(--text-color, #21333d);
    --datepicker-calendar-header-text-color: var(--text-color, #21333d);
    --datepicker-calendar-header-month-nav-color: var(--text-color, #21333d);
    --datepicker-calendar-header-month-nav-background-hover: var(--hover-color, #f5f5f5);
    --datepicker-calendar-today-border: 1px solid var(--text-color, #232a32);
    --datepicker-calendar-day-other-color: var(--text-secondary, #d1d3d6);
  }

  .datepicker-wrapper :global(.datepicker .calendars-container .calendar) {
    width: 100% !important;
    padding: 4px 4px 12px !important;
  }

  .datepicker-wrapper :global(.datepicker .calendars-container .calendar .month) {
    width: 100%;
  }

  .datepicker-wrapper :global(.datepicker .calendars-container .calendar .date span) {
    width: 32px !important;
    height: 32px !important;
    font-size: 0.8rem !important;
    padding: 2px !important;
  }

  .datepicker-wrapper :global(.datepicker .calendars-container .calendar .dow) {
    font-size: 0.75rem !important;
  }

  /* Fade out calendar on close */
  .datepicker-wrapper.closing :global(.datepicker .calendars-container) {
    opacity: 0;
  }

  /* Dark mode overrides */
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .calendars-container) {
    --datepicker-container-background: var(--surface-color, #1e293b);
    --datepicker-color: var(--text-color, #e2e8f0);
    --datepicker-container-border: 1px solid var(--border-color, #334155);
    --datepicker-border-color: var(--border-color, #334155);
    --datepicker-state-active: var(--primary-color, #3b82f6);
    --datepicker-state-hover: rgba(59, 130, 246, 0.15);
    --datepicker-calendar-day-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-day-color-hover: #fff;
    --datepicker-calendar-day-color-disabled: var(--text-secondary, #64748b);
    --datepicker-calendar-day-background-hover: rgba(255, 255, 255, 0.1);
    --datepicker-calendar-dow-color: var(--text-secondary, #94a3b8);
    --datepicker-calendar-header-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-text-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-month-nav-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-month-nav-background-hover: rgba(255, 255, 255, 0.1);
    --datepicker-calendar-today-border: 1px solid var(--text-color, #e2e8f0);
    --datepicker-calendar-day-other-color: var(--text-secondary, #475569);
    /* Range selection colors */
    --datepicker-calendar-range-background: rgba(59, 130, 246, 0.2);
    --datepicker-calendar-range-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-range-start-end-background: #3b82f6;
    --datepicker-calendar-range-start-end-color: #fff;
    --datepicker-calendar-range-included-background: rgba(59, 130, 246, 0.12);
    --datepicker-calendar-range-included-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-range-included-box-shadow: inset 20px 0 0 rgba(59, 130, 246, 0.12);
    /* Box-shadows behind start/end circles */
    --datepicker-calendar-range-start-box-shadow: inset -20px 0 0 rgba(59, 130, 246, 0.15);
    --datepicker-calendar-range-end-box-shadow: inset 20px 0 0 rgba(59, 130, 246, 0.15);
    --datepicker-calendar-range-start-box-shadow-selected: inset -20px 0 0 var(--surface-color, #1e293b);
    --datepicker-calendar-range-end-box-shadow-selected: inset 20px 0 0 var(--surface-color, #1e293b);
  }

  /* Invert nav arrow icons in dark mode (they're base64 black SVGs) */
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .icon-previous-month),
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .icon-next-month),
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .icon-next-year),
  :global([data-theme='dark']) .datepicker-wrapper :global(.datepicker .icon-previous-year) {
    filter: invert(1);
  }
</style>
