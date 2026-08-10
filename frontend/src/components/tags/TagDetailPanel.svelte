<script lang="ts">
  /**
   * Everything one selected tag touches, plus the operations that change it.
   *
   * Presentational: it owns only its inline-edit field. Fetching the impact
   * previews and applying the mutations belongs to the route coordinator, which
   * feeds the results back in as props.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Badge from '$components/ui/Badge.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { tagOriginKey, type TagListEntry } from './TagList.svelte';
  import type { TagImpact, TagReviewResult } from '$lib/types/tag';

  export let tag: TagListEntry;
  /** Which mutation is in flight, if any. Disables the confirming control. */
  export let busy: 'rename' | 'delete' | 'accept' | 'reject' | 'promote' | null = null;
  /**
   * Whether the caller may publish this tag into the shared vocabulary.
   *
   * Cosmetic — `POST /tags/promote` enforces the admin check itself.
   */
  export let canPromote = false;
  /** An impact preview is being fetched. */
  export let previewLoading = false;
  /** Impact of deleting this tag, once previewed. */
  export let deletePreview: TagImpact | null = null;
  /** Impact of rejecting this tag, once previewed. */
  export let rejectPreview: TagReviewResult | null = null;
  /** Set when the submitted rename resolved to a different existing tag. */
  export let renameMergeImpact: TagImpact | null = null;

  const dispatch = createEventDispatcher<{
    rename: { name: string };
    confirmRenameMerge: { name: string };
    cancelRenameMerge: void;
    previewDelete: void;
    confirmDelete: void;
    cancelDelete: void;
    accept: void;
    promote: void;
    previewReject: void;
    confirmReject: void;
    cancelReject: void;
  }>();

  let editing = false;
  let editName = tag.name;

  // Mirrors GroupDetailPanel: while not editing, the field tracks the tag.
  $: if (!editing) editName = tag.name;

  function startEdit() {
    editName = tag.name;
    editing = true;
  }

  function cancelEdit() {
    editing = false;
    editName = tag.name;
    if (renameMergeImpact) dispatch('cancelRenameMerge');
  }

  function submitRename() {
    const name = editName.trim();
    if (!name || name === tag.name) {
      cancelEdit();
      return;
    }
    dispatch('rename', { name });
  }

  function confirmRenameMerge() {
    dispatch('confirmRenameMerge', { name: editName.trim() });
  }

  /**
   * Cancelling the merge returns to the editable field with the typed name
   * intact — a near miss must not cost the user their typing.
   */
  function cancelRenameMerge() {
    dispatch('cancelRenameMerge');
  }

  function onNameKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') submitRename();
    else if (event.key === 'Escape') cancelEdit();
  }
</script>

