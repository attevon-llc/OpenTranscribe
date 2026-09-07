<script context="module" lang="ts">
  export interface TabItem {
    id: string;
    label: string;
    /** Optional count/badge shown after the label. */
    badge?: string | number | null;
    disabled?: boolean;
  }
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let tabs: TabItem[] = [];
  /** The active tab id. Bindable: `<Tabs bind:activeId>`. */
  export let activeId: string = tabs[0]?.id ?? '';
  /** Accessible label for the tablist (screen readers). Defaults to the translated "Tabs". */
  export let ariaLabel: string | undefined = undefined;

  $: resolvedAriaLabel = ariaLabel ?? $t('common.tabs');

  const dispatch = createEventDispatcher<{ change: string }>();

  let tabEls: HTMLButtonElement[] = [];

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
    switch (event.key) {
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

<div class="tabs" role="tablist" aria-label={resolvedAriaLabel}>
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
      on:click={() => select(tab.id)}
      on:keydown={(e) => onKeydown(e, i)}
    >
      <span class="tab-label">{tab.label}</span>
      {#if tab.badge !== undefined && tab.badge !== null && tab.badge !== ''}
        <span class="tab-badge">{tab.badge}</span>
      {/if}
    </button>
  {/each}
</div>

<style>
  .tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border-color);
  }
  .tab {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 10px 16px;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    /* Reset the aggressive global `button {}` base style (rounded corners +
       drop shadow) so tabs stay square with a flat bottom bar. */
    border-radius: 0;
    box-shadow: none;
    margin-bottom: -1px;
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
