<script context="module" lang="ts">
  export interface TabItem {
    id: string;
    label: string;
    /** Optional count/badge shown after the label. */
    badge?: string | number | null;
    disabled?: boolean;
    /** Native tooltip; used to say why a tab is disabled. */
    title?: string;
    /**
     * Marks a tab whose panel holds something that needs doing, without opening it. The
     * text is the accessible name and tooltip of the dot, so it must say WHAT needs doing;
     * a bare dot tells a screen-reader user nothing.
     */
    attention?: string;
  }
</script>

<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';

  export let tabs: TabItem[] = [];
  /** The active tab id. Bindable: `<Tabs bind:activeId>`. */
  export let activeId: string = tabs[0]?.id ?? '';
  /** Accessible label for the tablist (screen readers). Defaults to the translated "Tabs". */
  export let ariaLabel: string | undefined = undefined;

  /**
   * Pin the strip to the top of the nearest scrolling ancestor so a tall panel never scrolls the
   * user's place away. Opt-in: only settings sections whose panels outgrow the viewport want it.
   */
  export let sticky = false;

  /**
   * Wrap onto extra rows instead of scrolling sideways. For a strip with too many tabs to fit one
   * row at desktop width (Authentication has twelve); phones always scroll one row so a pinned
   * strip never eats the viewport.
   */
  export let wrap = false;

  $: resolvedAriaLabel = ariaLabel ?? $t('common.tabs');

  const dispatch = createEventDispatcher<{ change: string }>();

  let tabEls: HTMLButtonElement[] = [];
  let sentinel: HTMLDivElement | undefined;
  let stuck = false;

  // The sentinel sits just above the strip; once it scrolls out of its scroll container the strip
  // is pinned, which is when the divider shadow is worth showing.
  onMount(() => {
    if (!sticky || !sentinel || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(([entry]) => {
      stuck = !entry.isIntersecting;
    });
    observer.observe(sentinel);
    return () => observer.disconnect();
  });

  function select(id: string) {
    if (id === activeId) return;
    activeId = id;
    dispatch('change', id);
  }

  // WAI-ARIA roving keyboard navigation across the tablist.
  function onKeydown(event: KeyboardEvent, index: number) {
    const enabled = tabs.map((t, i) => ({ t, i })).filter(({ t }) => !t.disabled);
    if (enabled.length === 0) return;
    const pos = enabled.findIndex(({ i }) => i === index);
    let nextPos = pos;
    // Left/Right follow the visual order, which a right-to-left layout reverses.
    const rtl = (event.currentTarget as HTMLElement).closest('[dir]')?.getAttribute('dir') === 'rtl';
    const key =
      rtl && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')
        ? event.key === 'ArrowLeft'
          ? 'ArrowRight'
          : 'ArrowLeft'
        : event.key;
    switch (key) {
      case 'ArrowRight':
      case 'ArrowDown':
        nextPos = (pos + 1) % enabled.length;
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        nextPos = (pos - 1 + enabled.length) % enabled.length;
        break;
      case 'Home':
        nextPos = 0;
        break;
      case 'End':
        nextPos = enabled.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    const target = enabled[nextPos];
    select(target.t.id);
    tabEls[target.i]?.focus();
  }
</script>

{#if sticky}<div class="tabs-sentinel" bind:this={sentinel} aria-hidden="true"></div>{/if}
<div
  class="tabs"
  class:sticky
  class:stuck
  class:wrap
  role="tablist"
  aria-label={resolvedAriaLabel}
>
  {#each tabs as tab, i (tab.id)}
    <button
      bind:this={tabEls[i]}
      type="button"
      role="tab"
      class="tab"
      class:active={tab.id === activeId}
      id={`tab-${tab.id}`}
      aria-selected={tab.id === activeId}
      aria-controls={`tabpanel-${tab.id}`}
      tabindex={tab.id === activeId ? 0 : -1}
      disabled={tab.disabled}
      title={tab.title ?? tab.attention}
      on:click={() => select(tab.id)}
      on:keydown={(e) => onKeydown(e, i)}
    >
      <span class="tab-label">{tab.label}</span>
      {#if tab.badge !== undefined && tab.badge !== null && tab.badge !== ''}
        <span class="tab-badge">{tab.badge}</span>
      {/if}
      {#if tab.attention}
        <span class="tab-attention" role="img" aria-label={tab.attention}></span>
      {/if}
    </button>
  {/each}
</div>

<style>
  /* The hairline is an inset shadow, not a border: the strip scrolls sideways when its tabs do not
     fit, and an overflow clip at the padding box would shave the active tab's 2px underline that
     overlaps a bottom border. */
  .tabs {
    display: flex;
    gap: 4px;
    box-shadow: inset 0 -1px 0 var(--border-color);
    overflow-x: auto;
    overflow-y: hidden;
    scrollbar-width: thin;
  }
  .tabs.wrap {
    flex-wrap: wrap;
    overflow: visible;
  }
  @media (max-width: 640px) {
    .tabs.wrap {
      flex-wrap: nowrap;
      overflow-x: auto;
      overflow-y: hidden;
    }
  }
  /* Sticky offsets are measured from the scroll container's content box, so its padding (published
     by the host as --tabs-sticky-inset) would stay visible above a pinned strip as a band of
     scrolled content. Pin the strip's top edge flush with the scrollport instead (top: -inset);
     the small top padding, offset by an equal negative margin, keeps the resting layout unchanged. */
  .tabs.sticky {
    position: sticky;
    top: calc(-1 * var(--tabs-sticky-inset, 0px));
    z-index: var(--z-sticky);
    margin: -0.5rem 0 1.25rem;
    padding-top: 0.5rem;
    /* Opaque so the panel scrolls beneath it; the modal body paints with the surface token. */
    background: var(--surface-color);
  }
  .tabs.sticky.stuck {
    box-shadow:
      inset 0 -1px 0 var(--border-color),
      0 6px 8px -6px color-mix(in srgb, var(--text-color) 40%, transparent);
  }
  /* Zero-height marker just above the strip: once it leaves its scroll container the strip is pinned. */
  .tabs-sentinel {
    height: 0;
    visibility: hidden;
    pointer-events: none;
  }
  .tab {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 10px 16px;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    flex: 0 0 auto;
    white-space: nowrap;
    /* Reset the aggressive global `button {}` base style (rounded corners +
       drop shadow) so tabs stay square with a flat bottom bar. */
    border-radius: 0;
    box-shadow: none;
    color: var(--text-secondary);
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition:
      color 0.15s ease,
      border-color 0.15s ease,
      background 0.15s ease;
  }
  /* Neutralize the global `button:hover/:focus { transform: scale; box-shadow }`
     so tabs never lift, round, or shadow on hover/focus. */
  .tab:hover:not(:disabled),
  .tab:focus:not(:disabled) {
    color: var(--text-color);
    background: var(--button-hover);
    transform: none;
    box-shadow: none;
  }
  .tab.active {
    color: var(--primary-on-surface);
    border-bottom-color: var(--primary-color);
    background: transparent;
    transform: none;
    box-shadow: none;
  }
  .tab.active:hover:not(:disabled) {
    background: var(--button-hover);
  }
  .tab:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
    border-radius: 0;
  }
  .tab:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .tab-attention {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--warning-color);
    /* A ring in the surface colour keeps the dot legible on the active tab's tint in
       both themes. */
    box-shadow: 0 0 0 2px var(--surface-color);
  }
  .tab-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 6px;
    border-radius: 9px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    font-size: 11px;
    font-weight: 600;
    line-height: 1;
  }
  .tab.active .tab-badge {
    background: rgba(var(--primary-color-rgb), 0.12);
    border-color: transparent;
    color: var(--primary-on-surface);
  }
</style>
