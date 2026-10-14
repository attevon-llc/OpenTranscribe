<script lang="ts">
  import { DatePicker } from '@svelte-plugins/datepicker';
  import { format, parseISO, isValid } from 'date-fns';
  import { createEventDispatcher, onDestroy, tick } from 'svelte';
  import { t } from '$stores/locale';
  import { portal } from '$lib/actions/portal';
  import { clickOutside } from '$lib/actions/clickOutside';
  import { escapeKey } from '$lib/actions/escapeKey';
  import { computePopoverPlacement, type PopoverPlacement } from '$lib/popoverPosition';

  /**
   * The app's date-range control, used by the gallery filter sidebar AND the File
   * Status filters (one implementation, so they cannot drift apart again).
   *
   * A field-styled trigger shows the range. Activating it opens a POPOVER: a
   * `position: fixed` layer portaled to <body> and positioned from the trigger's
   * rect, holding the typed From/To fields (the keyboard path, #750) and the
   * calendar. It floats over the page instead of growing the card it sits in, so a
   * filter column never reflows when you pick a range, and the scrolling sidebar
   * cannot clip it. It flips above the trigger when there is no room below, clamps
   * to the viewport, closes on Escape / outside click / completing a range, and
   * returns focus to the trigger. On a narrow viewport it becomes a bottom sheet.
   *
   * Consumers supply the value and receive a `change` event; nothing about how it
   * looks is theirs to set.
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
  let trigger: HTMLButtonElement;
  let layer: HTMLDivElement | undefined;
  let placement: PopoverPlacement | null = null;
  let resizeObserver: ResizeObserver | undefined;
  let dpStartDate: Date | string | null = from;
  let dpEndDate: Date | string | null = to;

  const instanceId = `date-range-${Math.random().toString(36).slice(2, 9)}`;
  const layerId = `${instanceId}-popover`;

  const dateToInputValue = (d: Date | null): string => (d && isValid(d) ? format(d, 'yyyy-MM-dd') : '');
  $: fromInputValue = dateToInputValue(from);
  $: toInputValue = dateToInputValue(to);

  // `enableFutureDates=false` must mean the same thing to the typed field as
  // it does to the calendar plugin, or the two controls disagree about what's
  // selectable.
  $: maxAttr = enableFutureDates ? undefined : format(new Date(), 'yyyy-MM-dd');

  function parseTypedDate(value: string): Date | null {
    if (!value) return null;
    const parsed = parseISO(value);
    return isValid(parsed) ? parsed : null;
  }

  /** Typed input must write BOTH the props and the plugin's own start/end
   * state, or the calendar renders a different range than the text field. */
  function handleFromTyped(event: Event) {
    const parsed = parseTypedDate((event.target as HTMLInputElement).value);
    from = parsed;
    dpStartDate = parsed;
    dispatch('change', { from, to });
  }

  function handleToTyped(event: Event) {
    const parsed = parseTypedDate((event.target as HTMLInputElement).value);
    to = parsed;
    dpEndDate = parsed;
    dispatch('change', { from, to });
  }

  // Keep the plugin's internal state in step when a parent resets the range
  // (the sidebar's "clear" button does exactly that).
  $: if (from === null && to === null) {
    dpStartDate = null;
    dpEndDate = null;
  }

  function reposition() {
    if (!trigger || !layer) return;
    const rect = trigger.getBoundingClientRect();
    placement = computePopoverPlacement(
      { top: rect.top, left: rect.left, width: rect.width, height: rect.height },
      layer.offsetHeight,
      { width: window.innerWidth, height: window.innerHeight },
      { rtl: document.documentElement.dir === 'rtl' }
    );
  }

  async function open() {
    isOpen = true;
    await tick();
    reposition();
    if (layer && typeof ResizeObserver !== 'undefined') {
      resizeObserver?.disconnect();
      resizeObserver = new ResizeObserver(reposition);
      resizeObserver.observe(layer);
    }
    layer?.querySelector<HTMLInputElement>('input[type="date"]')?.focus();
  }

  function close(restoreFocus = true) {
    if (!isOpen) return;
    isOpen = false;
    placement = null;
    resizeObserver?.disconnect();
    if (restoreFocus) trigger?.focus();
  }

  onDestroy(() => resizeObserver?.disconnect());

  function clearRange() {
    from = null;
    to = null;
    dpStartDate = null;
    dpEndDate = null;
    dispatch('change', { from, to });
  }

  function handleDateChange(event: { startDate: Date | string; endDate?: Date | string }) {
    const start = event.startDate ? new Date(event.startDate) : null;
    const end = event.endDate ? new Date(event.endDate) : null;

    from = start && !isNaN(start.getTime()) ? start : null;
    to = end && !isNaN(end.getTime()) ? end : null;

    dispatch('change', { from, to });
    // A range is only complete once both ends exist; closing on the first
    // click would make picking a range impossible.
    if (from && to) close();
  }

  const label = (d: Date) => format(d, 'MMM d, yyyy');
