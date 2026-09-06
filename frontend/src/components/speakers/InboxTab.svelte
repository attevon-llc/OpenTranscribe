<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import SpeakerInboxItem from '$components/speakers/SpeakerInboxItem.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ListRowSkeleton from '$components/ui/ListRowSkeleton.svelte';
  import type {
    SpeakerInboxItem as InboxItem,
    SpeakerProfile
  } from '$lib/types/speakerCluster';

  export let loadingInbox = false;
  export let inboxItems: InboxItem[] = [];
  export let profiles: SpeakerProfile[] = [];
  export let inboxActionInProgress: Set<string> = new Set();
  export let inboxPage = 1;
  export let inboxPages = 0;

  const dispatch = createEventDispatcher();
</script>

<div class="tab-content">
  <div class="inbox-hint">
    {$t('speakers.keyboard.hint')}
  </div>
  {#if loadingInbox}
    <ListRowSkeleton count={5} size="compact" />
  {:else if inboxItems.length === 0}
    <EmptyState title={$t('speakers.inbox.emptyTitle')} description={profiles.length > 0 ? $t('speakers.inbox.emptyAllVerified') : $t('speakers.inbox.emptyNoProfiles')} padding="60px 20px">
      <svelte:fragment slot="icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="color: var(--success-color, #059669); opacity: 0.7;">
          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" />
        </svg>
      </svelte:fragment>
    </EmptyState>
  {:else}
    <div class="inbox-list">
      {#each inboxItems as item, idx (item.speaker_uuid)}
        <SpeakerInboxItem
          {item}
          {profiles}
          actionInProgress={inboxActionInProgress.has(item.speaker_uuid)}
          on:action
          on:preview
          on:prefetch
        />
      {/each}
    </div>

    {#if inboxPages > 1}
      <div class="pagination">
        <button disabled={inboxPage <= 1} on:click={() => dispatch('prevPage')}>
          {$t('speakers.pagination.prev')}
        </button>
        <span>{$t('speakers.pagination.pageOf', { page: inboxPage, pages: inboxPages })}</span>
        <button disabled={inboxPage >= inboxPages} on:click={() => dispatch('nextPage')}>
          {$t('speakers.pagination.next')}
        </button>
      </div>
    {/if}
  {/if}
</div>

<style>
  .inbox-hint {
    padding: 8px 16px;
    background: var(--hover-color);
    border-radius: 6px;
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 12px;
  }

  .inbox-list {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
    max-width: 100%;
  }

  .pagination {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 16px;
    margin-top: 16px;
    font-size: 14px;
    color: var(--text-secondary);
  }

  .pagination button {
    padding: 6px 14px;
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 6px;
    background: var(--card-background, #fff);
    color: var(--text-color, #111827);
    cursor: pointer;
    box-shadow: none;
    font-size: 14px;
  }

  .pagination button:hover:not(:disabled) {
    background: var(--hover-color, #f3f4f6);
    transform: none;
    box-shadow: none;
  }

  .pagination button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  @media (max-width: 768px) {
    .inbox-hint {
      padding: 6px 12px;
      font-size: 12px;
    }

    .pagination {
      gap: 8px;
      font-size: 13px;
    }

    .pagination button {
      padding: 6px 10px;
      font-size: 13px;
    }
  }
</style>
