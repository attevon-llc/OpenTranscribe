<script context="module" lang="ts">
  let instance = 0;

  function nextId(prefix: string): string {
    instance += 1;
    return `${prefix}-${instance}`;
  }
</script>

<script lang="ts">
  import { resolve } from '$app/paths';
  /**
   * The tag manager's pane for one selected tag: rename (with the merge
   * warning a rename onto an existing name triggers), facts, the files that
   * carry it, and share / promote / delete.
   *
   * Every write control follows `tag.can_write`, the server's verdict for this
   * caller; promote follows `canPromote` plus ownership. Nothing is applied
   * here: the panel emits intent and the coordinator calls the API.
   */
  import { createEventDispatcher, tick } from 'svelte';
  import { t } from '$stores/locale';
  import Badge from '$components/ui/Badge.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { clampTagNameInput, normalizeTagName } from '$lib/utils/tagName';
  import type { TagImpact, TaggedFile } from '$lib/types/tag';
  import { tagOriginKey, type TagListEntry } from './TagList.svelte';

  export let tag: TagListEntry;
  export let busy: 'rename' | 'delete' | 'promote' | null = null;
  export let canPromote = false;
  export let files: TaggedFile[] = [];
  export let fileTotal = 0;
  export let filesLoading = false;
  export let previewLoading = false;
  export let deletePreview: TagImpact | null = null;
  /** Set when the last rename would merge into another tag; nothing has been applied. */
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

  const fieldId = nextId('tag-rename');
  const errorId = `${fieldId}-error`;

  $: canMutate = tag.can_write;
  $: canShare = tag.can_write && tag.ownership !== 'system';
  $: showPromote = canPromote && tag.ownership === 'mine';
  $: renaming = busy === 'rename';
  $: moreFiles = Math.max(0, fileTotal - files.length);

  let editing = false;
  let draft = '';
  let nameError: string | null = null;
  /** The name the last `rename` carried: what a merge warning describes. */
  let submittedName: string | null = null;
  let field: HTMLInputElement | undefined;

  // The coordinator reuses this component across single selections, so typing
  // for one tag must never survive into another and rename it.
  let trackedUuid: string | null = null;
  $: followTag(tag.uuid);

  $: if (!editing) draft = tag.name;

  // The modal closes itself on confirm and cancel, so its open flag is owned
  // here and re-derived from each new preview; a one-way prop would stay true
  // and never reopen after a Cancel.
  let deleteOpen: boolean;
  $: deleteOpen = deletePreview !== null;

  function followTag(uuid: string) {
    if (trackedUuid !== null && uuid !== trackedUuid) resetEditor();
    trackedUuid = uuid;
  }

  function resetEditor() {
    editing = false;
    draft = tag.name;
    nameError = null;
    submittedName = null;
  }

  async function startEditing() {
    editing = true;
    draft = tag.name;
    nameError = null;
    await tick();
    field?.focus();
    field?.select();
  }

  function cancelEditing() {
    const warningUp = renameMergeImpact !== null;
    resetEditor();
    if (warningUp) dispatch('cancelRenameMerge');
  }

  function handleInput(event: Event) {
    draft = clampTagNameInput(event);
    nameError = null;
    // The warning described the submitted name, not this new text.
    if (renameMergeImpact) dispatch('cancelRenameMerge');
  }

  function submitRename() {
    if (renaming || renameMergeImpact) return;
    const name = draft.trim();
    if (!name || name === tag.name) {
      resetEditor();
      return;
    }
    if (!normalizeTagName(name)) {
      nameError = $t('tags.nameRequired');
      return;
    }
    submittedName = name;
    dispatch('rename', { name });
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      submitRename();
    } else if (event.key === 'Escape') {
      // Escape here leaves the editor, not the dialog around the manager.
      event.preventDefault();
      event.stopPropagation();
      cancelEditing();
    }
  }

  function confirmMerge() {
    if (renaming || !submittedName) return;
    dispatch('confirmRenameMerge', { name: submittedName });
  }

  function handleConfirmDelete() {
    if (busy) return;
    dispatch('confirmDelete');
  }
