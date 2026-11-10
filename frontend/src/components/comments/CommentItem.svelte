<script lang="ts">
  import { domId } from '$lib/utils/domId';
  import { createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  import { t, locale } from '$stores/locale';
  import { formatClock, formatDate } from '$lib/utils/formatting';
  import TruncatedText from '$components/TruncatedText.svelte';
  import CommentTimeField from './CommentTimeField.svelte';
  import FrozenNote from '$components/legal-hold/FrozenNote.svelte';

  export let comment: {
    uuid: string;
    text: string;
    timestamp: number | null;
    created_at: string;
    user?: { uuid: string; email?: string; full_name?: string } | null;
  };
  /** Whether the viewer wrote this comment (only the author edits or deletes it). */
  export let isAuthor = false;
  /** The file is under a legal hold: an existing comment can be neither edited nor deleted. */
  export let frozen = false;
  /** Live playback position, used when an edit turns a general comment into a timed one. */
  export let currentTime = 0;
  /** Media length in seconds when known, so an edited time cannot point past the end. */
  export let duration: number | null = null;
  /** Saves an edit; resolves true when it was saved, so the editor only closes on success. */
  export let save: (payload: { text: string; timestamp: number | null }) => Promise<boolean>;

  const id = domId('comment-item');
  const frozenNoteId = `${id}-frozen`;
  const dispatch = createEventDispatcher<{ seekTo: { time: number }; delete: void }>();

  let editing = false;
  let draftText = '';
  let draftTimed = true;
  let draftTime = 0;
  let timeField: CommentTimeField;

  $: isGeneral = comment.timestamp === null || comment.timestamp === undefined;
  $: author = comment.user
    ? comment.user.full_name || comment.user.email || `User ${comment.user.uuid}`
    : $t('comments.anonymous');
  $: draftSummary = draftTimed
    ? $t('comments.kindTimedSummary', { time: formatClock(draftTime) })
    : $t('comments.kindGeneralSummary');

  function startEdit() {
    draftText = comment.text;
    draftTimed = !isGeneral;
    draftTime = isGeneral ? currentTime : (comment.timestamp as number);
    editing = true;
  }

  function onTimedChange() {
    // Turning a general comment into a timed one needs a time: take the player's, which the
    // user can see in the editor before saving.
    if (draftTimed && isGeneral) draftTime = currentTime;
  }

  async function onSave() {
    if (draftTimed && timeField && !timeField.validate()) return;
    const saved = await save({ text: draftText, timestamp: draftTimed ? draftTime : null });
    if (saved) editing = false;
  }
</script>

<div class="comment-item" class:general={isGeneral} transition:slide={{ duration: 300 }}>
  <div class="comment-header">
    <div class="comment-user">{author}</div>
    <div class="comment-meta">
      {#if isGeneral}
        <span class="general-label" title={$t('comments.generalLabelHint')}
          >{$t('comments.generalLabel')}</span
        >
      {:else}
        <button
          type="button"
          class="timestamp-link"
          on:click={() => dispatch('seekTo', { time: comment.timestamp as number })}
          aria-label={$t('comments.jumpToTimestamp', { time: formatClock(comment.timestamp) })}
          title={$t('comments.jumpToTime', { time: formatClock(comment.timestamp) })}
        >
          {formatClock(comment.timestamp)}
        </button>
      {/if}
      <span>{formatDate(comment.created_at, $locale)}</span>
    </div>
  </div>

  {#if editing}
    <form class="edit-comment-form" on:submit|preventDefault={onSave}>
      <textarea
        bind:value={draftText}
        rows="2"
        aria-label={$t('comments.editHint')}
        aria-describedby="{id}-summary"
      ></textarea>
      <div class="edit-row">
        <label class="timestamp-toggle">
          <input
            type="checkbox"
            bind:checked={draftTimed}
            on:change={onTimedChange}
            aria-describedby="{id}-summary"
          />
          <span>{$t('comments.timestampToggle')}</span>
        </label>
        {#if draftTimed}
          <CommentTimeField
            bind:this={timeField}
            value={draftTime}
            {currentTime}
            {duration}
            on:change={(e) => (draftTime = e.detail)}
          />
        {/if}
      </div>
      <p id="{id}-summary" class="kind-summary">{draftSummary}</p>
      <div class="edit-buttons">
        <button
          type="button"
          class="btn btn-secondary cancel-button"
          on:click={() => (editing = false)}
          title={$t('comments.cancelEditHint')}
        >
          {$t('comments.cancel')}
        </button>
        <button type="submit" class="btn btn-primary save-button" title={$t('comments.saveChangesHint')}>
          {$t('comments.save')}
        </button>
      </div>
    </form>
  {:else}
    <div class="comment-body">
      <TruncatedText text={comment.text} maxLength={150} />
    </div>
    {#if isAuthor}
      <div class="comment-actions">
        <button
          type="button"
          class="btn btn-link"
          data-testid="comment-edit"
          on:click={startEdit}
          disabled={frozen}
          aria-describedby={frozen ? frozenNoteId : undefined}
          title={frozen ? $t('holds.frozenControl') : $t('comments.editHint')}
        >
          {$t('comments.edit')}
        </button>
        <button
          type="button"
          class="btn btn-link btn-danger-text"
          data-testid="comment-delete"
          on:click={() => dispatch('delete')}
          disabled={frozen}
          aria-describedby={frozen ? frozenNoteId : undefined}
          title={frozen ? $t('holds.frozenControl') : $t('comments.deleteHint')}
        >
          {$t('comments.delete')}
        </button>
        {#if frozen}
          <FrozenNote id={frozenNoteId} />
        {/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  .comment-item {
    padding: var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background-color: var(--surface-color);
    inline-size: 100%;
    box-sizing: border-box;
  }

  .comment-item:hover {
    border-color: var(--border-color-hover);
  }

  .comment-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-2);
    margin-block-end: var(--space-2);
    font-size: var(--font-size-sm);
  }

  .comment-meta {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .timestamp-link,
  .general-label {
    display: inline-flex;
    align-items: center;
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    white-space: nowrap;
  }

  .timestamp-link {
    background: var(--primary-light);
    border: 1px solid var(--primary-border);
    box-shadow: none;
    color: var(--primary-on-surface);
    font-variant-numeric: tabular-nums;
    cursor: pointer;
  }

  .timestamp-link:hover {
    border-color: var(--primary-color);
    box-shadow: none;
  }

  .general-label {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    color: var(--text-on-tint);
  }

  .comment-body {
    font-size: var(--font-size-sm);
    line-height: var(--line-height-normal);
    margin-block-end: var(--space-2);
  }

  .comment-actions,
  .edit-buttons {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
  }

  .edit-comment-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    inline-size: 100%;
  }

  .edit-comment-form textarea {
    inline-size: 100%;
    box-sizing: border-box;
  }

  .edit-row {
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
    color: var(--text-primary);
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

  .comment-actions .btn {
    font-size: var(--font-size-xs);
  }

  .comment-actions .btn-danger-text {
    color: var(--error-text);
  }

  .comment-actions {
    flex-wrap: wrap;
    align-items: center;
  }
</style>