</script>

<svelte:window on:resize={reposition} on:scroll|capture={reposition} />

<div class="drp">
  <button
    bind:this={trigger}
    type="button"
    class="date-trigger-btn"
    aria-haspopup="dialog"
    aria-expanded={isOpen}
    aria-controls={isOpen ? layerId : undefined}
    on:click={() => (isOpen ? close() : open())}
  >
    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="date-icon" aria-hidden="true">
      <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
      <line x1="16" y1="2" x2="16" y2="6"></line>
      <line x1="8" y1="2" x2="8" y2="6"></line>
      <line x1="3" y1="10" x2="21" y2="10"></line>
    </svg>
    <span class="date-text" class:placeholder={!from}>
      {#if from && to}
        {label(from)} — {label(to)}
      {:else if from}
        {label(from)} — ...
      {:else}
        {placeholder || $t('filter.selectDateRange')}
      {/if}
    </span>
  </button>
</div>

{#if isOpen}
  {#if placement?.sheet}
    <div class="drp-scrim" aria-hidden="true" use:portal></div>
  {/if}
  <div
    use:portal
    use:clickOutside={{ ignore: [trigger] }}
    on:click_outside={() => close(false)}
    use:escapeKey={{ onEscape: () => close() }}
    bind:this={layer}
    id={layerId}
    class="drp-layer"
    class:sheet={placement?.sheet}
    class:above={placement?.side === 'above'}
    role="dialog"
    aria-label={placeholder || $t('filter.selectDateRange')}
    style={placement
      ? placement.sheet
        ? `max-height:${placement.maxHeight}px`
        : `top:${placement.top}px;left:${placement.left}px;width:${placement.width}px;max-height:${placement.maxHeight}px`
      : 'visibility:hidden;top:0;left:0'}
  >
    <div class="typed-date-fields">
      <div class="typed-date-field">
        <label for="{instanceId}-from">{$t('common.from')}</label>
        <input
          id="{instanceId}-from"
          type="date"
          class="field-sm"
          value={fromInputValue}
          max={toInputValue || maxAttr}
          title={$t('filter.fromDateTooltip')}
          on:change={handleFromTyped}
        />
      </div>
      <div class="typed-date-field">
        <label for="{instanceId}-to">{$t('common.to')}</label>
        <input
          id="{instanceId}-to"
          type="date"
          class="field-sm"
          value={toInputValue}
          min={fromInputValue || undefined}
          max={maxAttr}
          title={$t('filter.toDateTooltip')}
          on:change={handleToTyped}
        />
      </div>
    </div>
    <div class="drp-calendar">
      <DatePicker
        isRange
        alwaysShow
        isOpen
        {enableFutureDates}
        includeFont={false}
        bind:startDate={dpStartDate}
        bind:endDate={dpEndDate}
        onDateChange={handleDateChange}
      />
    </div>
    {#if from || to}
      <div class="drp-footer">
        <button type="button" class="drp-clear" on:click={clearRange}>{$t('filter.clearDates')}</button>
      </div>
    {/if}
  </div>
{/if}

<style>
  .drp {
    position: relative;
  }

  /* The trigger is a field: same height, radius, border and focus ring as every input and
     select (see styles/form-elements.css), with a calendar glyph and a chevron. */
  .date-trigger-btn {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    margin: 0;
    padding: var(--field-padding-y) var(--field-padding-x);
    padding-inline-end: 2.25rem;
    border: 1px solid var(--field-border);
    border-radius: var(--field-radius);
    background-color: var(--field-bg);
    background-image: var(--field-chevron);
    background-repeat: no-repeat;
    background-position: right 0.75rem center;
    background-size: 1rem;
    color: var(--text-color);
    box-shadow: none;
    font-family: inherit;
    font-size: var(--field-font-size);
    font-weight: 400;
    line-height: var(--field-line-height);
    text-align: start;
    cursor: pointer;
    transform: none;
  }

  :global([dir='rtl']) .date-trigger-btn {
    background-position: left 0.75rem center;
  }

  .date-trigger-btn:hover:not(:disabled) {
    border-color: var(--field-border-hover);
    background-color: var(--field-bg);
    box-shadow: none;
    transform: none;
  }

  .date-trigger-btn:focus-visible,
  .date-trigger-btn[aria-expanded='true'] {
    border-color: var(--primary-color);
    box-shadow: var(--field-focus-ring);
    outline: 2px solid transparent;
    outline-offset: 0;
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

  .date-text.placeholder {
    color: var(--text-muted);
  }

  /* The floating layer. Fixed to the viewport and portaled to <body> (see the script
     note), so these rules cannot depend on any ancestor. Sits above modals and the mobile
     filter overlay, below toasts. */
  .drp-layer {
    position: fixed;
    z-index: calc(var(--z-modal) + 10);
    box-sizing: border-box;
    overflow-y: auto;
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 12px;
    background: var(--surface-color);
    color: var(--text-color);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18), 0 2px 6px rgba(0, 0, 0, 0.08);
    animation: drp-in 0.12s ease-out;
  }

  .drp-layer.above {
    animation-name: drp-in-above;
  }

  .drp-layer.sheet {
    inset: auto 0 0 0;
    width: auto;
    padding-bottom: calc(0.75rem + env(safe-area-inset-bottom, 0px));
    border-radius: 16px 16px 0 0;
    animation-name: drp-sheet-in;
  }

  .drp-scrim {
    position: fixed;
    inset: 0;
    z-index: calc(var(--z-modal) + 9);
    background: rgba(0, 0, 0, 0.4);
  }

  @keyframes drp-in {
    from { opacity: 0; transform: translateY(-4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  @keyframes drp-in-above {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  @keyframes drp-sheet-in {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
  }

  @media (prefers-reduced-motion: reduce) {
    .drp-layer {
      animation: none;
    }
  }

  .typed-date-fields {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }

  .typed-date-field {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
  }

  /* The calendar below already offers the picker, and the native indicator costs width the
     two fields cannot spare. */
  .typed-date-field input::-webkit-calendar-picker-indicator {
    display: none;
  }

  .typed-date-field label {
    font-size: var(--font-size-xs, 0.75rem);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .drp-footer {
    display: flex;
    justify-content: flex-end;
    margin-top: 0.5rem;
  }

  .drp-clear {
    padding: 0.25rem 0.5rem;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: var(--primary-on-surface);
    box-shadow: none;
    font-size: 0.8125rem;
    font-weight: 500;
    transform: none;
  }

  .drp-clear:hover:not(:disabled) {
    background: var(--button-hover);
    box-shadow: none;
    transform: none;
  }

  /* Datepicker theme: the plugin renders its own container; with `alwaysShow` + `isOpen`
     it is permanently displayed inside this layer, so it is made static and borderless
     and the layer provides the surface. */
  .drp-layer :global(.datepicker) {
    font-family: inherit;
  }

  .drp-layer :global(.datepicker .calendars-container) {
    position: static !important;
    width: 100% !important;
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
    /* Theming */
    --datepicker-container-background: transparent;
    --datepicker-container-border: none;
    --datepicker-container-box-shadow: none;
    --datepicker-container-font-family: inherit;
    --datepicker-container-width: 100%;
    --datepicker-color: var(--text-color);
    --datepicker-border-color: var(--border-color);
    --datepicker-state-active: var(--primary-color);
    --datepicker-state-hover: var(--hover-color);
    --datepicker-font-size-base: 0.8125rem;
    /* Calendar sizing */
    --datepicker-calendar-width: 100%;
    --datepicker-calendar-padding: 0;
    --datepicker-calendar-day-height: 34px;
    --datepicker-calendar-day-width: 34px;
    --datepicker-calendar-day-padding: 2px;
    --datepicker-calendar-day-font-size: 0.8125rem;
    --datepicker-calendar-dow-font-size: 0.75rem;
    --datepicker-calendar-dow-margin-bottom: 6px;
    --datepicker-calendar-header-font-size: 0.9375rem;
    --datepicker-calendar-header-padding: 4px 2px;
    --datepicker-calendar-header-margin: 0 0 6px 0;
    /* Colours */
    --datepicker-calendar-day-color: var(--text-color);
    --datepicker-calendar-day-color-hover: var(--text-color);
    --datepicker-calendar-day-background-hover: var(--hover-color);
    --datepicker-calendar-dow-color: var(--text-secondary);
    --datepicker-calendar-header-color: var(--text-color);
    --datepicker-calendar-header-text-color: var(--text-color);
    --datepicker-calendar-header-month-nav-color: var(--text-color);
    --datepicker-calendar-header-month-nav-background-hover: var(--hover-color);
    --datepicker-calendar-today-border: 1px solid var(--text-color);
    --datepicker-calendar-day-other-color: var(--text-secondary);
  }

  /* The global `button` rule (background, border, shadow, hover scale) would paint every
     calendar day and nav arrow as a raised button. `:where()` keeps this at the lowest
     specificity so the plugin's own selected/range backgrounds still win. */
  .drp-layer :global(:where(.datepicker .calendars-container button)),
  .drp-layer :global(:where(.datepicker .calendars-container button):hover:not(:disabled)) {
    border: none;
    border-radius: 0;
    background-color: transparent;
    box-shadow: none;
    transform: none;
    font-weight: 400;
    padding: 0;
  }

  .drp-layer :global(.datepicker .calendars-container .calendar) {
    width: 100% !important;
    padding: 0 !important;
  }

  .drp-layer :global(.datepicker .calendars-container .calendar .month) {
    width: 100%;
  }

  .drp-layer :global(.datepicker .calendars-container .calendar .date span) {
    width: 34px !important;
    height: 34px !important;
    font-size: 0.8125rem !important;
    padding: 2px !important;
  }

  .drp-layer :global(.datepicker .calendars-container .calendar .dow) {
    font-size: 0.75rem !important;
  }

  :global([data-theme='dark']) .drp-layer {
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55), 0 2px 6px rgba(0, 0, 0, 0.3);
  }

  :global([data-theme='dark']) .drp-layer :global(.datepicker .calendars-container) {
    --datepicker-state-hover: rgba(59, 130, 246, 0.15);
    --datepicker-calendar-day-color-hover: #fff;
    --datepicker-calendar-day-color-disabled: var(--text-secondary);
    --datepicker-calendar-day-background-hover: rgba(255, 255, 255, 0.1);
    --datepicker-calendar-header-month-nav-background-hover: rgba(255, 255, 255, 0.1);
    /* Range selection colours */
    --datepicker-calendar-range-background: rgba(59, 130, 246, 0.2);
    --datepicker-calendar-range-color: var(--text-color);
    --datepicker-calendar-range-start-end-background: var(--primary-color);
    --datepicker-calendar-range-start-end-color: #fff;
    --datepicker-calendar-range-included-background: rgba(59, 130, 246, 0.12);
    --datepicker-calendar-range-included-color: var(--text-color);
    --datepicker-calendar-range-included-box-shadow: inset 20px 0 0 rgba(59, 130, 246, 0.12);
    --datepicker-calendar-range-start-box-shadow: inset -20px 0 0 rgba(59, 130, 246, 0.15);
    --datepicker-calendar-range-end-box-shadow: inset 20px 0 0 rgba(59, 130, 246, 0.15);
    --datepicker-calendar-range-start-box-shadow-selected: inset -20px 0 0 var(--surface-color);
    --datepicker-calendar-range-end-box-shadow-selected: inset 20px 0 0 var(--surface-color);
  }

  /* The plugin's month/year arrows are black base64 SVGs. */
  :global([data-theme='dark']) .drp-layer :global(.datepicker .icon-previous-month),
  :global([data-theme='dark']) .drp-layer :global(.datepicker .icon-next-month),
  :global([data-theme='dark']) .drp-layer :global(.datepicker .icon-next-year),
  :global([data-theme='dark']) .drp-layer :global(.datepicker .icon-previous-year) {
    filter: invert(1);
  }
</style>
