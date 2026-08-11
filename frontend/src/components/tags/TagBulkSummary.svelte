<script lang="ts">
  /**
   * What a multi-tag selection is and what can be done to it.
   *
   * The survivor picker is the point of this pane: merge is the least
   * reversible operation the manager offers — nothing splits a merged tag back
   * apart — so the surviving name is chosen explicitly, never implicitly.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';
  import { tagOriginKey, type TagListEntry } from './TagList.svelte';
  import type { TagImpact } from '$lib/types/tag';

  /** The selected tags, in list order. */
  export let tags: TagListEntry[] = [];
  /**
   * The survivor the backend recommends for a collision cluster. When it is one
   * of the selected tags it wins the preselection; otherwise the highest-usage
   * selected tag does.
   */
  export let suggestedSurvivorUuid: string | null = null;
  export let busy: 'merge' | 'delete' | 'promote' | null = null;
  /**
   * Whether the caller may publish tags into the shared vocabulary.
   *
   * Cosmetic only — `POST /tags/promote` enforces the admin check itself. The
   * button is hidden rather than disabled because a non-admin has no way to
   * earn the right in this session, so a disabled control would only puzzle.
   */
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

  // Only a tag you own can be promoted: a system tag is already shared, and a
  // `shared_with_me` tag is someone else's to publish. A selection with none of
  // them hides the button rather than offering a no-op that 404s.
  $: promotableCount = tags.filter((tag) => (tag.ownership ?? 'mine') === 'mine').length;

  let survivorUuid = '';
  let lastSelectionKey = '';

  $: byUsage = [...tags].sort(
    (a, b) => b.usage_count - a.usage_count || a.name.localeCompare(b.name)
  );
  $: selectionKey = tags.map((tag) => tag.uuid).join('|');
  // Re-preselect whenever the selection itself changes, but never on a rerender
  // that only refreshed counts — the user's explicit pick must stick.
  $: if (selectionKey !== lastSelectionKey) {
    lastSelectionKey = selectionKey;
    survivorUuid =
      suggestedSurvivorUuid && tags.some((tag) => tag.uuid === suggestedSurvivorUuid)
        ? suggestedSurvivorUuid
        : (byUsage[0]?.uuid ?? '');
  }

  $: canMerge = tags.length > 1 && survivorUuid !== '';
</script>

<div class="bulk-summary">
  <header class="bulk-head">
    <h2 class="bulk-title">{$t('tags.manager.bulk.title', { count: tags.length })}</h2>
    <button
      type="button"
      class="btn btn-ghost"
      on:click={() => dispatch('clear')}
      disabled={busy !== null}
    >
      {$t('tags.manager.action.clearSelection')}
    </button>
  </header>

  <fieldset class="survivor">
    <legend class="survivor-legend">{$t('tags.manager.bulk.survivorLegend')}</legend>
    <p class="note">{$t('tags.manager.bulk.survivorHint')}</p>
    <div class="survivor-options">
      {#each byUsage as tag (tag.uuid)}
        <label class="survivor-option" class:is-chosen={survivorUuid === tag.uuid}>
          <input
            type="radio"
            name="tag-survivor"
            value={tag.uuid}
            bind:group={survivorUuid}
            disabled={busy !== null}
          />
          <span class="survivor-name">{tag.name}</span>
          <span class="survivor-meta">
            <span>{$t('tags.manager.usage', { count: tag.usage_count })}</span>
            <span>{$t(tagOriginKey(tag.source))}</span>
          </span>
        </label>
      {/each}
    </div>
  </fieldset>

  <div class="bulk-actions">
    <button
      type="button"
      class="btn btn-primary"
      on:click={() => dispatch('previewMerge', { survivorUuid })}
      disabled={!canMerge || busy !== null}
    >
      {$t('tags.manager.action.merge')}
    </button>
    {#if canPromote && promotableCount > 0}
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

  {#if mergePreview}
    <section class="panel panel-warning">
      <h3 class="panel-title">{$t('tags.manager.impact.title')}</h3>
      <p class="panel-text">
        {$t('tags.manager.bulk.mergeInto', {
          name: tags.find((tag) => tag.uuid === survivorUuid)?.name ?? '',
        })}
      </p>
      <ul class="impact-list">
        <li>
          {$t('tags.manager.impact.accessible', { count: mergePreview.accessible_file_count })}
        </li>
        <li class="impact-total">
          {$t('tags.manager.impact.total', { count: mergePreview.total_file_count })}
        </li>
      </ul>
      <p class="note">{$t('tags.manager.impact.totalNote', { count: mergePreview.total_file_count })}</p>
      <div class="panel-actions">
        <button
          type="button"
          class="btn btn-danger"
          on:click={() => dispatch('confirmMerge', { survivorUuid })}
          disabled={busy !== null}
        >
          {busy === 'merge' ? $t('tags.manager.action.merging') : $t('tags.manager.action.merge')}
        </button>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={() => dispatch('cancelMerge')}
          disabled={busy !== null}
        >
          {$t('tags.manager.action.cancel')}
        </button>
      </div>
    </section>
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
      <p class="note">
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

</div>

<style>
  .bulk-summary {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .bulk-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .bulk-title {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-color);
  }

  .survivor {
    margin: 0;
    padding: 12px 14px;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background: var(--surface-color);
  }

  .survivor-legend {
    padding: 0 4px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-color);
  }

  .survivor-options {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 8px;
  }

  .survivor-option {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    cursor: pointer;
  }

  .survivor-option.is-chosen {
    border-color: var(--primary-color);
    background: rgba(var(--primary-color-rgb), 0.1);
  }

  .survivor-option:focus-within {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .survivor-name {
    font-size: 14px;
    color: var(--text-color);
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .survivor-meta {
    display: flex;
    gap: 10px;
    font-size: 12px;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .bulk-actions,
  .panel-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
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

  .note {
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

  @media (max-width: 768px) {
    .survivor-option {
      grid-template-columns: auto 1fr;
    }
    .survivor-meta {
      grid-column: 2;
    }
  }
</style>
