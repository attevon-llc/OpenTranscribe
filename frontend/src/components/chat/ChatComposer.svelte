<!--
  ChatComposer.svelte — the message input.

  Follows the ChatGPT conventions users already have muscle memory for: Enter
  sends, Shift+Enter adds a newline, the textarea auto-grows to a cap, and the
  circular send button morphs into a square Stop while a reply is streaming
  (rather than sitting disabled, which would strand the user mid-generation).
-->
<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte';
  import { t } from '$stores/locale';
  import type { StreamStatus } from '$lib/types/chat';

  export let value = '';
  export let status: StreamStatus = 'idle';
  /** No LLM configured — the composer is inert and explains why. */
  export let disabled = false;
  export let maxLength = 8000;

  const MIN_ROWS = 1;
  const MAX_ROWS = 8;
  const LINE_HEIGHT_PX = 24;

  const dispatch = createEventDispatcher<{ send: string; stop: void }>();

  let textarea: HTMLTextAreaElement;

  $: isStreaming = status === 'submitting' || status === 'retrieving' || status === 'thinking' || status === 'streaming';
  $: tooLong = value.length > maxLength;
  $: canSend = value.trim().length > 0 && !tooLong && !disabled && !isStreaming;
  $: showCounter = value.length > maxLength * 0.9;

  async function autoGrow(): Promise<void> {
    if (!textarea) return;
    await tick();
    textarea.style.height = 'auto';
    const maxHeight = MAX_ROWS * LINE_HEIGHT_PX + 16;
    textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? 'auto' : 'hidden';
  }

  function submit(): void {
    if (!canSend) return;
    dispatch('send', value.trim());
    value = '';
    autoGrow();
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  function handleButton(): void {
    if (isStreaming) {
      dispatch('stop');
    } else {
      submit();
    }
  }

  export function focus(): void {
    textarea?.focus();
  }

  $: if (value !== undefined) autoGrow();
</script>

<div class="composer" class:disabled>
  <div class="input-shell" class:too-long={tooLong}>
    <textarea
      bind:this={textarea}
      bind:value
      on:keydown={handleKeydown}
      rows={MIN_ROWS}
      placeholder={disabled ? $t('chat.setup.noLlmMessage') : $t('chat.composer.placeholder')}
      aria-label={$t('chat.composer.placeholder')}
      {disabled}
      data-testid="chat-composer-input"
    ></textarea>

    <button
      type="button"
      class="send-button"
      class:stop={isStreaming}
      on:click={handleButton}
      disabled={!isStreaming && !canSend}
      aria-label={isStreaming ? $t('chat.composer.stop') : $t('chat.composer.send')}
      title={isStreaming ? $t('chat.composer.stop') : $t('chat.composer.send')}
      data-testid={isStreaming ? 'chat-stop' : 'chat-send'}
    >
      {#if isStreaming}
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <rect x="5" y="5" width="14" height="14" rx="2" />
        </svg>
      {:else}
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <line x1="12" y1="19" x2="12" y2="5" />
          <polyline points="5 12 12 5 19 12" />
        </svg>
      {/if}
    </button>
  </div>

  <div class="composer-footer">
    {#if tooLong}
      <span class="counter over" data-testid="chat-char-counter">
        {$t('chat.composer.tooLong', { max: maxLength.toLocaleString() })}
      </span>
    {:else if showCounter}
      <span class="counter" data-testid="chat-char-counter">
        {$t('chat.composer.charCount', {
          count: value.length.toLocaleString(),
          max: maxLength.toLocaleString(),
        })}
      </span>
    {/if}
  </div>

  <!--
    Standard AI disclaimer. Deliberately OUTSIDE .composer-footer (a
    space-between row for the character counter) so it stays centred under the
    input at every width, and it is shown on mobile too — the smaller the
    screen, the less other context the reader has.
  -->
  <p class="ai-disclaimer">{$t('chat.composer.disclaimer')}</p>
</div>

<style>
  .composer {
    padding: 0.75rem 0 0;
  }

  .input-shell {
    display: flex;
    align-items: flex-end;
    gap: 0.5rem;
    padding: 0.5rem 0.5rem 0.5rem 0.9rem;
    border: 1px solid var(--border-color);
    border-radius: 24px;
    background-color: var(--surface-color);
    transition:
      border-color 0.15s ease,
      box-shadow 0.15s ease;
  }

  .input-shell:focus-within {
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(var(--primary-color-rgb), 0.15);
  }

  .input-shell.too-long {
    border-color: var(--error-color, #dc3545);
  }

  textarea {
    flex: 1;
    border: none;
    background: transparent;
    resize: none;
    outline: none;
    color: var(--text-color);
    font-family: inherit;
    font-size: 0.95rem;
    line-height: 1.5;
    padding: 0.35rem 0;
    max-height: 208px;
    overflow-y: hidden;
  }

  textarea::placeholder {
    color: var(--text-secondary);
  }

  textarea:disabled {
    cursor: not-allowed;
  }

  .send-button {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 34px;
    border: none;
    border-radius: 50%;
    background-color: var(--primary-color);
    color: #fff;
    cursor: pointer;
    transition:
      background-color 0.15s ease,
      opacity 0.15s ease;
  }

  .send-button:disabled {
    background-color: var(--border-color);
    color: var(--text-secondary);
    cursor: not-allowed;
  }

  .send-button:not(:disabled):hover {
    filter: brightness(1.1);
  }

  .send-button.stop {
    background-color: var(--text-color);
    color: var(--background-color);
  }

  .composer-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    padding: 0.4rem 0.75rem 0;
    min-height: 1.1rem;
  }

  .counter {
    font-size: 0.72rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .counter.over {
    color: var(--error-color, #dc3545);
    font-weight: 600;
  }

  .ai-disclaimer {
    margin: 0.15rem 0 0;
    text-align: center;
    font-size: 0.72rem;
    color: var(--text-secondary);
  }

</style>
