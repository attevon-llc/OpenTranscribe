<!--
  ConversationListItem.svelte — one row in the history sidebar.

  Rename is inline (Enter commits, Escape reverts) rather than a modal, and
  delete asks for confirmation in place: a conversation is unrecoverable once
  deleted, but interrupting the user with a dialog for every rename would be
  disproportionate.
-->
<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte';
  import { t } from '$stores/locale';
  import type { ConversationSummary } from '$lib/types/chat';

  export let conversation: ConversationSummary;
  export let active = false;
  /** Viewing the archived list — the archive action becomes restore. */
  export let showArchived = false;

  const dispatch = createEventDispatcher<{
    select: string;
    rename: { uuid: string; title: string };
    delete: string;
    archive: string;
  }>();

  let editing = false;
  let confirmingDelete = false;
  let draftTitle = '';
  let input: HTMLInputElement;
  let renameTrigger: HTMLButtonElement;

  $: displayTitle = conversation.title || $t('chat.conversation.untitled');

  async function startRename(): Promise<void> {
    draftTitle = conversation.title ?? '';
    editing = true;
    await tick();
    input?.focus();
    input?.select();
  }

  async function endRename(): Promise<void> {
    editing = false;
    await tick();
    renameTrigger?.focus();
  }

  async function commitRename(): Promise<void> {
    const next = draftTitle.trim();
    await endRename();
    if (next && next !== conversation.title) {
      dispatch('rename', { uuid: conversation.uuid, title: next });
    }
  }

  function handleRenameKey(event: KeyboardEvent): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitRename();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      endRename();
    }
  }
</script>

