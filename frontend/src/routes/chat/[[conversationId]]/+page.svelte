<!--
  /chat — the RAG chat page.

  Coordinator only: it owns layout, guards and route↔store synchronisation;
  every piece of behaviour lives in a child component or the chat store.

  Route shape is `[[conversationId]]` so `/chat` (a fresh conversation, no server
  row yet) and `/chat/{uuid}` (an existing thread) are the same page. The server
  row is created lazily on first send, which keeps abandoned "I opened chat and
  changed my mind" visits from littering the history sidebar.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { t } from '$stores/locale';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { llmStatusStore } from '$stores/llmStatus';
  import { chatStore } from '$stores/chat';
  import { estimateContext } from '$lib/api/chatApi';
  import { emptyScope, type ChatScope, type ConversationSettings } from '$lib/types/chat';

  import ChatSidebar from '$components/chat/ChatSidebar.svelte';
  import ChatThread from '$components/chat/ChatThread.svelte';
  import ChatComposer from '$components/chat/ChatComposer.svelte';
  import ChatContextBar from '$components/chat/ChatContextBar.svelte';
  import ChatEmptyState from '$components/chat/ChatEmptyState.svelte';
  import ChatControlsPanel from '$components/chat/ChatControlsPanel.svelte';
  import FilePickerModal from '$components/chat/FilePickerModal.svelte';
  import TokenUsagePanel from '$components/chat/TokenUsagePanel.svelte';
  import LargeSelectionWarningModal from '$components/chat/LargeSelectionWarningModal.svelte';

  import type { PageData } from './$types';

  export let data: PageData;

  let composer: ChatComposer;
  let composerValue = '';
  let pickerOpen = false;
  let controlsOpen = false;
  let sidebarOpen = false;
  let lastLoadedId: string | null = null;
  let gearEl: HTMLButtonElement;
  let isNarrow = false;
  let mediaCleanup: (() => void) | undefined;
  let pendingScope: ChatScope | null = null;
  let contextWindow = 0;

  $: chatEnabled = isCapabilityEnabled($capabilities, 'chat.rag');
  $: llmAvailable = $llmStatusStore.available;
  $: state = $chatStore;
  $: hasMessages = state.messages.length > 0;
  $: settings = state.activeConversation?.settings ?? state.draftSettings;
  $: isStreaming = ['submitting', 'retrieving', 'thinking', 'streaming'].includes(
    state.streamStatus
  );

  // Route → store. Opening a different conversation (including via back/forward)
  // loads it; landing on bare /chat resets to a fresh thread.
  //
  // The self-created check is what keeps the FIRST answer of a new chat alive:
  // sending on /chat creates the conversation and navigates to /chat/{uuid},
  // which lands here mid-stream. Reloading at that moment would clear the
  // optimistic messages the stream is writing into — and the assistant row does
  // not exist server-side until the stream finishes, so the reload would show a
  // lone question and every delta would land nowhere.
  $: if (data.conversationId && data.conversationId !== lastLoadedId) {
    lastLoadedId = data.conversationId;
    if (!chatStore.consumeSelfCreated(data.conversationId)) {
      chatStore.openConversation(data.conversationId);
    }
  } else if (!data.conversationId && lastLoadedId !== null) {
    lastLoadedId = null;
    chatStore.newConversation();
  }

  onMount(() => {
    chatStore.loadConversations(true);

    // Mirror the 900px breakpoint the layout uses. Below it the sidebar is an
    // off-screen drawer; above it, a normal column that must stay reachable.
    const narrow = window.matchMedia('(max-width: 900px)');
    isNarrow = narrow.matches;
    const onChange = (event: MediaQueryListEvent) => {
      isNarrow = event.matches;
    };
    narrow.addEventListener('change', onChange);
    mediaCleanup = () => narrow.removeEventListener('change', onChange);
    // The token panel needs the active model's window to show a ratio.
    (async () => {
      try {
        const { LLMSettingsApi } = await import('$lib/api/llmSettings');
        const status = await LLMSettingsApi.getStatus();
        contextWindow = status.active_configuration?.max_tokens ?? 0;
      } catch {
        contextWindow = 0;
      }
    })();

    // A gallery hand-off ("Chat with 12") is consumed exactly once, so a later
    // navigation back to /chat doesn't silently re-apply a stale selection.
    const handoff = chatStore.consumePendingContext();
    const deepLinked = data.fileUuids.length ? data.fileUuids : null;
    const initial = handoff ?? deepLinked;
    if (initial?.length && !data.conversationId) {
      chatStore.setScope({ ...emptyScope(), file_uuids: initial });
      chatStore.refreshEstimate();
    }

    if (!data.conversationId) {
      lastLoadedId = null;
    }
  });

  onDestroy(() => {
    mediaCleanup?.();
    // Leaving the page must not leave a stream running in the background.
    if (state.streamStatus === 'streaming') chatStore.stopGeneration();
  });

  /**
   * App-level chat shortcuts, matching what ChatGPT users already reach for.
   *
   * Escape only stops an in-flight generation — it must not steal the key from
   * an open modal or an inline rename, so it checks that a stream is running.
   */
  function handleShortcuts(event: KeyboardEvent): void {
    const mod = event.metaKey || event.ctrlKey;

    if (mod && event.shiftKey && event.key.toLowerCase() === 'o') {
      event.preventDefault();
      handleNewChat();
      return;
    }

    if (event.key === 'Escape' && sidebarOpen) {
      event.preventDefault();
      sidebarOpen = false;
      return;
    }

    if (event.key === 'Escape' && isStreaming) {
      event.preventDefault();
      chatStore.stopGeneration();
      return;
    }

    // Focus the composer from anywhere that isn't already a text field.
    if (mod && event.key === '/') {
      const tag = (event.target as HTMLElement)?.tagName;
      if (tag !== 'INPUT' && tag !== 'TEXTAREA') {
        event.preventDefault();
        composer?.focus();
      }
    }
  }

  async function handleSend(event: CustomEvent<string>): Promise<void> {
    await chatStore.sendMessage(event.detail);
  }

  function handleSuggestion(event: CustomEvent<string>): void {
    composerValue = event.detail;
    composer?.focus();
  }

  async function handleSelectConversation(event: CustomEvent<string>): Promise<void> {
    sidebarOpen = false;
    await goto(`/chat/${event.detail}`);
  }

  async function handleNewChat(): Promise<void> {
    sidebarOpen = false;
    chatStore.newConversation();
    await goto('/chat');
  }

  async function applyScope(scope: ChatScope): Promise<void> {
    await chatStore.persistScope(scope);
    await chatStore.refreshEstimate();
  }

  async function handleScopeConfirm(event: CustomEvent<ChatScope>): Promise<void> {
    pickerOpen = false;
    const scope = event.detail;

    // An oversized selection does not fail — it quietly thins each recording's
    // contribution, so confirm rather than let the user discover it from a
    // vague answer.
    try {
      const estimate = await estimateContext(scope);
      if (estimate.warning_level === 'over') {
        pendingScope = scope;
        chatStore.setScope(scope);
        return;
      }
    } catch {
      // Estimator unavailable — proceed rather than block on an advisory check.
    }

    await applyScope(scope);
  }

  async function confirmLargeSelection(): Promise<void> {
    const scope = pendingScope;
    pendingScope = null;
    if (scope) await applyScope(scope);
  }

  function cancelLargeSelection(): void {
    pendingScope = null;
    // Restore whatever the conversation was actually scoped to.
    chatStore.setScope(state.activeConversation?.scope ?? emptyScope());
  }

  async function handleModelChange(uuid: string | null): Promise<void> {
    // Buffers when no conversation exists yet, so choosing a model on a fresh
    // /chat is applied when the row is created rather than silently dropped.
    await chatStore.applyModel(uuid);
  }

  async function handleClearScope(): Promise<void> {
    await chatStore.persistScope(emptyScope());
    await chatStore.refreshEstimate();
  }

  async function handleControlsChange(event: CustomEvent<Partial<ConversationSettings>>) {
    const patch = event.detail;
    if ('use_context' in patch) {
      await chatStore.setUseContext(Boolean(patch.use_context));
      return;
    }
    await chatStore.applySettings(patch);
  }
