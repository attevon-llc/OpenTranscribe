<script context="module" lang="ts">
  /** The four server-side views of the tag library. */
  export type TagFilterId = 'all' | 'unused' | 'colliding';
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Tabs from '$components/ui/Tabs.svelte';
  import type { TabItem } from '$components/ui/Tabs.svelte';
  import type { TagScope } from '$lib/types/tag';

  export let filter: TagFilterId = 'all';
  /** Row count of the active view, shown as the active tab's badge. */
  export let count: number | null = null;
  /** Ownership scope. A separate axis from the view, so "my unused tags" works. */
  export let scope: TagScope = 'all';

  const dispatch = createEventDispatcher<{ change: TagFilterId; scopeChange: TagScope }>();

  $: tabs = [
    { id: 'all', label: $t('tags.manager.filter.all') },
    { id: 'unused', label: $t('tags.manager.filter.unused') },
    { id: 'colliding', label: $t('tags.manager.filter.collisions') },
  ].map((tab) => ({
    ...tab,
    badge: tab.id === filter && count !== null ? count : null,
  })) satisfies TabItem[];

  function onChange(event: CustomEvent<string>) {
    dispatch('change', event.detail as TagFilterId);
  }

  function onScopeChange(event: Event) {
    dispatch('scopeChange', (event.currentTarget as HTMLSelectElement).value as TagScope);
  }
</script>

<div class="tag-filter-row">
  <div class="tag-filter-bar">
    <Tabs {tabs} activeId={filter} ariaLabel={$t('tags.manager.filterLabel')} on:change={onChange} />
  </div>

  <label class="scope-picker">
    <span class="scope-label">{$t('tags.manager.scopeLabel')}</span>
    <select class="form-select scope-select" value={scope} on:change={onScopeChange}>
      <option value="all">{$t('tags.manager.scope.all')}</option>
      <option value="mine">{$t('tags.manager.scope.mine')}</option>
      <option value="system">{$t('tags.manager.scope.system')}</option>
      <option value="shared_with_me">{$t('tags.manager.scope.shared_with_me')}</option>
    </select>
  </label>
</div>

<style>
  .tag-filter-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  /* The shared Tabs primitive wraps its labels on narrow screens, which turns
     "Collisions" into two lines. Scroll the strip instead — the convention the
     speakers page already uses for its tab row. */
  .tag-filter-bar {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    min-width: 0;
  }

  .tag-filter-bar::-webkit-scrollbar {
    display: none;
  }

  .tag-filter-bar :global(.tab) {
    white-space: nowrap;
    flex-shrink: 0;
  }

  .scope-picker {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .scope-label {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .scope-select {
    /* Sized to its content rather than the form default, which is full-width. */
    width: auto;
    min-width: 7rem;
    padding: 0.35rem 0.5rem;
    font-size: 0.8125rem;
  }

  /* Below the tab strip's own breakpoint the row stacks, or the select squeezes
     the tabs into a scroll on every screen. */
  @media (max-width: 640px) {
    .tag-filter-row {
      flex-direction: column;
      align-items: stretch;
      gap: 0.5rem;
    }

    .scope-picker {
      justify-content: flex-end;
    }
  }
</style>
