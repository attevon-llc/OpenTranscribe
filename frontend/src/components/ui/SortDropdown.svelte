<script context="module" lang="ts">
  export interface SortOption {
    /** Value sent in the `change` event / used as the sort query param. */
    value: string;
    /** i18n key for the option's display label. */
    label: string;
    /** True for options with a fixed order (e.g. "relevance") — no toggle-direction affordance. */
    noDirection?: boolean;
  }
</script>

<script lang="ts">
  import { t } from '$stores/locale';
  import { createEventDispatcher } from 'svelte';
  import { fade } from 'svelte/transition';
  import { clickOutside } from '$lib/actions/clickOutside';
  import { anchoredPanel } from '$lib/actions/anchoredPanel';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { SORT_ICON_BY_VALUE } from '$lib/ui/menuIcons';

  /** Available sort fields, in display order. */
  export let sortOptions: SortOption[];
  export let sortBy: string;
  export let sortOrder: 'asc' | 'desc' = 'desc';
  /** i18n key for the trigger button's accessible label. */
  export let ariaLabelKey: string;
  /** Menu alignment relative to the trigger. */
  export let align: 'left' | 'right' = 'left';

  const dispatch = createEventDispatcher<{
    change: { sortBy: string; sortOrder: 'asc' | 'desc' };
  }>();

  let isOpen = false;
  let buttonEl: HTMLButtonElement;
  // Portaled to <body> by anchoredPanel, so outside the wrapper.
  let menuEl: HTMLDivElement | undefined;

  $: currentOption = sortOptions.find(opt => opt.value === sortBy) || sortOptions[0];

  // Descending reads as a DOWN arrow, ascending as an UP arrow (UX review item 19).
  // Fixed-order options (relevance: best first) are descending by definition. The glyph
  // is drawn pointing the right way rather than flipped with a CSS rotation, so which way
  // it points is a property of the markup, not of a stylesheet cascade.
  const isDescending = (option: SortOption, order: 'asc' | 'desc') =>
    !!option.noDirection || order === 'desc';
  const ARROW_HEAD = { up: '5 12 12 5 19 12', down: '5 12 12 19 19 12' };

  function toggleDropdown() {
    isOpen = !isOpen;
  }

  function selectSort(option: SortOption) {
    if (option.noDirection) {
      // Fixed-order option (e.g. relevance): always desc, no toggle.
      sortBy = option.value;
      sortOrder = 'desc';
      dispatch('change', { sortBy: option.value, sortOrder: 'desc' });
    } else if (option.value === sortBy) {
      // Toggle direction if same field
      const newOrder = sortOrder === 'asc' ? 'desc' : 'asc';
      sortOrder = newOrder;
      dispatch('change', { sortBy, sortOrder: newOrder });
    } else {
      // Select new field with appropriate default direction
      sortBy = option.value;
      // Filename defaults to asc (A-Z), others default to desc (newest/longest first)
      const newOrder = option.value === 'filename' ? 'asc' : 'desc';
      sortOrder = newOrder;
      dispatch('change', { sortBy: option.value, sortOrder: newOrder });
    }
    isOpen = false;
  }

  function handleClickOutside() {
    isOpen = false;
  }
</script>

<div
  class="sort-dropdown"
  use:clickOutside={{ ignore: [menuEl ?? null] }}
  on:click_outside={handleClickOutside}>
  <button
    bind:this={buttonEl}
    type="button"
    class="pill-chip sort-button"
    on:click={toggleDropdown}
    aria-label={$t(ariaLabelKey)}
    aria-expanded={isOpen}
  >
    <!-- Sort icon -->
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <path d="m3 16 4 4 4-4" />
      <path d="M7 20V4" />
      <path d="m21 8-4-4-4 4" />
      <path d="M17 4v16" />
    </svg>

    {#if currentOption}
      <span class="sort-label">{$t(currentOption.label)}</span>
    {/if}

    <!-- Direction arrow: a real arrow (shaft + head), not a bare chevron/caret — issue #747
         §4.3. Down = descending, up = ascending, same on /search (the control is shared). -->
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      class="direction-arrow"
      aria-hidden="true"
    >
      {#if currentOption && isDescending(currentOption, sortOrder)}
        <line x1="12" y1="5" x2="12" y2="19"></line>
        <polyline points={ARROW_HEAD.down}></polyline>
      {:else}
        <line x1="12" y1="19" x2="12" y2="5"></line>
        <polyline points={ARROW_HEAD.up}></polyline>
      {/if}
    </svg>
  </button>

  {#if isOpen}
    <div
      class="dropdown-menu"
      bind:this={menuEl}
      use:anchoredPanel={{ anchor: buttonEl, width: 'content', edge: align, gap: 8 }}
      transition:fade={{ duration: 120 }}
    >
      {#each sortOptions as option (option.value)}
        {@const iconName = SORT_ICON_BY_VALUE[option.value]}
        <button
          type="button"
          class="dropdown-item"
          class:active={option.value === sortBy}
          on:click={() => selectSort(option)}
        >
          {#if iconName}
            <MenuIcon name={iconName} />
          {/if}
          <span class="option-label">{$t(option.label)}</span>
          {#if option.value === sortBy}
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              class="option-arrow"
              aria-hidden="true"
            >
              {#if isDescending(option, sortOrder)}
                <line x1="12" y1="5" x2="12" y2="19"></line>
                <polyline points={ARROW_HEAD.down}></polyline>
              {:else}
                <line x1="12" y1="19" x2="12" y2="5"></line>
                <polyline points={ARROW_HEAD.up}></polyline>
              {/if}
            </svg>
          {/if}
        </button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .sort-dropdown {
    position: relative;
    display: inline-block;
  }

  .sort-button {
    cursor: pointer;
    transition: all var(--duration-base) ease;
    background: var(--surface-color);
    border-color: var(--border-color);
    color: var(--text-secondary);
    box-shadow: var(--shadow-md);
    backdrop-filter: blur(8px);
    user-select: none;
  }

  .sort-button:hover {
    border-color: var(--primary-color);
    color: var(--text-primary);
  }

  .sort-button:focus {
    outline: 2px solid var(--focus-ring-color);
    outline-offset: 2px;
  }

  .sort-button svg {
    flex-shrink: 0;
  }

  /* Placed by anchoredPanel: fixed on <body>, flips above, scrolls when short on room. */
  .dropdown-menu {
    overflow-y: auto;
    min-width: 200px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-2xl);
    box-shadow: var(--shadow-xl);
    padding: var(--space-2);
    backdrop-filter: blur(12px);
  }

  .dropdown-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    width: 100%;
    padding: var(--space-2-5) var(--space-3);
    background: transparent;
    border: none;
    border-radius: var(--radius-lg);
    font-size: var(--font-size-md);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--duration-fast) ease;
    text-align: start;
  }

  .dropdown-item:hover {
    background: var(--button-hover);
    color: var(--text-color);
  }

  .dropdown-item.active {
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-semibold);
  }

  .option-label {
    flex: 1;
  }

  .option-arrow {
    flex-shrink: 0;
    margin-inline-start: var(--space-2);
  }

  /* Hide sort label on tablet and below to save space */
  @media (max-width: 1200px) {
    .sort-label {
      display: none;
    }
  }

  @media (max-width: 768px) {
    .sort-button {
      font-size: var(--font-size-sm);
      padding: 0.4rem var(--space-3);
    }

    .dropdown-menu {
      min-width: 180px;
    }
  }
</style>