<div class="tag-detail">
  <header class="detail-head">
    {#if editing}
      <div class="edit-inline">
        <label class="edit-label" for="tag-rename-input">
          {$t('tags.manager.detail.renameLabel')}
        </label>
        <input
          id="tag-rename-input"
          type="text"
          class="edit-input"
          bind:value={editName}
          maxlength="255"
          disabled={busy === 'rename'}
          on:keydown={onNameKeydown}
        />
        <div class="edit-actions">
          <button
            type="button"
            class="btn btn-primary"
            on:click={submitRename}
            disabled={busy === 'rename' || renameMergeImpact !== null}
          >
            {busy === 'rename'
              ? $t('tags.manager.detail.renaming')
              : $t('tags.manager.detail.rename')}
          </button>
          <button type="button" class="btn btn-ghost" on:click={cancelEdit}>
            {$t('tags.manager.action.cancel')}
          </button>
        </div>
      </div>
    {:else}
      <h2 class="detail-name">{tag.name}</h2>
      <button type="button" class="btn btn-ghost" on:click={startEdit}>
        {$t('tags.manager.detail.rename')}
      </button>
    {/if}
  </header>

  {#if renameMergeImpact}
    <section class="panel panel-warning">
      <h3 class="panel-title">{$t('tags.manager.detail.mergeOnRenameTitle')}</h3>
      <p class="panel-text">{$t('tags.manager.detail.mergeOnRenameDescription')}</p>
      <ul class="impact-list">
        <li>
          {$t('tags.manager.impact.accessible', {
            count: renameMergeImpact.accessible_file_count,
          })}
        </li>
        <li class="impact-total">
          {$t('tags.manager.impact.total', { count: renameMergeImpact.total_file_count })}
        </li>
      </ul>
      <p class="panel-note">
        {$t('tags.manager.impact.totalNote', { count: renameMergeImpact.total_file_count })}
      </p>
      <div class="panel-actions">
        <button
          type="button"
          class="btn btn-danger"
          on:click={confirmRenameMerge}
          disabled={busy === 'rename'}
        >
          {busy === 'rename'
            ? $t('tags.manager.action.merging')
            : $t('tags.manager.detail.confirmMergeRename')}
        </button>
        <button type="button" class="btn btn-ghost" on:click={cancelRenameMerge}>
          {$t('tags.manager.detail.keepEditing')}
        </button>
      </div>
    </section>
  {/if}

  <dl class="detail-facts">
    <div class="fact">
      <dt>{$t('tags.manager.detail.usageLabel')}</dt>
      <dd>{$t('tags.manager.usage', { count: tag.usage_count })}</dd>
    </div>
    <div class="fact">
      <dt>{$t('tags.manager.detail.originLabel')}</dt>
      <dd>{$t(tagOriginKey(tag.source))}</dd>
    </div>
  </dl>

  {#if tag.awaiting_review}
    <div class="review-flag">
      <Badge variant="warning">{$t('tags.manager.awaitingReview')}</Badge>
      <span class="panel-note">{$t('tags.manager.detail.awaitingReviewHint')}</span>
    </div>
  {/if}

  {#if tag.is_shared}
    <div class="review-flag">
      <Badge variant="info">{$t('tags.manager.sharedBadge')}</Badge>
      <span class="panel-note">{$t('tags.manager.detail.sharedHint')}</span>
    </div>
  {/if}

  <div class="detail-actions">
    {#if canPromote && !tag.is_shared}
      <button
        type="button"
        class="btn btn-ghost"
        on:click={() => dispatch('promote')}
        disabled={busy !== null}
        title={$t('tags.manager.promote.description')}
      >
        {busy === 'promote'
          ? $t('tags.manager.action.promoting')
          : $t('tags.manager.action.promote')}
      </button>
    {/if}
    {#if tag.awaiting_review}
      <button
        type="button"
        class="btn btn-primary"
        on:click={() => dispatch('accept')}
        disabled={busy !== null}
      >
        {busy === 'accept'
          ? $t('tags.manager.action.accepting')
          : $t('tags.manager.action.accept')}
      </button>
      <button
        type="button"
        class="btn btn-ghost"
        on:click={() => dispatch('previewReject')}
        disabled={busy !== null}
      >
        {$t('tags.manager.action.reject')}
      </button>
    {/if}
    <button
      type="button"
      class="btn btn-danger-outline"
      on:click={() => dispatch('previewDelete')}
      disabled={busy !== null}
    >
      {$t('tags.manager.action.delete')}
    </button>
  </div>

  {#if previewLoading}
    <div class="preview-loading">
      <Spinner size="small" />
      <span>{$t('tags.manager.impact.loading')}</span>
    </div>
  {/if}

  {#if deletePreview}
    <section class="panel panel-danger">
      <h3 class="panel-title">{$t('tags.manager.impact.title')}</h3>
      <ul class="impact-list">
        <li>
          {$t('tags.manager.impact.accessible', { count: deletePreview.accessible_file_count })}
        </li>
        <li class="impact-total">
          {$t('tags.manager.impact.total', { count: deletePreview.total_file_count })}
        </li>
      </ul>
      <p class="panel-note">
        {$t('tags.manager.impact.totalNote', { count: deletePreview.total_file_count })}
      </p>
      <div class="panel-actions">
        <button
          type="button"
          class="btn btn-danger"
          on:click={() => dispatch('confirmDelete')}
          disabled={busy !== null}
        >
          {busy === 'delete'
            ? $t('tags.manager.action.deleting')
            : $t('tags.manager.action.delete')}
        </button>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={() => dispatch('cancelDelete')}
          disabled={busy !== null}
        >
          {$t('tags.manager.action.cancel')}
        </button>
      </div>
    </section>
  {/if}

  {#if rejectPreview}
    <section class="panel panel-warning">
      <h3 class="panel-title">{$t('tags.manager.impact.title')}</h3>
      <ul class="impact-list">
        <li>
          {$t('tags.manager.impact.removed', {
            count: rejectPreview.removed_association_count,
          })}
        </li>
        <li>
          {$t('tags.manager.impact.retained', {
            count: rejectPreview.retained_association_count,
          })}
        </li>
        <li class="impact-total">
          {$t('tags.manager.impact.total', { count: rejectPreview.impact.total_file_count })}
        </li>
      </ul>
      <p class="panel-note">{$t('tags.manager.impact.retainedNote')}</p>
      <div class="panel-actions">
        <button
          type="button"
          class="btn btn-danger"
          on:click={() => dispatch('confirmReject')}
          disabled={busy !== null}
        >
          {busy === 'reject'
            ? $t('tags.manager.action.rejecting')
            : $t('tags.manager.action.reject')}
        </button>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={() => dispatch('cancelReject')}
          disabled={busy !== null}
        >
          {$t('tags.manager.action.cancel')}
        </button>
      </div>
    </section>
  {/if}
</div>

<style>
  .tag-detail {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .detail-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  .detail-name {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
    color: var(--text-color);
    word-break: break-word;
  }

  .edit-inline {
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: 100%;
  }

  .edit-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .edit-input {
    width: 100%;
    padding: 8px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 15px;
    box-sizing: border-box;
  }

  .edit-input:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  .edit-actions,
  .panel-actions,
  .detail-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .detail-facts {
    display: flex;
    flex-wrap: wrap;
    gap: 20px;
    margin: 0;
  }

  .fact dt {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--text-secondary);
  }

  .fact dd {
    margin: 2px 0 0;
    font-size: 14px;
    color: var(--text-color);
  }

  .review-flag {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .panel {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 14px;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background: var(--surface-color);
  }

  .panel-danger {
    border-color: var(--error-color, #dc2626);
    background: rgba(var(--error-color-rgb, 239, 68, 68), 0.06);
  }

  .panel-warning {
    border-color: var(--warning-color, #d97706);
    background: rgba(var(--warning-color-rgb, 245, 158, 11), 0.08);
  }

  .panel-title {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-color);
  }

  .panel-text {
    margin: 0;
    font-size: 13px;
    color: var(--text-color);
  }

  .panel-note {
    margin: 0;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .impact-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
    color: var(--text-color);
  }

  .impact-total {
    font-weight: 600;
  }

  .preview-loading {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .btn {
    padding: 7px 14px;
    border-radius: 8px;
    border: 1px solid transparent;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    box-shadow: none;
  }

  .btn:hover:not(:disabled) {
    transform: none;
    box-shadow: none;
  }

  .btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-primary {
    background: var(--primary-color);
    color: #fff;
  }

  .btn-ghost {
    background: transparent;
    border-color: var(--border-color);
    color: var(--text-color);
  }

  .btn-ghost:hover:not(:disabled) {
    background: var(--button-hover);
  }

  .btn-danger {
    background: var(--error-color, #dc2626);
    color: #fff;
  }

  .btn-danger-outline {
    background: transparent;
    border-color: var(--error-color, #dc2626);
    color: var(--error-color, #dc2626);
  }
</style>