</script>

<svelte:window on:keydown={handleShortcuts} />

<svelte:head>
  <title>{$t('chat.title')} · OpenTranscribe</title>
</svelte:head>

{#if !chatEnabled}
  <div class="chat-unavailable">
    <p>{$t('chat.errors.unavailable')}</p>
  </div>
{:else}
  <div class="chat-page" class:sidebar-open={sidebarOpen}>
    <div class="sidebar-pane" inert={isNarrow && !sidebarOpen ? true : undefined}>
      <ChatSidebar
        conversations={state.conversations}
        activeId={state.activeConversationId}
        loading={state.conversationsLoading}
        hasMore={state.conversations.length < state.conversationsTotal}
        showArchived={state.showArchived}
        on:select={handleSelectConversation}
        on:newChat={handleNewChat}
        on:search={(e) => chatStore.searchConversations(e.detail)}
        on:rename={(e) => chatStore.renameConversation(e.detail.uuid, e.detail.title)}
        on:delete={(e) => chatStore.deleteConversation(e.detail)}
        on:archive={(e) => chatStore.setArchived(e.detail, !state.showArchived)}
        on:toggleArchived={() => chatStore.toggleArchivedView()}
        on:loadMore={() => chatStore.loadConversations()}
      />
    </div>

    {#if sidebarOpen}
      <button
        type="button"
        class="sidebar-scrim"
        aria-label={$t('common.close')}
        on:click={() => (sidebarOpen = false)}
      ></button>
    {/if}

    <main class="chat-main">
      <header class="chat-header">
        <button
          type="button"
          class="hamburger"
          on:click={() => (sidebarOpen = !sidebarOpen)}
          aria-label={$t('chat.toggleSidebar')}
          aria-expanded={sidebarOpen}
        >
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            aria-hidden="true"
          >
            <line x1="3" y1="12" x2="21" y2="12" />
            <line x1="3" y1="6" x2="21" y2="6" />
            <line x1="3" y1="18" x2="21" y2="18" />
          </svg>
        </button>

        <h1 class="chat-title">
          {state.activeConversation?.title || $t('chat.newChat')}
        </h1>

        {#if state.activeConversationId}
          <button
            type="button"
            class="gear"
            on:click={() => chatStore.exportConversation('markdown')}
            aria-label={$t('chat.conversation.export')}
            title={$t('chat.conversation.export')}
            data-testid="chat-export"
          >
            <svg
              width="17"
              height="17"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              aria-hidden="true"
            >
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
          </button>
        {/if}

        <button
          type="button"
          class="gear"
          bind:this={gearEl}
          on:click={() => (controlsOpen = !controlsOpen)}
          aria-label={$t('chat.controls.title')}
          aria-expanded={controlsOpen}
          data-testid="chat-controls-toggle"
        >
          <svg
            width="17"
            height="17"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="3" />
            <path
              d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"
            />
          </svg>
        </button>

        <ChatControlsPanel
          isOpen={controlsOpen}
          {settings}
          useContext={state.useContext}
          llmConfigUuid={state.activeConversation?.llm_config_uuid ?? state.draftLlmConfigUuid}
          triggerEl={gearEl}
          on:change={handleControlsChange}
          on:model={(e) => handleModelChange(e.detail)}
          on:close={() => (controlsOpen = false)}
        />
      </header>

      <div class="chat-body">
        {#if state.error}
          <div class="chat-error" role="alert" data-testid="chat-error-banner">
            <span>{$t(`chat.errors.${state.error}`)}</span>
            {#if state.error === 'conversationNotFound'}
              <button type="button" class="error-action" on:click={handleNewChat}>
                {$t('chat.newChat')}
              </button>
            {/if}
          </div>
        {/if}

        {#if hasMessages}
          <ChatThread
            messages={state.messages}
            status={state.streamStatus}
            streamingMessageId={state.streamingMessageId}
            on:regenerate={() => chatStore.regenerate()}
            on:retry={() => chatStore.regenerate()}
            on:edit={(e) => chatStore.editMessage(e.detail.uuid, e.detail.content)}
          />
        {:else}
          <ChatEmptyState {llmAvailable} on:suggestion={handleSuggestion} />
        {/if}
      </div>

      <div class="chat-footer">
        <TokenUsagePanel usage={state.tokenUsage} {contextWindow} />
        <ChatContextBar
          scope={state.scope}
          useContext={state.useContext}
          estimate={state.contextEstimate}
          on:openPicker={() => (pickerOpen = true)}
          on:clear={handleClearScope}
        />
        <ChatComposer
          bind:this={composer}
          bind:value={composerValue}
          status={state.streamStatus}
          disabled={!llmAvailable}
          on:send={handleSend}
          on:stop={() => chatStore.stopGeneration()}
        />
      </div>
    </main>
  </div>

  <FilePickerModal
    isOpen={pickerOpen}
    scope={state.scope}
    on:confirm={handleScopeConfirm}
    on:close={() => (pickerOpen = false)}
  />

  <LargeSelectionWarningModal
    isOpen={pendingScope !== null}
    estimate={state.contextEstimate}
    on:proceed={confirmLargeSelection}
    on:cancel={cancelLargeSelection}
  />
{/if}

<style>
  .chat-page {
    display: grid;
    grid-template-columns: 17rem 1fr;
    /* Full-bleed, like every other chat product: the composer sits ON the
       viewport bottom and the page itself never scrolls — only the thread does.
       The app shell pads .content, so cancel that padding rather than
       subtracting it from the height; otherwise the shell's padding is added
       back below the page and the whole window scrolls by exactly 2x padding. */
    margin: -1rem;
    height: calc(100vh - var(--navbar-height, 60px));
    overflow: hidden;
  }

  /* Mirrors .content's own breakpoint in +layout.svelte. The negative margin
     must equal the shell's padding exactly: any mismatch reappears as page
     scroll of 2x the difference. */
  @media (min-width: 768px) {
    .chat-page {
      margin: -2rem;
    }
  }

  .sidebar-pane {
    min-height: 0;
    overflow: hidden;
  }

  .chat-main {
    position: relative;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    padding: 0 1.25rem;
  }

  .chat-header {
    position: relative;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 0;
    border-bottom: 1px solid var(--border-color);
  }

  .chat-title {
    flex: 1;
    margin: 0;
    font-size: 0.98rem;
    font-weight: 600;
    color: var(--text-color);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .hamburger,
  .gear {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border: none;
    border-radius: 6px;
    background: none;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .hamburger:hover,
  .gear:hover {
    background-color: var(--button-hover);
    color: var(--text-color);
  }

  .hamburger {
    display: none;
  }

  .chat-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    max-width: 52rem;
    width: 100%;
    margin: 0 auto;
  }

  .chat-footer {
    max-width: 52rem;
    width: 100%;
    margin: 0 auto;
    padding-bottom: max(0.75rem, env(safe-area-inset-bottom));
  }

  .chat-error {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin: 0.75rem 0 0;
    padding: 0.6rem 0.85rem;
    border: 1px solid rgba(var(--error-color-rgb, 220, 53, 69), 0.35);
    border-radius: 8px;
    background-color: rgba(var(--error-color-rgb, 220, 53, 69), 0.08);
    color: var(--error-color, #dc3545);
    font-size: 0.85rem;
  }

  .error-action {
    margin-left: auto;
    padding: 0.2rem 0.6rem;
    border: 1px solid currentColor;
    border-radius: 6px;
    background: none;
    color: inherit;
    font-size: 0.78rem;
    cursor: pointer;
  }

  .error-action:hover {
    background-color: rgba(var(--error-color-rgb, 220, 53, 69), 0.12);
  }

  .chat-unavailable {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 60vh;
    color: var(--text-secondary);
  }

  .sidebar-scrim {
    display: none;
  }

  @media (max-width: 900px) {
    .chat-page {
      grid-template-columns: 1fr;
    }

    .hamburger {
      display: inline-flex;
    }

    .sidebar-pane {
      position: fixed;
      top: var(--navbar-height, 60px);
      bottom: 0;
      left: 0;
      width: min(17rem, 82vw);
      z-index: 30;
      transform: translateX(-100%);
      transition: transform 0.2s ease;
    }

    .chat-page.sidebar-open .sidebar-pane {
      transform: translateX(0);
    }

    .sidebar-scrim {
      display: block;
      position: fixed;
      inset: var(--navbar-height, 60px) 0 0 0;
      z-index: 29;
      border: none;
      background-color: rgba(0, 0, 0, 0.4);
      cursor: pointer;
    }

    .chat-main {
      padding: 0 0.85rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .sidebar-pane {
      transition: none;
    }
  }
</style>
