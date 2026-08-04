<!--
  ChatSidebar.svelte — conversation history.

  Date-grouped (Today / Yesterday / Last 7 days / …) rather than a flat list,
  because people find an old conversation by remembering roughly when they had
  it. Search and pagination are both server-side; the client never holds the
  whole history.
-->
<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { isThisMonth, isThisWeek, isToday, isYesterday, parseISO } from 'date-fns';
  import { t } from '$stores/locale';
  import SearchBar from '$components/ui/SearchBar.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import ConversationListItem from './ConversationListItem.svelte';
  import type { ConversationSummary } from '$lib/types/chat';

  export let conversations: ConversationSummary[] = [];
  export let activeId: string | null = null;
  export let loading = false;
  export let hasMore = false;

  const dispatch = createEventDispatcher<{
    select: string;
    rename: { uuid: string; title: string };
    delete: string;
    newChat: void;
    search: string;
    loadMore: void;
  }>();

  let query = '';
  let sentinel: HTMLDivElement;
  let observer: IntersectionObserver | undefined;

  type GroupKey = 'today' | 'yesterday' | 'last7Days' | 'last30Days' | 'older';

  const GROUP_ORDER: GroupKey[] = ['today', 'yesterday', 'last7Days', 'last30Days', 'older'];

  function groupFor(conversation: ConversationSummary): GroupKey {
    const raw = conversation.last_message_at ?? conversation.created_at;
    if (!raw) return 'older';
    const date = parseISO(raw);
    if (isToday(date)) return 'today';
    if (isYesterday(date)) return 'yesterday';
    if (isThisWeek(date, { weekStartsOn: 1 })) return 'last7Days';
    if (isThisMonth(date)) return 'last30Days';
    return 'older';
  }

  $: grouped = GROUP_ORDER.map((key) => ({
    key,
    items: conversations.filter((c) => groupFor(c) === key),
  })).filter((group) => group.items.length > 0);

  onMount(() => {
    observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && hasMore && !loading) dispatch('loadMore');
      },
      { rootMargin: '120px' }
    );
    if (sentinel) observer.observe(sentinel);
  });

  onDestroy(() => observer?.disconnect());
</script>

<aside class="chat-sidebar" data-testid="chat-sidebar">
  <div class="sidebar-header">
    <button
      type="button"
      class="new-chat-btn"
      on:click={() => dispatch('newChat')}
      data-testid="chat-new"
    >
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        aria-hidden="true"
      >
        <line x1="12" y1="5" x2="12" y2="19" />
        <line x1="5" y1="12" x2="19" y2="12" />
      </svg>
      {$t('chat.newChat')}
    </button>

    <SearchBar
      bind:value={query}
      placeholder={$t('chat.searchPlaceholder')}
      ariaLabel={$t('chat.searchPlaceholder')}
      size="sm"
      on:search={() => dispatch('search', query)}
    />
  </div>

  <nav class="sidebar-list" aria-label={$t('chat.title')}>
    {#each grouped as group (group.key)}
      <div class="group">
        <h2 class="group-label">{$t(`chat.groups.${group.key}`)}</h2>
        <ul>
          {#each group.items as conversation (conversation.uuid)}
            <ConversationListItem
              {conversation}
              active={conversation.uuid === activeId}
              on:select
              on:rename
              on:delete
            />
          {/each}
        </ul>
      </div>
    {/each}

    {#if !loading && conversations.length === 0}
      <p class="empty">{$t('chat.emptyHistory')}</p>
    {/if}

    <div class="sentinel" bind:this={sentinel}>
      {#if loading}
        <Spinner size="small" />
      {/if}
    </div>
  </nav>
</aside>

<style>
  .chat-sidebar {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    border-right: 1px solid var(--border-color);
    background-color: var(--surface-color);
  }

  .sidebar-header {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 0.85rem 0.75rem;
    border-bottom: 1px solid var(--border-color);
    position: sticky;
    top: 0;
    background-color: var(--surface-color);
    z-index: 1;
  }

  .new-chat-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    width: 100%;
    padding: 0.55rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--card-background);
    color: var(--text-color);
    font-size: 0.87rem;
    font-weight: 500;
    cursor: pointer;
    transition: background-color 0.15s ease;
  }

  .new-chat-btn:hover {
    background-color: var(--button-hover);
  }

  .sidebar-list {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem 0.5rem 1rem;
    min-height: 0;
  }

  .group + .group {
    margin-top: 0.85rem;
  }

  .group-label {
    margin: 0 0 0.3rem;
    padding: 0 0.35rem;
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
  }

  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }

  .empty {
    padding: 1.5rem 0.75rem;
    text-align: center;
    font-size: 0.82rem;
    color: var(--text-secondary);
  }

  .sentinel {
    display: flex;
    justify-content: center;
    min-height: 1.5rem;
    padding-top: 0.5rem;
  }
</style>
