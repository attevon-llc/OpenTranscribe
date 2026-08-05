<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { clickOutside } from '$lib/actions/clickOutside';
  import { t } from '$stores/locale';

  /** Open state. Bindable: `<Dropdown bind:open>`. */
  export let open = false;
  /** Menu alignment relative to the trigger. */
  export let align: 'left' | 'right' = 'left';
  /** Accessible label for the trigger button. Defaults to the translated "Menu". */
  export let ariaLabel: string | undefined = undefined;

  $: resolvedAriaLabel = ariaLabel ?? $t('common.menu');
  /** Extra classes for the trigger button (lets callers reuse existing button styles). */
  export let buttonClass = '';

  const dispatch = createEventDispatcher<{ toggle: boolean }>();

  function setOpen(value: boolean) {
    if (value === open) return;
    open = value;
    dispatch('toggle', open);
  }
  const toggle = () => setOpen(!open);
  const close = () => setOpen(false);

  function onKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && open) {
      close();
    }
  }
</script>

<svelte:window on:keydown={onKeydown} />

<div class="dropdown" use:clickOutside={{ enabled: open }} on:click_outside={close}>
  <button
    type="button"
    class={`dropdown-trigger ${buttonClass}`.trim()}
    aria-haspopup="menu"
    aria-expanded={open}
    aria-label={resolvedAriaLabel}
    on:click={toggle}
  >
    <slot name="trigger" {open} />
  </button>

  {#if open}
    <div class="dropdown-menu" class:right={align === 'right'} role="menu">
      <slot {close} />
    </div>
  {/if}
</div>

<style>
  .dropdown {
    position: relative;
    display: inline-flex;
  }
  .dropdown-trigger {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: transparent;
    border: none;
    cursor: pointer;
    color: inherit;
    font: inherit;
    padding: 0;
  }
  .dropdown-menu {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    z-index: 1000;
    min-width: 180px;
    padding: 6px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .dropdown-menu.right {
    left: auto;
    right: 0;
  }
</style>
