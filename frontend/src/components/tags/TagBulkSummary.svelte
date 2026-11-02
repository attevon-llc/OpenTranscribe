<script context="module" lang="ts">
  let instance = 0;

  function nextId(prefix: string): string {
    instance += 1;
    return `${prefix}-${instance}`;
  }
</script>

<script lang="ts">
  /**
   * The tag manager's pane for two or more selected tags: pick a survivor and
   * merge, promote, or delete.
   *
   * The server rejects the whole request at the first tag the caller may not
   * write, so each action is offered only when the entire selection allows it.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import type { TagImpact } from '$lib/types/tag';
  import { tagOriginKey, type TagListEntry } from './TagList.svelte';

  /** The selected tags, in list order. */
  export let tags: TagListEntry[] = [];
  /** The backend's pick for the survivor of a cluster, when the selection came from one. */
  export let suggestedSurvivorUuid: string | null = null;
  export let busy: 'merge' | 'delete' | 'promote' | null = null;
  export let canPromote = false;
  export let previewLoading = false;
  export let mergePreview: TagImpact | null = null;
  export let deletePreview: TagImpact | null = null;

  const dispatch = createEventDispatcher<{
    previewMerge: { survivorUuid: string };
    confirmMerge: { survivorUuid: string };
    cancelMerge: void;
    previewDelete: void;
    confirmDelete: void;
    cancelDelete: void;
    promote: void;
    clear: void;
  }>();

  const radioName = nextId('tag-survivor');

  $: allWritable = tags.length > 0 && tags.every((tag) => tag.can_write);
  $: allPromotable =
    canPromote && tags.length > 0 && tags.every((tag) => (tag.ownership ?? 'mine') === 'mine');
  $: locked = busy !== null;

  $: ordered = [...tags].sort(
    (a, b) => b.usage_count - a.usage_count || a.name.localeCompare(b.name)
  );

  let survivorUuid: string | null = null;

  // Preselect only when the set of tags changes, so a refresh that merely
  // updates the counts never overrides the user's explicit pick.
  let selectionKey: string | null = null;
  $: syncSurvivor(
    tags
      .map((tag) => tag.uuid)
      .sort()
      .join('|'),
    ordered
  );

  $: survivor = tags.find((tag) => tag.uuid === survivorUuid) ?? null;

  function syncSurvivor(key: string, candidates: TagListEntry[]) {
    if (key === selectionKey) return;
    selectionKey = key;
    survivorUuid = pickDefaultSurvivor(candidates, suggestedSurvivorUuid);
  }

  function pickDefaultSurvivor(candidates: TagListEntry[], suggested: string | null) {
    if (suggested && candidates.some((tag) => tag.uuid === suggested)) return suggested;
    return candidates[0]?.uuid ?? null;
  }

  // Owned here for the same reason as in TagDetailPanel: the modal closes
  // itself, and each new preview must be able to open it again.
  let deleteOpen: boolean;
  $: deleteOpen = deletePreview !== null;

  function previewMerge() {
    if (survivorUuid) dispatch('previewMerge', { survivorUuid });
  }

  function confirmMerge() {
    if (survivorUuid && !locked) dispatch('confirmMerge', { survivorUuid });
  }

  function handleConfirmDelete() {
    if (!locked) dispatch('confirmDelete');
  }
</script>

