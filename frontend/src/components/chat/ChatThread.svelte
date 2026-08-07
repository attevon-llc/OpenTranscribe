<!--
  ChatThread.svelte — the scrolling message list.

  Auto-scroll follows the stream only while the user is already at the bottom.
  Yanking the viewport down while someone is reading an earlier answer is the
  single most annoying thing a chat UI can do, so scrolling up suppresses
  follow and surfaces a "jump to latest" pill instead.
-->
<script lang="ts">
  import { createEventDispatcher, afterUpdate, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import ChatMessage from './ChatMessage.svelte';
  import ChatStatusIndicator from './ChatStatusIndicator.svelte';
  import type { ChatMessage as ChatMessageType, StreamStatus } from '$lib/types/chat';

  export let messages: ChatMessageType[] = [];
  export let status: StreamStatus = 'idle';
  export let streamingMessageId: string | null = null;

  const dispatch = createEventDispatcher<{ regenerate: void; retry: void }>();

  /** Anything under this from the bottom counts as "following the stream". */
  const FOLLOW_THRESHOLD_PX = 120;

  let container: HTMLDivElement;
  let following = true;

  function atBottom(): boolean {
    if (!container) return true;
    const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
    return distance <= FOLLOW_THRESHOLD_PX;
  }

  function handleScroll(): void {
    following = atBottom();
  }

  function scrollToBottom(smooth = false): void {
    if (!container) return;
    container.scrollTo({
      top: container.scrollHeight,
      behavior: smooth ? 'smooth' : 'auto',
    });
    following = true;
  }

  onMount(() => scrollToBottom());

  afterUpdate(() => {
    if (following) scrollToBottom();
  });

  $: lastAssistantId = [...messages].reverse().find((m) => m.role === 'assistant')?.uuid;
</script>

<div class="thread-wrapper">
  <div
    class="thread"
    bind:this={container}
    on:scroll={handleScroll}
    role="log"
    aria-label={$t('chat.title')}
    data-testid="chat-thread"
  >
    {#each messages as message (message.uuid)}
      <ChatMessage
        {message}
        isLast={message.uuid === lastAssistantId}
        streaming={message.uuid === streamingMessageId && status === 'streaming'}
        on:regenerate={() => dispatch('regenerate')}
        on:retry={() => dispatch('retry')}
        on:edit
      />
    {/each}

    <ChatStatusIndicator {status} />
  </div>

  {#if !following}
    <button
      type="button"
      class="jump-pill"
      on:click={() => scrollToBottom(true)}
      data-testid="chat-jump-to-bottom"
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        aria-hidden="true"
      >
        <line x1="12" y1="5" x2="12" y2="19" />
        <polyline points="19 12 12 19 5 12" />
      </svg>
      {$t('chat.jumpToLatest')}
    </button>
  {/if}
</div>

<style>
  .thread-wrapper {
    position: relative;
    flex: 1;
    min-height: 0;
    display: flex;
  }

  .thread {
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem 0 0.5rem;
    scroll-behavior: auto;
  }

  .jump-pill {
    position: absolute;
    bottom: 1rem;
    left: 50%;
    transform: translateX(-50%);
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.4rem 0.9rem;
    border: 1px solid var(--border-color);
    border-radius: 999px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8rem;
    cursor: pointer;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
  }

  .jump-pill:hover {
    background-color: var(--button-hover);
  }
</style>
