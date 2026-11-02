<script context="module" lang="ts">
  /** The manager's three views of the library. */
  export type TagFilterId = 'all' | 'unused' | 'colliding';
</script>

<script lang="ts">
  /**
   * View tabs (all / unused / colliding) and the ownership picker.
   *
   * The picker's values are the strings `GET /tags?scope=` accepts. The
   * collisions endpoint takes no scope, so the picker is disabled in that view
   * rather than appearing to filter something it cannot.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Tabs, { type TabItem } from '$components/ui/Tabs.svelte';
  import type { TagScope } from '$lib/types/tag';

  export let filter: TagFilterId = 'all';
  /** Rows currently shown; null hides the badge (loading or error). */
  export let count: number | null = null;
  export let scope: TagScope = 'all';

  const dispatch = createEventDispatcher<{ change: TagFilterId; scopeChange: TagScope }>();

  const FILTERS: { id: TagFilterId; key: string }[] = [
    { id: 'all', key: 'tags.manager.filter.all' },
    { id: 'unused', key: 'tags.manager.filter.unused' },
    { id: 'colliding', key: 'tags.manager.filter.collisions' },
  ];

  const SCOPES: { value: TagScope; key: string }[] = [
    { value: 'all', key: 'tags.manager.scope.all' },
    { value: 'mine', key: 'tags.manager.scope.mine' },
    { value: 'system', key: 'tags.manager.scope.system' },
    { value: 'shared_with_me', key: 'tags.manager.scope.shared_with_me' },
  ];

  const selectId = 'tag-scope-select';

  $: tabs = FILTERS.map(
    ({ id, key }): TabItem => ({
      id,
      label: $t(key),
      badge: id === filter ? count : null,
    })
  );

  function handleTabChange(event: CustomEvent<string>) {
    const next = FILTERS.find(({ id }) => id === event.detail);
    if (next) dispatch('change', next.id);
  }

  function handleScopeChange(event: Event) {
    const value = (event.currentTarget as HTMLSelectElement).value;
    const next = SCOPES.find((candidate) => candidate.value === value);
    if (next) dispatch('scopeChange', next.value);
  }
</script>

<div class="tag-filter-bar">
  <div class="filter-tabs">
    <Tabs
      {tabs}
      activeId={filter}
      ariaLabel={$t('tags.manager.filterLabel')}
      on:change={handleTabChange}
    />
  </div>
  <div class="scope-picker">
    <label for={selectId}>{$t('tags.manager.scopeLabel')}</label>
    <select
      id={selectId}
      class="scope-select"
      value={scope}
      disabled={filter === 'colliding'}
      on:change={handleScopeChange}
    >
      {#each SCOPES as option (option.value)}
        <option value={option.value}>{$t(option.key)}</option>
      {/each}
    </select>
  </div>
</div>

<style>
  .tag-filter-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .filter-tabs {
    flex: 1 1 auto;
    min-width: 0;
    overflow-x: auto;
  }

  .filter-tabs :global(.tab-label) {
    white-space: nowrap;
  }

  .scope-picker {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: var(--space-2);
  }

  .scope-picker label {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    white-space: nowrap;
  }

  /* The global field rule already gives it the same height as the inputs above. */
  .scope-select {
    width: auto;
  }

  @media (max-width: 640px) {
    .tag-filter-bar {
      flex-direction: column;
      align-items: stretch;
    }

    .scope-picker {
      justify-content: flex-end;
    }
  }
</style>
