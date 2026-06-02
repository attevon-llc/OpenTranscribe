<script context="module" lang="ts">
  export type ChipVariant = 'default' | 'success' | 'warning' | 'error' | 'info';
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  /** Visual variant — maps to the app's semantic status colors. */
  export let variant: ChipVariant = 'default';
  /** When true, renders a × button that dispatches `remove`. */
  export let removable = false;
  /** Accessible label for the remove button. Caller should pass translated text. */
  export let removeLabel = 'Remove';

  const dispatch = createEventDispatcher<{ remove: void }>();

  function onRemove() {
    dispatch('remove');
  }
</script>

<span class={`chip chip-${variant}`} class:removable>
  <span class="chip-content"><slot /></span>
  {#if removable}
    <button type="button" class="chip-remove" aria-label={removeLabel} on:click={onRemove}>
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2.5"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    </button>
  {/if}
</span>

<style>
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    border-radius: 14px;
    font-size: 12px;
    font-weight: 600;
    line-height: 1.4;
    white-space: nowrap;
    border: 1px solid transparent;
  }
  .chip.removable {
    padding-right: 4px;
  }
  .chip-content {
    display: inline-flex;
    align-items: center;
  }
  .chip-default {
    background: var(--surface-color);
    border-color: var(--border-color);
    color: var(--text-secondary);
  }
  .chip-success {
    background: rgba(var(--success-color-rgb, 34, 197, 94), 0.12);
    color: var(--success-color, #16a34a);
  }
  .chip-warning {
    background: rgba(var(--warning-color-rgb, 245, 158, 11), 0.14);
    color: var(--warning-color, #d97706);
  }
  .chip-error {
    background: rgba(var(--error-color-rgb, 239, 68, 68), 0.12);
    color: var(--error-color, #dc2626);
  }
  .chip-info {
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-color);
  }
  .chip-remove {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    padding: 0;
    border: none;
    border-radius: 50%;
    background: transparent;
    color: inherit;
    cursor: pointer;
    opacity: 0.7;
    transition:
      opacity 0.15s ease,
      background 0.15s ease;
  }
  .chip-remove:hover {
    opacity: 1;
    background: rgba(0, 0, 0, 0.1);
  }
  .chip-remove:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
    opacity: 1;
  }
</style>
