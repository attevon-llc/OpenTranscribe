<script context="module" lang="ts">
  /** The four server-side views of the tag library. */
  export type TagFilterId = 'all' | 'awaiting_review' | 'unused' | 'colliding';
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Tabs from '$components/ui/Tabs.svelte';
  import type { TabItem } from '$components/ui/Tabs.svelte';

  export let filter: TagFilterId = 'all';
  /** Row count of the active view, shown as the active tab's badge. */
  export let count: number | null = null;

  const dispatch = createEventDispatcher<{ change: TagFilterId }>();

  $: tabs = [
    { id: 'all', label: $t('tags.manager.filter.all') },
    { id: 'awaiting_review', label: $t('tags.manager.filter.awaitingReview') },
    { id: 'unused', label: $t('tags.manager.filter.unused') },
    { id: 'colliding', label: $t('tags.manager.filter.collisions') },
  ].map((tab) => ({
    ...tab,
    badge: tab.id === filter && count !== null ? count : null,
  })) satisfies TabItem[];

  function onChange(event: CustomEvent<string>) {
    dispatch('change', event.detail as TagFilterId);
  }
</script>

<div class="tag-filter-bar">
  <Tabs {tabs} activeId={filter} ariaLabel={$t('tags.manager.filterLabel')} on:change={onChange} />
</div>

<style>
  /* The shared Tabs primitive wraps its labels on narrow screens, which turns
     "Collisions" into two lines. Scroll the strip instead — the convention the
     speakers page already uses for its tab row. */
  .tag-filter-bar {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }

  .tag-filter-bar::-webkit-scrollbar {
    display: none;
  }

  .tag-filter-bar :global(.tab) {
    white-space: nowrap;
    flex-shrink: 0;
  }
</style>
