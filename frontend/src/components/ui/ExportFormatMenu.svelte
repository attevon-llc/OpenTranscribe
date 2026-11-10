<script context="module" lang="ts">
  import type { MenuIconName } from '$lib/ui/menuIcons';

  export interface ExportFormatOption {
    value: string;
    /** Already translated (or a technical name such as "CSV"). */
    label: string;
    icon?: MenuIconName;
  }
</script>

<script lang="ts">
  /**
   * One Export button that opens a short menu of formats, same interaction as the transcript
   * Export menu: pick a format and the choice is dispatched at once. The panel is placed by
   * `anchoredPanel` and dismissed by `clickOutside` / Escape. When `disabled`, the button stays
   * visible with `disabledReason` as its tooltip so the reason is never a mystery.
   */
  import { createEventDispatcher } from 'svelte';
  import { clickOutside } from '$lib/actions/clickOutside';
  import { anchoredPanel } from '$lib/actions/anchoredPanel';
  import MenuIcon from '$components/ui/MenuIcon.svelte';

  export let formats: ExportFormatOption[];
  /** The (translated) button text. */
  export let label: string;
  export let disabled = false;
  export let disabledReason = '';
  export let busy = false;

  const dispatch = createEventDispatcher<{ select: { format: string } }>();

  let open = false;
  let button: HTMLButtonElement;
  let menu: HTMLDivElement | undefined;

  $: if (disabled) open = false;

  function choose(format: string) {
    open = false;
    dispatch('select', { format });
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && open) {
      event.stopPropagation();
      open = false;
      button?.focus();
    }
  }
</script>

<!-- The panel is portaled to <body>, so Escape is heard on the window. -->
<svelte:window on:keydown={onKeydown} />

<div
  class="export-menu"
  use:clickOutside={{ enabled: open, ignore: [menu ?? null] }}
  on:click_outside={() => (open = false)}
  role="presentation"
>
  <button
    bind:this={button}
    type="button"
    class="btn btn-secondary export-trigger"
    {disabled}
    title={disabled ? disabledReason : undefined}
    aria-haspopup="menu"
    aria-expanded={open}
    on:click={() => (open = !open)}
  >
    <MenuIcon name="export-file" size={16} />
    {label}
    <MenuIcon name="chevron-down" size={14} />
  </button>
  {#if open && !disabled}
    <div
      class="export-panel"
      role="menu"
      bind:this={menu}
      use:anchoredPanel={{ anchor: button, width: 'content', gap: 8 }}
    >
      {#each formats as format (format.value)}
        <button type="button" role="menuitem" class="export-item" disabled={busy} on:click={() => choose(format.value)}>
          {#if format.icon}<MenuIcon name={format.icon} size={16} />{/if}
          <span dir="ltr">{format.label}</span>
        </button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .export-menu {
    position: relative;
    display: inline-block;
  }

  .export-trigger {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }

  /* Placed by anchoredPanel: fixed on <body>, flips above, scrolls when short on room. */
  .export-panel {
    overflow-y: auto;
    min-width: 10rem;
    padding: var(--space-1);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-md);
  }

  .export-item {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    padding: var(--space-2) var(--space-3);
    background: none;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--text-color);
    font-size: var(--font-size-md);
    text-align: start;
    cursor: pointer;
  }

  .export-item:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .export-item:focus-visible,
  .export-item:hover:not(:disabled) {
    background: var(--hover-color);
  }
</style>
