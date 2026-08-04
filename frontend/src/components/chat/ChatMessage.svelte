<!--
  ChatMessage.svelte — one turn in the thread.

  User messages render as PLAIN TEXT with `white-space: pre-wrap`, never through
  the markdown pipeline: there is no reason to interpret markup a user typed into
  their own prompt, and not doing so removes a whole class of self-XSS.
  Assistant messages go through ChatMarkdown's sanitized pipeline.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import CopyButton from '$components/ui/CopyButton.svelte';
  import ChatMarkdown from './ChatMarkdown.svelte';
  import ChatMessageMeta from './ChatMessageMeta.svelte';
  import ChatSources from './ChatSources.svelte';
  import type { ChatMessage } from '$lib/types/chat';

  export let message: ChatMessage;
  /** True for the newest assistant message (gets the regenerate affordance). */
  export let isLast = false;
  export let streaming = false;

  const dispatch = createEventDispatcher<{ regenerate: void; retry: void }>();

  $: isUser = message.role === 'user';
  $: hasError = message.status === 'error';
  $: wasCancelled = message.status === 'cancelled';
  $: sources = message.citations ?? [];
</script>

<article
  class="chat-message"
  class:user={isUser}
  class:assistant={!isUser}
  data-testid={isUser ? 'chat-message-user' : 'chat-message-assistant'}
  data-status={message.status ?? 'complete'}
>
  <div class="bubble">
    {#if isUser}
      <p class="user-text">{message.content}</p>
    {:else}
      {#if message.content}
        <ChatMarkdown content={message.content} {streaming} />
      {:else if !hasError}
        <span class="sr-only">{$t('chat.status.thinking')}</span>
      {/if}

      {#if hasError}
        <p class="error-text" data-testid="chat-message-error">
          {message.error || $t('chat.message.errorGeneric')}
        </p>
      {/if}

      {#if wasCancelled}
        <p class="cancelled-note">{$t('chat.message.aborted')}</p>
      {/if}

      <ChatSources {sources} />
      <ChatMessageMeta {message} />
    {/if}
  </div>

  {#if !streaming}
    <div class="actions" class:always-visible={hasError}>
      {#if message.content}
        <CopyButton
          text={message.content}
          iconOnly
          title={$t('chat.message.copy')}
          label={$t('chat.message.copy')}
          copiedLabel={$t('chat.message.copied')}
        />
      {/if}

      {#if !isUser && isLast && !hasError}
        <button
          type="button"
          class="action-btn"
          on:click={() => dispatch('regenerate')}
          title={$t('chat.message.regenerate')}
          aria-label={$t('chat.message.regenerate')}
          data-testid="chat-regenerate"
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
            <polyline points="23 4 23 10 17 10" />
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
          </svg>
        </button>
      {/if}

      {#if !isUser && hasError}
        <button
          type="button"
          class="action-btn"
          on:click={() => dispatch('retry')}
          data-testid="chat-retry"
        >
          {$t('chat.message.retry')}
        </button>
      {/if}
    </div>
  {/if}
</article>

<style>
  .chat-message {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    margin-bottom: 1.5rem;
  }

  .chat-message.user {
    align-items: flex-end;
  }

  .bubble {
    max-width: 100%;
  }

  .chat-message.user .bubble {
    max-width: min(80%, 42rem);
    padding: 0.7rem 1rem;
    border-radius: 16px 16px 4px 16px;
    background-color: rgba(var(--primary-color-rgb), 0.1);
    border: 1px solid rgba(var(--primary-color-rgb), 0.2);
  }

  .user-text {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: 0.95rem;
    line-height: 1.55;
    color: var(--text-color);
  }

  .error-text {
    margin: 0.25rem 0 0;
    padding: 0.6rem 0.75rem;
    border-radius: 8px;
    background-color: rgba(var(--error-color-rgb, 220, 53, 69), 0.08);
    border: 1px solid rgba(var(--error-color-rgb, 220, 53, 69), 0.25);
    color: var(--error-color, #dc3545);
    font-size: 0.85rem;
  }

  .cancelled-note {
    margin: 0.35rem 0 0;
    font-size: 0.8rem;
    font-style: italic;
    color: var(--text-secondary);
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 0.15rem;
    opacity: 0;
    transition: opacity 0.15s ease;
  }

  .chat-message:hover .actions,
  .actions:focus-within,
  .actions.always-visible {
    opacity: 1;
  }

  .action-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.3rem;
    min-width: 28px;
    height: 28px;
    padding: 0 0.4rem;
    background: none;
    border: none;
    border-radius: 6px;
    color: var(--text-secondary);
    font-size: 0.78rem;
    cursor: pointer;
  }

  .action-btn:hover {
    background-color: var(--button-hover);
    color: var(--text-color);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  @media (prefers-reduced-motion: reduce) {
    .actions {
      transition: none;
    }
  }

  /* Touch devices have no hover — keep actions permanently visible. */
  @media (hover: none) {
    .actions {
      opacity: 1;
    }
  }
</style>
