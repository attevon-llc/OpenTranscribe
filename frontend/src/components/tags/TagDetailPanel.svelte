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
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { tagOriginKey, type TagListEntry } from './TagList.svelte';
  import type { TagImpact } from '$lib/types/tag';
  import { canMutateTag } from '$lib/types/tag';
  import type { TaggedFile } from '$lib/types/tag';

  export let tag: TagListEntry;
  /** Which mutation is in flight, if any. Disables the confirming control. */
  export let busy: 'rename' | 'delete' | 'promote' | null = null;
  /**
   * Whether the caller may publish this tag into the shared vocabulary.
   *
   * Cosmetic — `POST /tags/promote` enforces the admin check itself.
   */
  export let canPromote = false;
  /** Whether the caller is an admin — decides rights over a system tag. */
  export let isAdmin = false;

  /**
   * Whether to offer the destructive controls at all.
   *
   * Mirrors `endpoints/tags.py:_writable_tag_ids`. A `shared_with_me` tag is
   * refused with a 404, so rendering Rename/Delete for it would offer an action
   * that can only fail — the badge and hint explain the absence instead.
   */
  $: canMutate = canMutateTag(tag, isAdmin);

  /**
   * The files this tag is on. The pane's whole promise — the empty state says
   * "see what it touches" — and previously it showed a count and nothing else.
   */
  export let files: TaggedFile[] = [];
  export let fileTotal = 0;
  export let filesLoading = false;
  $: hiddenFileCount = Math.max(0, fileTotal - files.length);
  /** An impact preview is being fetched. */
  export let previewLoading = false;
  /** Impact of deleting this tag, once previewed. */
  export let deletePreview: TagImpact | null = null;
  /** Set when the submitted rename resolved to a different existing tag. */
  export let renameMergeImpact: TagImpact | null = null;

  const dispatch = createEventDispatcher<{
    rename: { name: string };
    confirmRenameMerge: { name: string };
    cancelRenameMerge: void;
    previewDelete: void;
    confirmDelete: void;
    cancelDelete: void;
    promote: void;
    share: void;
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
      {#if canMutate}
        <button
          type="button"
          class="btn btn-ghost"
          on:click={startEdit}
          title={$t('tags.manager.detail.renameTooltip')}
        >
          {$t('tags.manager.detail.rename')}
        </button>
      {/if}
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


  {#if tag.ownership === 'system'}
    <div class="review-flag">
      <Badge variant="info">{$t('tags.manager.sharedBadge')}</Badge>
      <span class="panel-note">{$t('tags.manager.detail.sharedHint')}</span>
    </div>
  {:else if tag.ownership === 'shared_with_me'}
    <div class="review-flag">
      <Badge variant="default">{$t('tags.manager.sharedWithMeBadge')}</Badge>
      <span class="panel-note">{$t('tags.manager.detail.sharedWithMeHint')}</span>
    </div>
  {/if}

  <section class="touches">
    <h3 class="touches-title">{$t('tags.manager.detail.filesTitle')}</h3>
    {#if filesLoading}
      <div class="touches-loading"><Spinner size="small" /></div>
    {:else if files.length === 0}
      <p class="panel-note">{$t('tags.manager.detail.noFiles')}</p>
    {:else}
      <ul class="touches-list">
        {#each files as file (file.uuid)}
          <li>
            <a class="touches-link" href={`/files/${file.uuid}`} title={file.display_title}>
              <span class="touches-name">{file.display_title}</span>
              {#if file.formatted_duration}
                <span class="touches-meta">{file.formatted_duration}</span>
              {/if}
            </a>
          </li>
        {/each}
      </ul>
      {#if hiddenFileCount > 0}
        <p class="panel-note">
          {$t('tags.manager.detail.moreFiles', { count: hiddenFileCount })}
        </p>
      {/if}
    {/if}
  </section>

  <div class="detail-actions">
    {#if tag.ownership === 'mine'}
      <button
        type="button"
        class="btn btn-ghost"
        on:click={() => dispatch('share')}
        disabled={busy !== null}
        title={$t('tags.manager.share.buttonTooltip')}
      >
        {$t('tags.manager.share.button')}
      </button>
    {/if}
    {#if canPromote && tag.ownership === 'mine'}
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
    {#if canMutate}
      <button
        type="button"
        class="btn btn-danger-outline"
        on:click={() => dispatch('previewDelete')}
        disabled={busy !== null}
        title={$t('tags.manager.detail.deleteTooltip')}
      >
        {$t('tags.manager.action.delete')}
      </button>
    {/if}
  </div>

  {#if previewLoading}
    <div class="preview-loading">
      <Spinner size="small" />
      <span>{$t('tags.manager.impact.loading')}</span>
    </div>
  {/if}

  <!-- Deletes go through the app's shared confirmation dialog, the same one
       CollectionsPanel uses, so a destructive tag action looks and behaves
       like every other destructive action in the app. The impact numbers ride
       in the message: an accessible-only count would understate a shared
       tag's blast radius. -->
  <ConfirmationModal
    isOpen={deletePreview !== null}
    title={$t('tags.manager.impact.title')}
    message={deletePreview
      ? $t('tags.manager.impact.deleteMessage', {
          name: tag.name,
          accessible: deletePreview.accessible_file_count,
          total: deletePreview.total_file_count,
        })
      : ''}
    confirmText={busy === 'delete'
      ? $t('tags.manager.action.deleting')
      : $t('tags.manager.action.delete')}
    cancelText={$t('tags.manager.action.cancel')}
    confirmButtonClass="modal-delete-button"
    cancelButtonClass="modal-cancel-button"
    on:confirm={() => dispatch('confirmDelete')}
    on:cancel={() => dispatch('cancelDelete')}
  />

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
  .touches {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  .touches-title {
    margin: 0;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-secondary);
  }

  .touches-list {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .touches-link {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
    padding: 4px 6px;
    border-radius: 4px;
    color: var(--text-color);
    text-decoration: none;
    font-size: 13px;
  }

  .touches-link:hover {
    background: var(--hover-color, rgba(127, 127, 127, 0.12));
  }

  .touches-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .touches-meta {
    flex-shrink: 0;
    font-size: 11px;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .touches-loading {
    padding: 4px 0;
  }

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