<li class="conversation-item" class:active data-testid="chat-conversation-item">
  {#if editing}
    <input
      class="rename-input"
      bind:this={input}
      bind:value={draftTitle}
      on:keydown={handleRenameKey}
      on:blur={commitRename}
      aria-label={$t('chat.conversation.rename')}
      data-testid="chat-rename-input"
    />
  {:else if confirmingDelete}
    <div class="confirm-row">
      <span class="confirm-text">{$t('chat.conversation.deleteConfirmMessage')}</span>
      <div class="confirm-actions">
        <button
          type="button"
          class="confirm-btn danger"
          on:click={() => dispatch('delete', conversation.uuid)}
          data-testid="chat-delete-confirm"
        >
          {$t('common.delete')}
        </button>
        <button type="button" class="confirm-btn" on:click={() => (confirmingDelete = false)}>
          {$t('common.cancel')}
        </button>
      </div>
    </div>
  {:else}
    <button
      type="button"
      class="item-button"
      on:click={() => dispatch('select', conversation.uuid)}
      title={displayTitle}
      data-testid="chat-conversation-select"
    >
      <span class="item-title">{displayTitle}</span>
    </button>

    <div class="item-actions">
      <button
        type="button"
        class="icon-btn"
        bind:this={renameTrigger}
        on:click|stopPropagation={startRename}
        title={$t('chat.conversation.rename')}
        aria-label={$t('chat.conversation.rename')}
        data-testid="chat-rename"
      >
        <svg
          width="13"
          height="13"
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
      <button
        type="button"
        class="icon-btn"
        on:click|stopPropagation={() => dispatch('archive', conversation.uuid)}
        title={showArchived
          ? $t('chat.conversation.restore')
          : $t('chat.conversation.archive')}
        aria-label={showArchived
          ? $t('chat.conversation.restore')
          : $t('chat.conversation.archive')}
        data-testid="chat-archive"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          aria-hidden="true"
        >
          <polyline points="21 8 21 21 3 21 3 8" />
          <rect x="1" y="3" width="22" height="5" />
          <line x1="10" y1="12" x2="14" y2="12" />
        </svg>
      </button>
      <button
        type="button"
        class="icon-btn danger"
        on:click|stopPropagation={() => (confirmingDelete = true)}
        title={$t('chat.conversation.delete')}
        aria-label={$t('chat.conversation.delete')}
        data-testid="chat-delete"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          aria-hidden="true"
        >
          <polyline points="3 6 5 6 21 6" />
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
        </svg>
      </button>
    </div>
  {/if}
</li>

<style>
  .conversation-item {
    display: flex;
    align-items: center;
    gap: 0.15rem;
    border-radius: 8px;
    padding: 0 0.2rem;
    position: relative;
  }

  .conversation-item:hover {
    background-color: var(--button-hover);
  }

  .conversation-item.active {
    background-color: rgba(var(--primary-color-rgb), 0.12);
  }

  .conversation-item.active::before {
    content: '';
    position: absolute;
    left: 0;
    top: 20%;
    bottom: 20%;
    width: 3px;
    border-radius: 0 2px 2px 0;
    background-color: var(--primary-color);
  }

  .item-button {
    flex: 1;
    min-width: 0;
    text-align: left;
    background: none;
    border: none;
    /* form-elements.css's bare `button` rule also sets border-radius and a
       drop-shadow; unset border/background alone still leaves those two
       painting a rounded card behind the title in light mode. */
    border-radius: 0;
    box-shadow: none;
    padding: 0.5rem 0.35rem;
    color: var(--text-color);
    font-size: 0.85rem;
    cursor: pointer;
  }

  /* The ROW (.conversation-item) owns the hover/selected highlight. Without this
     reset, form-elements.css's `button:hover:not(:disabled)` — specificity
     (0,2,1), which outranks the scoped .item-button rule — paints --button-hover
     across only the title's width. Against the selected row's tint that reads as
     two different colours meeting mid-row, worst in dark mode where
     --button-hover is rgba(255,255,255,0.1) over a blue tint. */
  .item-button:hover,
  .item-button:focus {
    background-color: transparent;
    transform: none;
    box-shadow: none;
  }

  .item-title {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .item-actions {
    display: flex;
    gap: 0.1rem;
    opacity: 0;
    transition: opacity 0.15s ease;
  }

  .conversation-item:hover .item-actions,
  .item-actions:focus-within {
    opacity: 1;
  }

  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    /* form-elements.css styles every bare <button>. Its padding (0.6rem 1.2rem)
       and box-shadow survive unless reset here: with box-sizing: border-box the
       padding alone exceeds the 24px width, so the button stretches to ~38px and
       squeezes the icon to zero, leaving a shadow-only rectangle. */
    padding: 0;
    box-shadow: none;
    border: none;
    border-radius: 5px;
    background: none;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .icon-btn:hover {
    /* Translucent, not an opaque surface colour — these sit on top of the
       selected row's tint, and an opaque swatch reads as a second colour.
       transform/box-shadow are reset for the same reason as .item-button. */
    background-color: rgba(var(--primary-color-rgb), 0.18);
    color: var(--text-color);
    transform: none;
    box-shadow: none;
  }

  .icon-btn.danger:hover {
    color: var(--error-color, #dc3545);
  }

  .rename-input {
    width: 100%;
    padding: 0.4rem 0.5rem;
    border: 1px solid var(--primary-color);
    border-radius: 6px;
    background-color: var(--card-background);
    color: var(--text-color);
    font-size: 0.85rem;
  }

  .rename-input:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  .confirm-row {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    padding: 0.5rem 0.35rem;
    width: 100%;
  }

  .confirm-text {
    font-size: 0.78rem;
    color: var(--text-secondary);
  }

  .confirm-actions {
    display: flex;
    gap: 0.35rem;
  }

  .confirm-btn {
    padding: 0.2rem 0.55rem;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    background-color: var(--card-background);
    color: var(--text-color);
    font-size: 0.75rem;
    cursor: pointer;
  }

  .confirm-btn:hover {
    background-color: var(--button-hover);
  }

  .confirm-btn.danger {
    border-color: rgba(var(--error-color-rgb, 220, 53, 69), 0.5);
    color: var(--error-color, #dc3545);
  }

  @media (hover: none) {
    .item-actions {
      opacity: 1;
    }
  }
</style>
