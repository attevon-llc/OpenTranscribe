<script context="module" lang="ts">
  let uid = 0;
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';

  export let title: string;
  /** Bindable open state: `<ExpandableSection bind:expanded>`. */
  export let expanded = false;
  /**
   * Wrap the toggle in an `<hN>` so a section title stays in the heading outline (screen
   * readers navigate by heading). `null` keeps the plain button.
   */
  export let headingLevel: 2 | 3 | 4 | 5 | 6 | null = null;

  const regionId = `expandable-region-${++uid}`;
  const dispatch = createEventDispatcher<{ toggle: boolean }>();

  function toggle() {
    expanded = !expanded;
    dispatch('toggle', expanded);
  }
</script>

<div class="expandable">
  <svelte:element this={headingLevel ? `h${headingLevel}` : 'div'} class="expandable-heading">
    <button
      type="button"
      class="expandable-header"
      aria-expanded={expanded}
      aria-controls={regionId}
      on:click={toggle}
    >
      <svg
        class="chevron"
        class:open={expanded}
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <polyline points="9 18 15 12 9 6" />
      </svg>
      <span class="expandable-title">{title}</span>
      <slot name="header-extra" />
    </button>
  </svelte:element>

  {#if expanded}
    <div class="expandable-content" id={regionId} transition:slide|local={{ duration: 150 }}>
      <slot />
    </div>
  {/if}
</div>

<style>
  .expandable {
    border-bottom: 1px solid var(--border-color);
  }
  .expandable-heading {
    margin: 0;
    font: inherit;
  }
  .expandable-header {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 12px 4px;
    background: transparent;
    border: none;
    border-radius: 0;
    box-shadow: none;
    cursor: pointer;
    color: var(--text-color);
    font-size: 14px;
    font-weight: 600;
    text-align: start;
  }
  /* Flat disclosure row: cancel form-elements.css's global button hover (fill + scale). */
  .expandable-header:hover {
    color: var(--primary-on-surface);
    background: transparent;
    box-shadow: none;
    transform: none;
  }
  .expandable-header:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 4px;
  }
  .chevron {
    flex-shrink: 0;
    transition: transform 0.15s ease;
  }
  .chevron.open {
    transform: rotate(90deg);
  }
  /* Collapsed, the chevron points along the reading direction: toward the end edge. */
  :global([dir='rtl']) .chevron:not(.open) {
    transform: scaleX(-1);
  }
  .expandable-title {
    flex: 1;
  }
  .expandable-content {
    padding: 0 4px 12px;
  }
</style>
