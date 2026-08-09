<!--
  ChatMessage.svelte — one turn in the thread.

  User messages render as PLAIN TEXT with `white-space: pre-wrap`, never through
  the markdown pipeline: there is no reason to interpret markup a user typed into
  their own prompt, and not doing so removes a whole class of self-XSS.
  Assistant messages go through ChatMarkdown's sanitized pipeline.
-->
<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte';
  import { t } from '$stores/locale';
  import CopyButton from '$components/ui/CopyButton.svelte';
  import ChatMarkdown from './ChatMarkdown.svelte';
  import ChatMessageMeta from './ChatMessageMeta.svelte';
  import ChatReasoning from './ChatReasoning.svelte';
  import ChatSources from './ChatSources.svelte';
  import type { ChatMessage } from '$lib/types/chat';

  export let message: ChatMessage;
  /** True for the newest assistant message (gets the regenerate affordance). */
  export let isLast = false;
  export let streaming = false;

  const dispatch = createEventDispatcher<{
    regenerate: void;
    retry: void;
    edit: { uuid: string; content: string };
  }>();

  let editing = false;
  let draft = '';
  let editArea: HTMLTextAreaElement;
  // Focus must return here when editing ends, or it falls to <body> and a
  // keyboard user loses their place in the thread.
  let editTrigger: HTMLButtonElement;

  async function startEdit(): Promise<void> {
    draft = message.content;
    editing = true;
    await tick();
    editArea?.focus();
    editArea?.select();
  }

  async function endEdit(): Promise<void> {
    editing = false;
    await tick();
    editTrigger?.focus();
  }

  async function submitEdit(): Promise<void> {
    const next = draft.trim();
    await endEdit();
    if (next && next !== message.content) {
      dispatch('edit', { uuid: message.uuid, content: next });
    }
  }

  function handleEditKey(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submitEdit();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      endEdit();
    }
  }

  /** Local time, shown on hover — absolute beats "2 hours ago" when citing. */
  function formatTimestamp(iso: string | null | undefined): string {
    if (!iso) return '';
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
  }

  $: isUser = message.role === 'user';
  $: hasError = message.status === 'error';
  $: wasCancelled = message.status === 'cancelled';
  $: sources = message.citations ?? [];
  // Retrieval found excerpts but none fit the context window, so this answer is
  // not grounded in the user's recordings (issue #384). Shown above the sources
  // block because it changes how the answer should be read.
  $: contextDropped = Boolean(message.msg_metadata?.context_dropped);
  $: timestamp = formatTimestamp(message.created_at);
  $: canEdit = isUser && !message.pending;
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
      {#if editing}
        <div class="edit-box">
          <textarea
            bind:this={editArea}
            bind:value={draft}
            on:keydown={handleEditKey}
            rows="3"
            aria-label={$t('chat.message.edit')}
            data-testid="chat-edit-input"
          ></textarea>
          <div class="edit-actions">
            <button type="button" class="edit-btn primary" on:click={submitEdit}>
              {$t('chat.message.saveAndResend')}
            </button>
            <button type="button" class="edit-btn" on:click={endEdit}>
              {$t('common.cancel')}
            </button>
          </div>
        </div>
      {:else}
        <p class="user-text">{message.content}</p>
      {/if}
    {:else}
      {#if message.reasoning_content}
        <ChatReasoning
          content={message.reasoning_content}
          streaming={Boolean(message.reasoningStreaming)}
          startedAt={message.reasoningStartedAt}
          durationMs={message.reasoningDurationMs}
        />
      {/if}

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

      {#if contextDropped}
        <p class="context-warning" data-testid="chat-context-dropped">
          {$t('chat.message.contextDropped')}
        </p>
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

      {#if canEdit && !editing}
        <button
          type="button"
          class="action-btn"
          bind:this={editTrigger}
          on:click={startEdit}
          title={$t('chat.message.edit')}
          aria-label={$t('chat.message.edit')}
          data-testid="chat-edit"
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
            <path d="M12 20h9" />
            <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
          </svg>
        </button>
      {/if}

      {#if timestamp}
        <span class="timestamp" title={timestamp}>{timestamp}</span>
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

  .context-warning {
    margin: 0.5rem 0 0;
    padding: 0.6rem 0.75rem;
    border-radius: 8px;
    /* Theme-provided pair — light/dark parity is handled in theme.css. */
    background-color: var(--warning-bg);
    border: 1px solid var(--warning-border);
    color: var(--text-color);
    font-size: 0.82rem;
    line-height: 1.45;
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

  .edit-box {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    min-width: min(32rem, 70vw);
  }

  .edit-box textarea {
    width: 100%;
    padding: 0.5rem 0.65rem;
    border: 1px solid var(--primary-color);
    border-radius: 10px;
    background-color: var(--card-background);
    color: var(--text-color);
    font-family: inherit;
    font-size: 0.95rem;
    line-height: 1.5;
    resize: vertical;
  }

  .edit-box textarea:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  .edit-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.35rem;
  }

  .edit-btn {
    padding: 0.25rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--card-background);
    color: var(--text-color);
    font-size: 0.78rem;
    cursor: pointer;
  }

  .edit-btn:hover {
    background-color: var(--button-hover);
  }

  .edit-btn.primary {
    border-color: var(--primary-color);
    color: var(--primary-color);
    font-weight: 500;
  }

  .timestamp {
    font-size: 0.7rem;
    color: var(--text-secondary);
    padding: 0 0.3rem;
    font-variant-numeric: tabular-nums;
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
