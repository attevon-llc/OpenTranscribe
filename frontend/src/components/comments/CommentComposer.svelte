<script lang="ts">
  import { domId } from '$lib/utils/domId';
  import { t } from '$stores/locale';
  import { formatClock } from '$lib/utils/formatting';
  import CommentKindHelp from './CommentKindHelp.svelte';
  import CommentTimeField from './CommentTimeField.svelte';

  /** Live playback position from the player, in seconds. */
  export let currentTime = 0;
  /** Media length in seconds when known, so a typed time cannot point past the end. */
  export let duration: number | null = null;
  /**
   * Posts the comment; resolves true when it was saved, so the composer only clears on success
   * and a refused comment keeps the text the user typed.
   */
  export let submit: (payload: { text: string; timestamp: number | null }) => Promise<boolean>;

  const id = domId('comment-composer');

  let text = '';
  let timed = true;
  /**
   * The time the comment will carry. `null` follows the player; it is pinned the moment the
   * user starts typing, so the time is where they started writing, not where playback has
   * moved on to by the time they press Add (the Frame.io / Vimeo review behaviour).
   */
  let pinnedTime: number | null = null;
  let showEmptyError = false;
  let submitting = false;
  let textarea: HTMLTextAreaElement;
  let timeField: CommentTimeField;

  $: commentTime = pinnedTime ?? currentTime;
  $: summary = timed
    ? $t('comments.kindTimedSummary', { time: formatClock(commentTime) })
    : $t('comments.kindGeneralSummary');

  function onInput() {
    if (text.trim()) showEmptyError = false;
    if (text.length > 0) pin();
  }

  function pin() {
    if (pinnedTime === null) pinnedTime = currentTime;
  }

  async function onSubmit(event?: Event) {
    event?.preventDefault();
    if (submitting) return;
    if (!text.trim()) {
      showEmptyError = true;
      textarea?.focus();
      return;
    }
    if (timed && timeField && !timeField.validate()) return;
    submitting = true;
    try {
      const saved = await submit({ text, timestamp: timed ? commentTime : null });
      if (saved) {
        text = '';
        pinnedTime = null;
        timed = true;
      }
    } finally {
      submitting = false;
    }
  }

  function onKeydown(event: KeyboardEvent) {
    // Same convention as the chat composer: Enter sends, Shift+Enter is a new line, and an
    // IME composition's Enter (choosing a CJK candidate) is never a send.
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      void onSubmit(event);
    }
  }
</script>

<form class="comment-form" on:submit={onSubmit} novalidate>
  <div class="composer-head">
    <label class="composer-label" for="{id}-text">{$t('comments.newCommentLabel')}</label>
    <CommentKindHelp />
  </div>
  <textarea
    id="{id}-text"
    bind:this={textarea}
    bind:value={text}
    on:input={onInput}
    on:keydown={onKeydown}
    placeholder={$t('comments.placeholder')}
    rows="2"
    aria-invalid={showEmptyError}
    aria-describedby="{id}-summary {id}-keys{showEmptyError ? ` ${id}-error` : ''}"
  ></textarea>
  {#if showEmptyError}
    <p id="{id}-error" class="form-error" role="alert">{$t('comments.emptyTextError')}</p>
  {/if}

  <div class="form-actions">
    <div class="kind-controls">
      <label class="timestamp-toggle">
        <input type="checkbox" bind:checked={timed} aria-describedby="{id}-summary" />
        <span>{$t('comments.timestampToggle')}</span>
      </label>
      {#if timed}
        <CommentTimeField
          bind:this={timeField}
          value={commentTime}
          {currentTime}
          {duration}
          on:pin={pin}
          on:change={(e) => (pinnedTime = e.detail)}
        />
      {/if}
    </div>
    <button type="submit" class="btn btn-primary submit-button" aria-busy={submitting}>
      {$t('comments.addComment')}
    </button>
  </div>
  <p id="{id}-summary" class="kind-summary" class:general={!timed}>{summary}</p>
  <p id="{id}-keys" class="sr-only">{$t('comments.keyboardHint')}</p>
</form>

<style>
  .comment-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    inline-size: 100%;
  }

  .composer-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .composer-label {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  textarea {
    inline-size: 100%;
    box-sizing: border-box;
    resize: vertical;
    min-block-size: 70px;
  }

  .form-error {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--error-text);
  }

  .form-actions {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-3);
  }

  .kind-controls {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .timestamp-toggle {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-color);
    cursor: pointer;
  }

  .timestamp-toggle input {
    margin: 0;
    accent-color: var(--primary-color);
  }

  .kind-summary {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .kind-summary.general {
    color: var(--text-color);
  }
</style>
