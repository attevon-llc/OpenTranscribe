<!--
  AuditUserPicker.svelte — pick one member of the organization to filter the audit log by.

  Searches `GET /users/search`, which the server limits to the caller's own organization and
  which returns a masked address, never a real one. Only the uuid is sent on; the server still
  refuses a uuid outside the organization.
-->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import { anchoredPanel } from '$lib/actions/anchoredPanel';
  import { GroupsApi } from '$lib/api/groups';
  import type { UserSearchResult } from '$lib/types/groups';
  import { createDebouncedHandler } from '$lib/utils/debounce';
  import { t } from '$stores/locale';

  export let userUuid = '';
  export let userLabel = '';

  let query = '';
  let results: UserSearchResult[] = [];
  let searching = false;
  let inputElement: HTMLInputElement | undefined;
  const debounced = createDebouncedHandler(() => search(), 300);

  onDestroy(() => debounced.cleanup());

  function onInput() {
    debounced.cleanup();
    if (query.trim().length < 2) {
      results = [];
      return;
    }
    debounced.trigger();
  }

  async function search() {
    searching = true;
    try {
      results = await GroupsApi.searchUsers(query.trim());
    } catch (error) {
      console.error('Member search failed:', error);
      results = [];
    } finally {
      searching = false;
    }
  }

  function pick(user: UserSearchResult) {
    userUuid = user.uuid;
    userLabel = user.full_name || user.masked_email;
    query = '';
    results = [];
  }

  function clear() {
    userUuid = '';
    userLabel = '';
  }
</script>

<div class="picker">
  <span class="label-text" id="audit-user-label">{$t('orgAudit.userFilter')}</span>
  {#if userUuid}
    <span class="chip">
      <span class="chip-name">{userLabel}</span>
      <button type="button" class="chip-clear" aria-label={$t('common.clear')} on:click={clear}>
        &times;
      </button>
    </span>
  {:else}
    <input
      type="search"
      aria-labelledby="audit-user-label"
      placeholder={$t('orgAudit.userPlaceholder')}
      bind:this={inputElement}
      bind:value={query}
      on:input={onInput}
    />
    {#if results.length > 0 || searching}
      <ul class="results" role="listbox" use:anchoredPanel={{ anchor: inputElement ?? null, width: 'anchor', minWidth: 224 }}>
        {#if searching}
          <li class="hint">{$t('settings.auditLog.loadingShort')}</li>
        {/if}
        {#each results as user (user.uuid)}
          <li role="option" aria-selected="false">
            <button type="button" on:click={() => pick(user)}>
              <span>{user.full_name || user.masked_email}</span>
              {#if user.full_name}<span class="sub" dir="ltr">{user.masked_email}</span>{/if}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</div>

<style>
  .picker {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    font-size: 0.75rem;
  }

  .label-text {
    color: var(--color-text-secondary);
    font-size: var(--font-size-xs);
  }

  input {
    padding: 0.25rem 0.375rem;
    font-size: 0.75rem;
    height: 1.75rem;
    min-width: 11rem;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    height: 1.75rem;
    padding: 0 0.5rem;
    border-radius: 6px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .chip-clear {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary);
    font-size: 1rem;
    line-height: 1;
    padding: 0;
  }

  .chip-clear:hover {
    color: var(--text-color);
  }

  /* Placed by anchoredPanel: fixed on <body>, flips above / caps max-height to the room. */
  .results {
    overflow-y: auto;
    margin: 0;
    padding: 0.25rem;
    list-style: none;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  }

  .results button {
    display: flex;
    flex-direction: column;
    width: 100%;
    text-align: start;
    padding: 0.25rem 0.5rem;
    background: none;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    color: var(--text-color);
    font-size: 0.75rem;
  }

  .results button:hover {
    background: var(--button-hover);
  }

  .sub,
  .hint {
    color: var(--text-secondary);
    font-size: 0.6875rem;
  }

  .hint {
    padding: 0.25rem 0.5rem;
  }
</style>
