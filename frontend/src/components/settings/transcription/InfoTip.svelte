<script lang="ts" context="module">
  let nextId = 0;
</script>

<script lang="ts">
  import { onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import { anchoredPanel } from '$lib/actions/anchoredPanel';

  // `inline` is the small icon that sits inside a field label; the default is the card-title icon.
  export let inline = false;
  // Accessible name of the trigger. Pass the topic ("How it works") when the icon stands alone.
  export let label = '';

  const panelId = `infotip-${nextId++}`;
  const CLOSE_DELAY_MS = 150;
  const PANEL_GAP = 8;

  let root: HTMLSpanElement;
  let trigger: HTMLButtonElement;
  let panel: HTMLSpanElement | undefined;
  let hovering = false;
  let pinned = false;
  let pointerFocus = false;
  let closeTimer: ReturnType<typeof setTimeout> | undefined;
  let listening = false;

  $: open = hovering || pinned;
  $: listen(open);

  function cancelClose() {
    clearTimeout(closeTimer);
  }

  function hoverIn() {
    cancelClose();
    hovering = true;
  }

  function hoverOut() {
    cancelClose();
    closeTimer = setTimeout(() => (hovering = false), CLOSE_DELAY_MS);
  }

  function close() {
    cancelClose();
    hovering = false;
    pinned = false;
  }

  function toggle() {
    pointerFocus = false;
    if (open) close();
    else pinned = true;
  }

  function onFocus() {
    // A mouse press focuses the button before click toggles it; opening here too would make the
    // click close what the focus just opened.
    if (!pointerFocus) pinned = true;
  }

  function onFocusOut(e: FocusEvent) {
    const next = e.relatedTarget as Node | null;
    if (!next || (!root.contains(next) && !panel?.contains(next))) pinned = false;
  }

  function onKeyCapture(e: KeyboardEvent) {
    if (e.key !== 'Escape') return;
    // Dismiss the tip only: the surrounding dialog also closes on Escape.
    e.stopPropagation();
    close();
  }

  function onPointerDownOutside(e: Event) {
    const target = e.target as Node;
    // The panel lives on <body> (see anchoredPanel), so it is not inside `root`.
    if (!root.contains(target) && !panel?.contains(target)) close();
  }

  function listen(on: boolean) {
    if (typeof window === 'undefined' || on === listening) return;
    listening = on;
    if (on) {
      window.addEventListener('keydown', onKeyCapture, true);
      document.addEventListener('pointerdown', onPointerDownOutside, true);
    } else {
      window.removeEventListener('keydown', onKeyCapture, true);
      document.removeEventListener('pointerdown', onPointerDownOutside, true);
    }
  }

  onDestroy(() => {
    cancelClose();
    listen(false);
  });
</script>

<span
  class="tip"
  class:inline
  bind:this={root}
  on:mouseenter={hoverIn}
  on:mouseleave={hoverOut}
  on:focusout={onFocusOut}
  role="presentation"
>
  <button
    type="button"
    class="trigger"
    bind:this={trigger}
    aria-label={label || $t('common.moreInformation')}
    aria-expanded={open}
    aria-describedby={open ? panelId : undefined}
    on:pointerdown={() => (pointerFocus = true)}
    on:focus={onFocus}
    on:click={toggle}
  >
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={inline ? 12 : 14}
      height={inline ? 12 : 14}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10"></circle>
      <line x1="12" y1="16" x2="12" y2="12"></line>
      <line x1="12" y1="8" x2="12.01" y2="8"></line>
    </svg>
  </button>
  {#if open}
    <span
      id={panelId}
      class="panel"
      class:inline
      role="tooltip"
      bind:this={panel}
      use:anchoredPanel={{ anchor: trigger, width: 'content', gap: PANEL_GAP }}
      on:mouseenter={hoverIn}
      on:mouseleave={hoverOut}
    >
      <slot />
    </span>
  {/if}
</span>

<style>
  .tip {
    position: relative;
    display: inline-flex;
    align-items: center;
    color: var(--text-muted);
  }

  .tip.inline {
    margin-inline-start: 0.25rem;
    vertical-align: middle;
  }

  .trigger {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    margin: 0;
    border: none;
    border-radius: 50%;
    background: none;
    color: inherit;
    cursor: help;
  }

  .tip:hover,
  .tip:focus-within {
    color: var(--text-color);
  }

  .tip.inline:hover,
  .tip.inline:focus-within {
    color: var(--primary-on-surface);
  }

  .trigger:focus-visible {
    outline: 2px solid var(--primary-on-surface);
    outline-offset: 2px;
  }

  /* Position, z-index and max-height come from the anchoredPanel action: the panel lives on <body>. */
  .panel {
    width: min(280px, calc(100vw - 2rem));
    box-sizing: border-box;
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    background-color: var(--surface-color, #333);
    color: var(--text-color);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
    font-size: 0.75rem;
    font-weight: 400;
    line-height: 1.4;
    text-align: start;
    white-space: normal;
    overflow-y: auto;
  }

  .panel.inline {
    width: min(320px, calc(100vw - 2rem));
    padding: 0.75rem 1rem;
    border-radius: 8px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
    line-height: 1.5;
  }

  /* The gap between icon and panel must not end the hover (WCAG 1.4.13 hoverable). */
  .panel::before {
    content: '';
    position: absolute;
    inset-inline: 0;
    top: -10px;
    height: 10px;
  }

  .panel[data-side='above']::before {
    top: auto;
    bottom: -10px;
  }

  .panel :global(strong) {
    color: var(--primary-on-surface);
  }

  .panel :global(ul) {
    margin: 0.375rem 0 0;
    padding-inline-start: 1.125rem;
  }

  .panel :global(li + li) {
    margin-top: 0.25rem;
  }
</style>
