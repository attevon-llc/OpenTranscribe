<!--
  ChatSidebar.svelte — conversation history.

  Date-grouped (Today / Yesterday / Last 7 days / …) rather than a flat list,
  because people find an old conversation by remembering roughly when they had
  it. Search and pagination are both server-side; the client never holds the
  whole history.
-->
<script lang="ts">
  import { SvelteSet } from 'svelte/reactivity';
  import { CHAT_GROUP_KEYS } from '$lib/i18n/keyMaps';
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { isThisMonth, isThisWeek, isToday, isYesterday, parseISO } from 'date-fns';
  import { t } from '$stores/locale';
  import SearchBar from '$components/ui/SearchBar.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import ConversationListItem from './ConversationListItem.svelte';
  import type { ChatProject, ConversationSummary } from '$lib/types/chat';

  export let conversations: ConversationSummary[] = [];
  /** Project groups, shown above the date-grouped ungrouped chats (#360). */
  export let projects: ChatProject[] = [];
  export let activeId: string | null = null;
  export let loading = false;
  export let hasMore = false;
  export let showArchived = false;

  const dispatch = createEventDispatcher<{
    select: string;
    rename: { uuid: string; title: string };
    delete: string;
    archive: string;
    toggleArchived: void;
    newChat: void;
    search: string;
    loadMore: void;
    newProject: void;
    editProject: string;
    newChatInProject: string;
  }>();

  /** Which project sections are expanded. Collapsed is the calmer default. */
  let expanded = new Set<string>();

  function toggleProject(uuid: string): void {
    // Reassigned rather than mutated so Svelte's reactivity sees the change.
    const next = new SvelteSet(expanded);
    if (next.has(uuid)) next.delete(uuid);
    else next.add(uuid);
    expanded = next;
  }

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

  // Conversations inside a project are listed under it; the date groups below
  // cover only ungrouped chats, so nothing appears twice.
  $: ungrouped = conversations.filter((c) => !c.project_uuid);

  // A plain function, not a reactive assignment: `conversations` is a prop, so
  // the markup re-evaluates these calls whenever it changes anyway.
  function byProject(uuid: string): ConversationSummary[] {
    return conversations.filter((c) => c.project_uuid === uuid);
  }

  $: grouped = GROUP_ORDER.map((key) => ({
    key,
    items: ungrouped.filter((c) => groupFor(c) === key),
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

    <button
      type="button"
      class="archive-toggle"
      class:active={showArchived}
      on:click={() => dispatch('toggleArchived')}
      aria-pressed={showArchived}
      data-testid="chat-toggle-archived"
    >
      {showArchived ? $t('chat.showActive') : $t('chat.showArchived')}
    </button>
  </div>

  <nav class="sidebar-list" aria-label={$t('chat.title')}>
    {#if !showArchived}
      <div class="group projects-group">
        <div class="projects-header">
          <h2 class="group-label">{$t('chat.projects.title')}</h2>
          <button
            type="button"
            class="add-project"
            on:click={() => dispatch('newProject')}
            title={$t('chat.projects.new')}
            aria-label={$t('chat.projects.new')}
            data-testid="chat-new-project"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" aria-hidden="true">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
          </button>
        </div>

        {#if projects.length === 0}
          <p class="projects-empty">{$t('chat.projects.empty')}</p>
        {:else}
          <ul class="project-list">
            {#each projects as project (project.uuid)}
              <li>
                <div class="project-row">
                  <button
                    type="button"
                    class="project-toggle"
                    on:click={() => toggleProject(project.uuid)}
                    aria-expanded={expanded.has(project.uuid)}
                    title={project.description || project.name}
                    data-testid="chat-project-toggle"
                  >
                    <svg class="chevron" class:open={expanded.has(project.uuid)} width="12"
                         height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="2" aria-hidden="true">
                      <polyline points="9 18 15 12 9 6" />
                    </svg>
                    <span class="project-name">{project.name}</span>
                    <span class="project-count">{project.conversation_count}</span>
                  </button>
                  <button
                    type="button"
                    class="project-action"
                    on:click|stopPropagation={() => dispatch('newChatInProject', project.uuid)}
                    title={$t('chat.projects.newChatHere')}
                    aria-label={$t('chat.projects.newChatHere')}
                    data-testid="chat-project-new-chat"
                  >
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="2" aria-hidden="true">
                      <line x1="12" y1="5" x2="12" y2="19" />
                      <line x1="5" y1="12" x2="19" y2="12" />
                    </svg>
                  </button>
                  <button
                    type="button"
                    class="project-action"
                    on:click|stopPropagation={() => dispatch('editProject', project.uuid)}
                    title={$t('chat.projects.settings')}
                    aria-label={$t('chat.projects.settings')}
                    data-testid="chat-project-edit"
                  >
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="2" aria-hidden="true">
                      <path d="M12 20h9" />
                      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
                    </svg>
                  </button>
                </div>

                {#if expanded.has(project.uuid)}
                  <ul class="project-conversations">
                    {#each byProject(project.uuid) as conversation (conversation.uuid)}
                      <ConversationListItem
                        {conversation}
                        active={conversation.uuid === activeId}
                        {showArchived}
                        on:select
                        on:rename
                        on:delete
                        on:archive
                      />
                    {:else}
                      <li class="project-empty">{$t('chat.projects.noChats')}</li>
                    {/each}
                  </ul>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}

    {#each grouped as group (group.key)}
      <div class="group">
        <h2 class="group-label">{$t(CHAT_GROUP_KEYS[group.key])}</h2>
        <ul>
          {#each group.items as conversation (conversation.uuid)}
            <ConversationListItem
              {conversation}
              active={conversation.uuid === activeId}
              {showArchived}
              on:select
              on:rename
              on:delete
              on:archive
            />
          {/each}
        </ul>
      </div>
    {/each}

    {#if !loading && conversations.length === 0}
      <p class="empty">
        {showArchived ? $t('chat.emptyArchive') : $t('chat.emptyHistory')}
      </p>
    {/if}

    <div class="sentinel" bind:this={sentinel}>
      {#if loading}
        <Spinner label={$t('common.loading')} size="small" />
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
    gap: var(--space-2-5);
    padding: var(--space-3-5) var(--space-3);
    border-bottom: 1px solid var(--border-color);
    position: sticky;
    top: 0;
    background-color: var(--surface-color);
    z-index: var(--z-base);
  }

  .new-chat-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-1-5);
    width: 100%;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background-color: var(--card-background);
    color: var(--text-color);
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    cursor: pointer;
    transition: background-color var(--duration-fast) ease;
  }

  .new-chat-btn:hover {
    background-color: var(--button-hover);
  }

  .archive-toggle {
    align-self: flex-start;
    padding: var(--space-0-5) var(--space-1-5);
    margin-left: calc(-1 * var(--space-1-5));
    border: none;
    border-radius: var(--radius-md);
    /* form-elements.css's bare `button` rule sets a drop-shadow that isn't
       cleared by border/background alone (see ConversationListItem.svelte). */
    box-shadow: none;
    background: none;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    cursor: pointer;
  }

  .archive-toggle:hover {
    background-color: var(--button-hover);
    color: var(--text-color);
  }

  .archive-toggle.active {
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-medium);
  }

  .sidebar-list {
    flex: 1;
    overflow-y: auto;
    padding: var(--space-2) var(--space-2) var(--space-4);
    min-height: 0;
  }

  .group + .group {
    margin-top: var(--space-3-5);
  }

  /* --- projects (#360) --- */
  .projects-group {
    border-bottom: 1px solid var(--border-color);
    padding-bottom: var(--space-2-5);
    margin-bottom: var(--space-1);
  }

  .projects-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .add-project,
  .project-action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    flex: none;
    /* form-elements.css styles every bare <button>; without these resets the
       icon collapses to zero width inside its own padding. */
    padding: 0;
    box-shadow: none;
    border: none;
    border-radius: var(--radius-md);
    background: none;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .add-project:hover,
  .project-action:hover {
    background-color: rgba(var(--primary-color-rgb), 0.18);
    color: var(--text-color);
    transform: none;
    box-shadow: none;
  }

  .project-list,
  .project-conversations {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .project-conversations {
    margin-left: var(--space-3);
    border-left: 1px solid var(--border-color);
    padding-left: var(--space-1);
  }

  .project-row {
    display: flex;
    align-items: center;
    gap: var(--space-0-5);
    border-radius: var(--radius-lg);
    padding: 0 var(--space-1);
  }

  .project-row:hover {
    background-color: var(--button-hover);
  }

  .project-toggle {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    padding: var(--space-2) var(--space-1-5);
    border: none;
    background: none;
    box-shadow: none;
    color: var(--text-color);
    font-size: var(--font-size-md);
    text-align: left;
    cursor: pointer;
  }

  .project-toggle:hover,
  .project-toggle:focus {
    background: none;
    transform: none;
    box-shadow: none;
  }

  .chevron {
    flex: none;
    transition: transform var(--duration-fast) ease;
  }

  .chevron.open {
    transform: rotate(90deg);
  }

  .project-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .project-count {
    flex: none;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .projects-empty,
  .project-empty {
    margin: var(--space-1-5) var(--space-1-5) 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    list-style: none;
  }

  .group-label {
    margin: 0 0 var(--space-1);
    padding: 0 var(--space-1-5);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
  }

  .empty {
    padding: var(--space-6) var(--space-3);
    text-align: center;
    font-size: var(--font-size-md);
    color: var(--text-secondary);
  }

  .sentinel {
    display: flex;
    justify-content: center;
    min-height: 1.5rem;
    padding-top: var(--space-2);
  }
</style>