<div class="tag-bulk">
  <header class="bulk-header">
    <h2>{$t('tags.manager.bulk.title', { count: tags.length })}</h2>
    <button
      type="button"
      class="btn btn-secondary btn-small"
      disabled={locked}
      on:click={() => dispatch('clear')}
    >
      {$t('tags.manager.action.clearSelection')}
    </button>
  </header>

  {#if allWritable}
    <fieldset class="survivor-picker">
      <legend>{$t('tags.manager.bulk.survivorLegend')}</legend>
      <p class="hint">{$t('tags.manager.bulk.survivorHint')}</p>
      {#each ordered as tag (tag.uuid)}
        <label class="survivor-option" class:is-chosen={tag.uuid === survivorUuid}>
          <input
            type="radio"
            name={radioName}
            value={tag.uuid}
            bind:group={survivorUuid}
            disabled={locked}
          />
          <span class="option-body">
            <span class="option-name">{tag.name}</span>
            <span class="option-meta">
              <span>{$t('tags.manager.usage', { count: tag.usage_count })}</span>
              <span aria-hidden="true">·</span>
              <span>{$t(tagOriginKey(tag.source))}</span>
            </span>
          </span>
        </label>
      {/each}
    </fieldset>
  {/if}

  <div class="actions">
    {#if allWritable}
      <button
        type="button"
        class="btn btn-primary"
        disabled={locked || tags.length < 2 || !survivorUuid}
        on:click={previewMerge}
      >
        {$t('tags.manager.action.merge')}
      </button>
    {/if}
    {#if allPromotable}
      <button
        type="button"
        class="btn btn-secondary"
        title={$t('tags.manager.promote.description')}
        disabled={locked}
        on:click={() => dispatch('promote')}
      >
        {busy === 'promote'
          ? $t('tags.manager.action.promoting')
          : $t('tags.manager.action.promote')}
      </button>
    {/if}
    {#if allWritable}
      <button
        type="button"
        class="btn danger-outline"
        disabled={locked}
        on:click={() => dispatch('previewDelete')}
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

  {#if mergePreview}
    <section class="merge-preview">
      <h3>{$t('tags.manager.impact.title')}</h3>
      {#if survivor}
        <p>{$t('tags.manager.bulk.mergeInto', { name: survivor.name })}</p>
      {/if}
      <div class="impact-counts">
        <span>
          {$t('tags.manager.impact.accessible', { count: mergePreview.accessible_file_count })}
        </span>
        <span>{$t('tags.manager.impact.total', { count: mergePreview.total_file_count })}</span>
      </div>
      <p class="hint">
        {$t('tags.manager.impact.totalNote', { count: mergePreview.total_file_count })}
      </p>
      <div class="actions">
        <button
          type="button"
          class="btn btn-danger"
          disabled={locked || !survivorUuid}
          on:click={confirmMerge}
        >
          {busy === 'merge' ? $t('tags.manager.action.merging') : $t('tags.manager.action.merge')}
        </button>
        <button
          type="button"
          class="btn btn-secondary"
          disabled={locked}
          on:click={() => dispatch('cancelMerge')}
        >
          {$t('tags.manager.action.cancel')}
        </button>
      </div>
    </section>
  {/if}
</div>

<ConfirmationModal
  bind:isOpen={deleteOpen}
  title={$t('tags.manager.impact.title')}
  message={deletePreview
    ? $t('tags.manager.impact.deleteMessage', {
        name: tags.map((tag) => tag.name).join(', '),
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
  on:confirm={handleConfirmDelete}
  on:cancel={() => dispatch('cancelDelete')}
  on:close={() => dispatch('cancelDelete')}
/>

<style>
  .tag-bulk {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    color: var(--text-color);
  }

  .bulk-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .bulk-header h2 {
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: var(--font-weight-semibold);
  }

  .survivor-picker {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
    margin: 0;
    padding: var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
  }

  .survivor-picker legend {
    padding-inline: var(--space-1);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-semibold);
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .survivor-option {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    padding: var(--space-2);
    border: 1px solid transparent;
    border-radius: var(--radius-md);
    cursor: pointer;
  }

  .survivor-option:hover {
    background-color: var(--hover-color);
  }

  .survivor-option.is-chosen {
    border-color: rgba(var(--primary-color-rgb), 0.4);
    background-color: rgba(var(--primary-color-rgb), 0.08);
  }

  .option-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    min-width: 0;
  }

  .option-name {
    font-weight: var(--font-weight-medium);
    overflow-wrap: anywhere;
  }

  .option-meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .preview-loading {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .merge-preview {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-3);
    border: 1px solid var(--warning-border);
    border-radius: var(--radius-lg);
    background-color: var(--warning-bg);
  }

  .merge-preview h3,
  .merge-preview p {
    margin: 0;
  }

  .merge-preview h3 {
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

  .danger-outline {
    border: 1px solid var(--error-border);
    background-color: transparent;
    color: var(--error-text);
  }

  .danger-outline:hover:not(:disabled) {
    background-color: rgba(var(--error-color-rgb), 0.08);
  }
</style>
