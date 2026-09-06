<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import type { UserSearchResult } from '$lib/types/groups';
  import { GroupsApi } from '$lib/api/groups';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { getInitials } from '$lib/utils/formatting';
  import { createDebouncedHandler } from '$lib/utils/debounce';
  import Spinner from '../ui/Spinner.svelte';

  export let groupUuid: string;
  export let existingMemberUuids: string[] = [];

  const dispatch = createEventDispatcher<{
    memberAdded: { userUuid: string };
  }>();

  let searchQuery = '';
  let searchResults: UserSearchResult[] = [];
  let isSearching = false;
  let addingUserUuid: string | null = null;
  let selectedRole: 'admin' | 'member' = 'member';
  const debouncedSearch = createDebouncedHandler(() => performSearch(), 300);

  onDestroy(() => {
    debouncedSearch.cleanup();
  });

  $: filteredResults = searchResults.filter(
    (user) => !existingMemberUuids.includes(user.uuid)
  );

  function handleSearchInput() {
    debouncedSearch.cleanup();

    if (searchQuery.trim().length < 2) {
      searchResults = [];
      return;
    }

    debouncedSearch.trigger();
  }

  async function performSearch() {
    if (searchQuery.trim().length < 2) return;
    isSearching = true;

    try {
      searchResults = await GroupsApi.searchUsers(searchQuery.trim());
    } catch (err: unknown) {
      console.error('User search failed:', err);
      searchResults = [];
    } finally {
      isSearching = false;
    }
  }

  async function handleAddMember(user: UserSearchResult) {
    addingUserUuid = user.uuid;

    try {
      await GroupsApi.addMember(groupUuid, {
        user_uuid: user.uuid,
        role: selectedRole,
      });

      toastStore.success($t('groups.toast.memberAdded'));
      dispatch('memberAdded', { userUuid: user.uuid });

      // Clear search after successful add (dropdown-select pattern)
      searchQuery = '';
      searchResults = [];
    } catch (err: unknown) {
      const message = getErrorMessage(err, $t('groups.toast.addMemberFailed'));
      toastStore.error(message);
    } finally {
      addingUserUuid = null;
    }
  }

</script>

<div class="member-search">
  <div class="search-header">
    <h4 class="search-title">{$t('groups.addMembers')}</h4>
  </div>

  <div class="search-controls">
    <div class="search-input-wrapper">
      <svg class="search-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="11" cy="11" r="8"></circle>
        <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
      </svg>
      <input
        type="text"
        class="search-input"
        placeholder={$t('groups.searchUsersPlaceholder')}
        bind:value={searchQuery}
        on:input={handleSearchInput}
      />
      {#if isSearching}
        <Spinner size="small" />
      {/if}
    </div>

    <select class="role-picker" bind:value={selectedRole}>
      <option value="member">{$t('groups.roles.member')}</option>
      <option value="admin">{$t('groups.roles.admin')}</option>
    </select>
  </div>

  {#if searchQuery.trim().length > 0 && searchQuery.trim().length < 2}
    <div class="search-hint">{$t('groups.searchMinChars')}</div>
  {/if}

  {#if filteredResults.length > 0}
    <ul class="search-results">
      {#each filteredResults as user (user.uuid)}
        <li>
          <button
            class="result-item"
            on:click={() => handleAddMember(user)}
            disabled={addingUserUuid === user.uuid}
          >
            <div class="result-avatar">
              {getInitials(user.full_name, user.email)}
            </div>
            <div class="result-info">
              <span class="result-name">{user.full_name || user.email}</span>
              {#if user.full_name}
                <span class="result-email">{user.email}</span>
              {/if}
            </div>
            {#if addingUserUuid === user.uuid}
              <Spinner size="small" />
            {/if}
          </button>
        </li>
      {/each}
    </ul>
  {:else if searchQuery.trim().length >= 2 && !isSearching}
    <div class="no-results">{$t('groups.noUsersFound')}</div>
  {/if}
</div>

<style>
  .member-search {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .search-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .search-title {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 0;
  }

  .search-controls {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
  }

  .search-input-wrapper {
    flex: 1;
    min-width: 0;
    position: relative;
    display: flex;
    align-items: center;
  }

  .search-icon {
    position: absolute;
    left: 0.625rem;
    color: var(--text-secondary);
    pointer-events: none;
  }

  .search-input {
    width: 100%;
    min-width: 120px;
    padding: 0.5rem 0.625rem 0.5rem 2rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .search-input:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light);
  }

  .search-input::placeholder {
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .role-picker {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    cursor: pointer;
    flex-shrink: 0;
    max-width: 120px;
  }

  .role-picker:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light);
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
    gap: 0.625rem;
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: none;
    background: transparent;
    color: var(--text-color);
    cursor: pointer;
    text-align: left;
    transition: background-color 0.15s ease;
  }

  .result-item:hover:not(:disabled) {
    background-color: var(--hover-color, rgba(59, 130, 246, 0.08));
  }

  .result-item:disabled {
    opacity: 0.6;
    cursor: wait;
  }

  .result-avatar {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background-color: var(--primary-color);
    color: white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.625rem;
    font-weight: 600;
    flex-shrink: 0;
  }

  .result-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .result-name {
    font-size: 0.8125rem;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .result-email {
    font-size: 0.75rem;
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .no-results {
    padding: 0.5rem;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    text-align: center;
  }

  .search-hint {
    padding: 0.375rem 0.5rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
    text-align: center;
  }
</style>
