<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import { GroupsApi } from '$lib/api/groups';
  import { createDebouncedHandler } from '$lib/utils/debounce';
  import Spinner from '../ui/Spinner.svelte';
  import type { ShareTargetSearchResult } from '$lib/types/groups';

  export let existingShareTargets: Array<{ type: string; uuid: string }> = [];

  const dispatch = createEventDispatcher();

  let searchQuery = '';
  let results: ShareTargetSearchResult[] = [];
  let loading = false;
  const debouncedSearch = createDebouncedHandler(() => doSearch(searchQuery), 300);

  // Cache groups to avoid fetching on every keystroke
  let cachedGroups: ShareTargetSearchResult[] | null = null;

  onDestroy(() => {
    debouncedSearch.cleanup();
  });

  async function loadGroupsOnce(): Promise<ShareTargetSearchResult[]> {
    if (cachedGroups !== null) return cachedGroups;
    try {
      const groups = await GroupsApi.fetchGroups();
      cachedGroups = groups.map(g => ({
        type: 'group' as const,
        uuid: g.uuid,
        name: g.name,
        member_count: g.member_count,
      }));
    } catch {
      cachedGroups = [];
    }
    return cachedGroups;
  }

  async function doSearch(query: string) {
    if (query.trim().length < 2) {
      results = [];
      return;
    }

    loading = true;
    try {
      // Search users and load cached groups in parallel
      const [users, allGroups] = await Promise.all([
        GroupsApi.searchUsers(query.trim()),
        loadGroupsOnce(),
      ]);

      const userResults: ShareTargetSearchResult[] = users.map(u => ({
        type: 'user' as const,
        uuid: u.uuid,
        name: u.full_name || u.masked_email,
        detail: u.masked_email,
      }));

      // Filter cached groups by query
      const groupResults = allGroups.filter(
        g => g.name.toLowerCase().includes(query.toLowerCase())
      );

      // Merge and filter out existing targets
      const allResults = [...userResults, ...groupResults];
      results = allResults.filter(
        r => !existingShareTargets.some(e => e.type === r.type && e.uuid === r.uuid)
      );
    } catch (err) {
      console.error('Error searching share targets:', err);
      results = [];
    } finally {
      loading = false;
    }
  }

  function handleInput() {
    debouncedSearch.trigger();
  }

  function selectTarget(target: ShareTargetSearchResult) {
    dispatch('select', target);
    searchQuery = '';
    results = [];
  }
</script>

<div class="share-target-search">
  <div class="search-input-wrapper">
    <svg class="search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="11" cy="11" r="8"/>
      <line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <input
      type="text"
      bind:value={searchQuery}
      on:input={handleInput}
      placeholder={$t('sharing.searchUsersGroups')}
      class="search-input"
    />
  </div>

  {#if searchQuery.trim().length > 0 && searchQuery.trim().length < 2}
    <div class="search-hint">{$t('sharing.searchMinChars')}</div>
  {:else if loading}
    <div class="search-loading">
      <Spinner size="small" />
      {$t('sharing.searching')}
    </div>
  {:else if results.length > 0}
    <ul class="search-results">
      {#each results as result (result.type + '-' + result.uuid)}
        <li>
          <button class="result-item" on:click={() => selectTarget(result)}>
            {#if result.type === 'user'}
              <svg class="type-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                <circle cx="12" cy="7" r="4"/>
              </svg>
              <div class="result-info">
                <span class="result-name">{result.name}</span>
                {#if result.detail && result.detail !== result.name}
                  <span class="result-detail">{result.detail}</span>
                {/if}
              </div>
            {:else}
              <svg class="type-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
                <circle cx="9" cy="7" r="4"/>
                <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
                <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
              </svg>
              <div class="result-info">
                <span class="result-name">{result.name}</span>
                {#if result.member_count != null}
                  <span class="result-detail">
                    {$t('sharing.memberCount', { count: result.member_count })}
                  </span>
                {/if}
              </div>
            {/if}
          </button>
        </li>
      {/each}
    </ul>
  {:else if searchQuery.trim().length >= 2}
    <div class="no-results">{$t('sharing.noResults')}</div>
  {/if}
</div>

<style>
  .share-target-search {
    position: relative;
  }

  .search-input-wrapper {
    position: relative;
    display: flex;
    align-items: center;
  }

  .search-icon {
    position: absolute;
    left: 10px;
    color: var(--text-secondary);
    pointer-events: none;
  }

  .search-input {
    width: 100%;
    padding: 0.6rem 0.75rem 0.6rem 2rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--input-bg, var(--background-color));
    color: var(--text-color);
    font-size: 0.875rem;
    transition: border-color 0.2s ease;
  }

  .search-input:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.15);
  }

  .search-loading {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0.5rem;
    font-size: 0.85rem;
    color: var(--text-secondary);
  }

  .search-results {
    list-style: none;
    margin: 4px 0 0 0;
    padding: 0;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    max-height: 200px;
    overflow-y: auto;
    background: var(--surface-color);
  }

  .search-results li + li {
    border-top: 1px solid var(--border-color);
  }

  .result-item {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: none;
    background: transparent;
    color: var(--text-color);
    cursor: pointer;
    text-align: left;
    transition: background-color 0.15s ease;
  }

  .result-item:hover {
    background-color: var(--hover-color, rgba(59, 130, 246, 0.08));
  }

  .type-icon {
    flex-shrink: 0;
    color: var(--text-secondary);
  }

  .result-info {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .result-name {
    font-size: 0.875rem;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .result-detail {
    font-size: 0.75rem;
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .no-results {
    padding: 0.5rem;
    font-size: 0.85rem;
    color: var(--text-secondary);
    text-align: center;
  }

  :global([data-theme='dark']) .search-input {
    background-color: rgba(255, 255, 255, 0.05);
  }

  :global([data-theme='dark']) .search-results {
    background: var(--surface-color);
  }

  .search-hint {
    padding: 0.375rem 0.5rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
    text-align: center;
  }
</style>