</script>

<div class="tag-detail">
  <header class="detail-header">
    {#if editing}
      <div class="rename-editor">
        <label class="rename-label" for={fieldId}>{$t('tags.manager.detail.renameLabel')}</label>
        <div class="rename-row">
          <input
            bind:this={field}
            id={fieldId}
            type="text"
            value={draft}
            disabled={renaming}
            aria-invalid={nameError ? 'true' : undefined}
            aria-describedby={nameError ? errorId : undefined}
            on:input={handleInput}
            on:keydown={handleKeydown}
          />
          <button
            type="button"
            class="btn btn-primary"
            disabled={renaming || renameMergeImpact !== null}
            on:click={submitRename}
          >
            {renaming ? $t('tags.manager.detail.renaming') : $t('tags.manager.detail.rename')}
          </button>
          <button type="button" class="btn btn-secondary" on:click={cancelEditing}>
            {$t('tags.manager.action.cancel')}
          </button>
        </div>
        {#if nameError}
          <p id={errorId} class="field-error" role="alert">{nameError}</p>
        {/if}
      </div>
    {:else}
      <h2 class="tag-title">{tag.name}</h2>
      {#if canMutate}
        <button
          type="button"
          class="btn btn-secondary btn-sm"
          title={$t('tags.manager.detail.renameTooltip')}
          disabled={busy !== null}
          on:click={startEditing}
        >
          {$t('tags.manager.detail.rename')}
        </button>
      {/if}
    {/if}
  </header>

  {#if renameMergeImpact}
    <section class="merge-warning">
      <h3>{$t('tags.manager.detail.mergeOnRenameTitle')}</h3>
      <p>{$t('tags.manager.detail.mergeOnRenameDescription')}</p>
      <div class="impact-counts">
        <span>
          {$t('tags.manager.impact.accessible', {
            count: renameMergeImpact.accessible_file_count,
          })}
        </span>
        <span>
          {$t('tags.manager.impact.total', { count: renameMergeImpact.total_file_count })}
        </span>
      </div>
      <p class="impact-note">
        {$t('tags.manager.impact.totalNote', { count: renameMergeImpact.total_file_count })}
      </p>
      <div class="warning-actions">
        <button type="button" class="btn btn-danger" disabled={renaming} on:click={confirmMerge}>
          {renaming
            ? $t('tags.manager.action.merging')
            : $t('tags.manager.detail.confirmMergeRename')}
        </button>
        <button
          type="button"
          class="btn btn-secondary"
          on:click={() => dispatch('cancelRenameMerge')}
        >
          {$t('tags.manager.detail.keepEditing')}
        </button>
      </div>
    </section>
  {/if}

  <div class="facts">
    <div class="fact">
      <span class="fact-label">{$t('tags.manager.detail.usageLabel')}</span>
      <span class="fact-value">{$t('tags.manager.usage', { count: tag.usage_count })}</span>
    </div>
    <div class="fact">
      <span class="fact-label">{$t('tags.manager.detail.originLabel')}</span>
      <span class="fact-value">{$t(tagOriginKey(tag.source))}</span>
    </div>
  </div>

  {#if tag.ownership === 'system'}
    <p class="ownership-note">
      <Badge variant="info">{$t('tags.manager.sharedBadge')}</Badge>
      <span>{$t('tags.manager.detail.sharedHint')}</span>
    </p>
  {:else if tag.ownership === 'shared_with_me'}
    <p class="ownership-note">
      <Badge variant="default">{$t('tags.manager.sharedWithMeBadge')}</Badge>
      <span>{$t('tags.manager.detail.sharedWithMeHint')}</span>
    </p>
  {/if}

  <section class="files">
    <h3 class="section-title">{$t('tags.manager.detail.filesTitle')}</h3>
    {#if filesLoading}
      <div class="files-loading"><Spinner size="small" /></div>
    {:else if files.length === 0}
      <p class="muted">{$t('tags.manager.detail.noFiles')}</p>
    {:else}
      <ul class="touches-list">
        {#each files as file (file.uuid)}
          <li>
            <a class="touches-link" href={resolve('/files/[id]', { id: file.uuid })} title={file.display_title}>
              <span class="touches-title">{file.display_title}</span>
              {#if file.formatted_duration}
                <span class="touches-meta">{file.formatted_duration}</span>
              {/if}
            </a>
          </li>
        {/each}
      </ul>
      {#if moreFiles > 0}
        <p class="muted">{$t('tags.manager.detail.moreFiles', { count: moreFiles })}</p>
      {/if}
    {/if}
  </section>

  {#if previewLoading}
    <div class="preview-loading">
      <Spinner size="small" />
      <span>{$t('tags.manager.impact.loading')}</span>
    </div>
  {/if}

  <div class="actions">
    {#if canShare}
      <button
        type="button"
        class="btn btn-secondary"
        title={$t('tags.manager.share.buttonTooltip')}
        disabled={busy !== null}
        on:click={() => dispatch('share')}
      >
        {$t('tags.manager.share.button')}
      </button>
    {/if}
    {#if showPromote}
      <button
        type="button"
        class="btn btn-secondary"
        title={$t('tags.manager.promote.description')}
        disabled={busy !== null}
        on:click={() => dispatch('promote')}
      >
        {busy === 'promote'
          ? $t('tags.manager.action.promoting')
          : $t('tags.manager.action.promote')}
      </button>
    {/if}
    {#if canMutate}
      <button
        type="button"
        class="btn danger-outline"
        title={$t('tags.manager.detail.deleteTooltip')}
        disabled={busy !== null}
        on:click={() => dispatch('previewDelete')}
      >
        {$t('tags.manager.action.delete')}
      </button>
    {/if}
  </div>
</div>

<ConfirmationModal
  bind:isOpen={deleteOpen}
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
  variant="danger"
  on:confirm={handleConfirmDelete}
  on:cancel={() => dispatch('cancelDelete')}
  on:close={() => dispatch('cancelDelete')}
/>

<style>
  .tag-detail {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    color: var(--text-color);
  }

  .detail-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .tag-title {
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: var(--font-weight-semibold);
    overflow-wrap: anywhere;
  }

  .rename-editor {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    gap: var(--space-1);
  }

  .rename-label,
  .fact-label {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .rename-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .rename-row input {
    flex: 1 1 12rem;
    min-width: 0;
  }

  .field-error {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--error-text);
  }

  .merge-warning {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-3);
    border: 1px solid var(--warning-border);
    border-radius: var(--radius-lg);
    background-color: var(--warning-bg);
  }

  .merge-warning h3,
  .merge-warning p {
    margin: 0;
  }

  .merge-warning h3 {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--warning-text);
  }

  .impact-counts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
    font-weight: var(--font-weight-medium);
  }

  .impact-note,
  .muted {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .warning-actions,
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .facts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-6);
  }

  .fact {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
  }

  .fact-value {
    font-size: var(--font-size-md);
  }

  .ownership-note {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .files {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .section-title {
    margin: 0;
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-semibold);
    color: var(--text-secondary);
  }

  .files-loading,
  .preview-loading {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .touches-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .touches-link {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-1-5) var(--space-2);
    border-radius: var(--radius-md);
    color: var(--text-color);
    text-decoration: none;
  }

  .touches-link:hover {
    background-color: var(--hover-color);
  }

  .touches-link:focus-visible {
    outline: 2px solid var(--focus-ring-color);
    outline-offset: 1px;
  }

  .touches-title {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .touches-meta {
    flex-shrink: 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .danger-outline {
    border: 1px solid var(--error-border);
    background-color: transparent;
    color: var(--error-text);
  }

  .danger-outline:hover:not(:disabled) {
    background-color: rgba(var(--error-color-rgb), 0.08);
  }
</style>
