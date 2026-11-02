<script lang="ts">
  /**
   * The file detail page's tag editor: the file's tags as removable chips, a
   * free-text field, the most used library tags, the full library, and AI
   * suggestions.
   *
   * The server decides which row a typed name becomes. Before asking it, the
   * editor checks only whether the name already resolves to a library tag and,
   * if not, whether it is a near match of one; a near match is put to the user
   * as a question and never substituted silently. Nothing is optimistic: only
   * the tag the server returned is appended.
   */
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { addTagToFile, listTags, removeTagFromFile } from '$lib/api/tags';
  import { getErrorCode, getErrorMessage, getErrorStatus } from '$lib/utils/apiError';
  import {
    clampTagNameInput,
    cleanTagName,
    findSimilarTagName,
    normalizeTagName,
  } from '$lib/utils/tagName';
  import type { Tag, TagWithCount } from '$lib/types/tag';
  import AISuggestionsDropdown from './AISuggestionsDropdown.svelte';
  import SearchableMultiSelect from './SearchableMultiSelect.svelte';

  type AISuggestion = { name: string; confidence: number; rationale?: string };

  /** The file uuid. */
  export let fileId = '';
  export let tags: Tag[] = [];
  export let aiSuggestions: AISuggestion[] = [];

  const dispatch = createEventDispatcher<{
    tagsUpdated: { tags: Tag[] };
    aiSuggestionAccepted: { suggestion: AISuggestion };
  }>();

  const SUGGESTED_COUNT = 5;

  let library: TagWithCount[] = [];
  let input = '';
  let busy = false;
  let notice = '';
  /** A typed name that resembles a library tag, waiting for the user's choice. */
  let nearMatch: { typed: string; name: string; suggestion: AISuggestion | null } | null = null;

  $: fileNames = new Set(tags.map((tag) => normalizeTagName(tag.name)));
  $: fileUuids = new Set(tags.map((tag) => tag.uuid));
  $: offeredAi = aiSuggestions.filter(
    (suggestion) => !fileNames.has(normalizeTagName(suggestion.name))
  );
  $: offeredAiNames = new Set(offeredAi.map((suggestion) => normalizeTagName(suggestion.name)));
  $: offerable = library.filter((tag) => {
    const key = normalizeTagName(tag.name);
    return !fileUuids.has(tag.uuid) && !fileNames.has(key) && !offeredAiNames.has(key);
  });
  $: suggested = [...offerable]
    .sort((a, b) => b.usage_count - a.usage_count)
    .slice(0, SUGGESTED_COUNT);
  $: dropdownOptions = offerable.map((tag) => ({
    id: tag.uuid,
    name: tag.name,
    count: tag.usage_count,
  }));
  $: autoAppliedTags = tags.filter((tag) => tag.source === 'auto_ai').map((tag) => tag.name);
  $: trimmedInput = input.trim();

  onMount(() => {
    void loadLibrary();
  });

  async function loadLibrary() {
    try {
      const rows: (TagWithCount | null)[] = await listTags();
      library = rows.filter((row): row is TagWithCount => !!row && !!row.uuid && !!row.name);
    } catch (error: unknown) {
      console.error('[TagsEditor] Failed to load tags:', error);
      if (getErrorStatus(error) === 401) toastStore.error($t('tags.unauthorizedLogin'));
      else if (getErrorCode(error) === 'ERR_NETWORK') toastStore.error($t('tags.networkError'));
      else toastStore.error($t('tags.failedToLoad'));
    }
  }

  /** Sets the polite live region and mirrors it as an info toast; empty text shows nothing. */
  function announce(text: string) {
    notice = text;
    if (text) toastStore.info(text);
  }

  function addToFile(tag: Tag) {
    tags = [...tags, tag];
    dispatch('tagsUpdated', { tags });
  }

  function rememberInLibrary(tag: Tag) {
    if (!library.some((row) => row.uuid === tag.uuid)) {
      library = [...library, { ...tag, usage_count: 0 }];
    }
  }

  /** The server's `detail`, rendered as `getErrorMessage` renders it, ignoring axios' own text. */
  function detailText(error: unknown): string {
    const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data
      ?.detail;
    if (detail === undefined || detail === null) return '';
    return getErrorMessage({ response: { data: { detail } } }, '');
  }

  /** Attaches `name`; resolves true when the tag is (now or already) on the file. */
  async function apply(name: string, typed: string): Promise<boolean> {
    busy = true;
    try {
      const tag: Tag | null = await addTagToFile(fileId, name);
      if (!tag || !tag.uuid || !tag.name) throw new Error('tag response without uuid or name');
      rememberInLibrary(tag);
      if (tags.some((existing) => existing.uuid === tag.uuid)) {
        announce($t('tags.alreadyOnFile', { name: tag.name }));
      } else {
        addToFile(tag);
        announce(tag.name !== typed ? $t('tags.resolvedTo', { name: tag.name, typed }) : '');
      }
      input = '';
      return true;
    } catch (error: unknown) {
      console.error('[TagsEditor] Failed to add tag:', error);
      const detail = detailText(error);
      if (getErrorStatus(error) === 401) toastStore.error($t('tags.unauthorizedLogin'));
      else if (detail) toastStore.error($t('tags.failedToCreateWithDetail', { detail }));
      else toastStore.error($t('tags.failedToCreate'));
      return false;
    } finally {
      busy = false;
    }
  }

  /**
   * Submits the typed name. Resolves true only when the name was applied now;
   * a near-match question is not an application.
   */
  async function submitTyped(suggestion: AISuggestion | null = null): Promise<boolean> {
    if (busy) return false;
    const cleaned = cleanTagName(input);
    if (!normalizeTagName(cleaned)) {
      toastStore.error($t('tags.nameRequired'));
      return false;
    }
    notice = '';
    nearMatch = null;
    const key = normalizeTagName(cleaned);
    if (!library.some((tag) => normalizeTagName(tag.name) === key)) {
      const similar = findSimilarTagName(
        cleaned,
        library.map((tag) => tag.name)
      );
      if (similar) {
        nearMatch = { typed: cleaned, name: similar, suggestion };
        return false;
      }
    }
    return apply(cleaned, cleaned);
  }

  async function resolveNearMatch(useExisting: boolean) {
    if (!nearMatch || busy) return;
    const { typed, name, suggestion } = nearMatch;
    const applied = await apply(useExisting ? name : typed, typed);
    if (applied) {
      nearMatch = null;
      if (suggestion) dispatch('aiSuggestionAccepted', { suggestion });
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    if (input.trim()) void submitTyped();
  }

  async function addExisting(uuid: string) {
    const tag = library.find((row) => row.uuid === uuid);
    if (!tag || busy) return;
    busy = true;
    try {
      const added: Tag = await addTagToFile(fileId, tag.name);
      if (!tags.some((existing) => existing.uuid === added.uuid)) addToFile(added);
    } catch (error: unknown) {
      console.error('[TagsEditor] Failed to add existing tag:', error);
      toastStore.error(
        getErrorStatus(error) === 401 ? $t('tags.unauthorizedLogin') : $t('tags.failedToAdd')
      );
    } finally {
      busy = false;
    }
  }

  async function removeTag(tag: Tag) {
    if (busy) return;
    notice = '';
    nearMatch = null;
    busy = true;
    try {
      await removeTagFromFile(fileId, tag.name);
      const remaining = tags.filter((existing) => existing.uuid !== tag.uuid);
      if (remaining.length !== tags.length) {
        tags = remaining;
        dispatch('tagsUpdated', { tags });
      }
    } catch (error: unknown) {
      console.error('[TagsEditor] Failed to remove tag:', error);
      if (getErrorStatus(error) === 401) toastStore.error($t('tags.unauthorizedLogin'));
      else if (getErrorCode(error) === 'ERR_NETWORK') toastStore.error($t('tags.networkError'));
      else toastStore.error($t('tags.failedToRemove'));
    } finally {
      busy = false;
    }
  }

  async function acceptAiSuggestion(event: CustomEvent<{ suggestion: AISuggestion }>) {
    const { suggestion } = event.detail;
    input = suggestion.name;
    if (await submitTyped(suggestion)) dispatch('aiSuggestionAccepted', { suggestion });
  }
</script>

<div class="tags-editor">
  {#if tags.length === 0}
    <p class="no-tags">{$t('tags.noTagsYet')}</p>
  {:else}
    <div class="tags-list">
      {#each tags as tag (tag.uuid)}
        <span class="tag" class:ai-tag={tag.source === 'auto_ai'}>
          {#if tag.source === 'auto_ai'}
            <span class="ai-icon" title={$t('autoLabel.autoAppliedTag')}>
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="currentColor"
                aria-hidden="true"
              >
                <path d="M12 2l2.4 7.6L22 12l-7.6 2.4L12 22l-2.4-7.6L2 12l7.6-2.4z" />
              </svg>
            </span>
          {/if}
          <span class="tag-text">{tag.name}</span>
          <button
            type="button"
            class="tag-remove"
            title={$t('tags.removeTag')}
            aria-label={$t('gallery.bulk.tagModal.removeChip', { name: tag.name })}
            disabled={busy}
            on:click={() => removeTag(tag)}
          >
            <span aria-hidden="true">×</span>
          </button>
        </span>
      {/each}
    </div>
  {/if}

  {#if notice}
    <p class="notice" role="status" aria-live="polite">{notice}</p>
  {/if}

  <div class="input-row">
    <input
      type="text"
      class="tag-input"
      value={input}
      placeholder={$t('tags.addTagPlaceholder')}
      title={$t('tags.typeNewTagHint')}
      disabled={busy}
      on:input={(event) => (input = clampTagNameInput(event))}
      on:keydown={handleKeydown}
    />
    {#if trimmedInput}
      <button
        type="button"
        class="btn btn-primary btn-small tag-add-button"
        title={$t('tags.createAndAddHint', { tagName: trimmedInput })}
        disabled={busy}
        on:click={() => void submitTyped()}
      >
        {$t('tags.add')}
      </button>
    {/if}
  </div>

  {#if nearMatch}
    <div class="near-match" role="status" aria-live="polite">
      <p>{$t('tags.nearMatch.question', { name: nearMatch.name, typed: nearMatch.typed })}</p>
      <div class="near-match-actions">
        <button
          type="button"
          class="btn btn-secondary btn-small"
          disabled={busy}
          on:click={() => resolveNearMatch(true)}
        >
          {$t('tags.nearMatch.accept', { name: nearMatch.name })}
        </button>
        <button
          type="button"
          class="btn btn-secondary btn-small"
          disabled={busy}
          on:click={() => resolveNearMatch(false)}
        >
          {$t('tags.nearMatch.decline', { typed: nearMatch.typed })}
        </button>
      </div>
    </div>
  {/if}

  {#if offeredAi.length > 0}
    <AISuggestionsDropdown
      suggestions={offeredAi}
      type="tag"
      loading={busy}
      {autoAppliedTags}
      on:accept={acceptAiSuggestion}
    />
  {/if}

  {#if offerable.length > 0}
    <div class="library">
      <span class="section-label">{$t('tags.suggested')}</span>
      <div class="suggested-list">
        {#each suggested as tag (tag.uuid)}
          <button
            type="button"
            class="suggested-tag"
            title={$t('tags.addExistingTagHint', { tagName: tag.name })}
            disabled={busy}
            on:click={() => addExisting(tag.uuid)}
          >
            {tag.name}
          </button>
        {/each}
      </div>
      <span class="section-label">{$t('tags.selectFromAllTags')}</span>
      <SearchableMultiSelect
        options={dropdownOptions}
        selectedIds={[]}
        placeholder={$t('tags.addFromLibraryPlaceholder')}
        showCounts
        disabled={busy}
        on:select={(event) => addExisting(String(event.detail.id))}
      />
    </div>
  {/if}
</div>

<style>
  .tags-editor {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    color: var(--text-color);
  }

  .no-tags {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .tags-list,
  .suggested-list {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
  }

  .tag {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding-block: var(--space-0-5);
    padding-inline: var(--space-2-5) var(--space-1);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-pill);
    background-color: var(--surface-secondary);
    font-size: var(--font-size-sm);
  }

  .tag.ai-tag {
    border-color: rgba(var(--primary-color-rgb), 0.35);
    background-color: rgba(var(--primary-color-rgb), 0.08);
  }

  /* The light tint washes out on the dark card, so the AI tint is stronger there. */
  :global([data-theme='dark']) .tag.ai-tag {
    border-color: rgba(var(--primary-color-rgb), 0.55);
    background-color: rgba(var(--primary-color-rgb), 0.2);
  }

  .ai-icon {
    display: inline-flex;
    color: var(--primary-on-surface);
  }

  .tag-text {
    overflow-wrap: anywhere;
  }

  .tag-remove {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0 var(--space-1);
    border: none;
    border-radius: var(--radius-round);
    background: transparent;
    color: var(--text-secondary);
    font-size: var(--font-size-md);
    line-height: var(--line-height-none);
    box-shadow: none;
  }

  .tag-remove:hover:not(:disabled) {
    background-color: var(--hover-color);
    color: var(--error-text);
  }

  .notice {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .input-row {
    display: flex;
    align-items: center;
  }

  .tag-input {
    flex: 1 1 auto;
    min-width: 0;
  }

  .tag-add-button {
    flex-shrink: 0;
    margin-inline-start: var(--space-2);
  }

  .near-match {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-2-5) var(--space-3);
    border: 1px solid var(--warning-border);
    border-radius: var(--radius-lg);
    background-color: var(--warning-bg);
    font-size: var(--font-size-sm);
  }

  .near-match p {
    margin: 0;
  }

  .near-match-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .library {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .section-label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .suggested-tag {
    padding: var(--space-0-5) var(--space-2-5);
    border: 1px dashed var(--border-color);
    border-radius: var(--radius-pill);
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: var(--font-size-sm);
    box-shadow: none;
  }

  .suggested-tag:hover:not(:disabled) {
    border-color: var(--primary-color);
    background-color: var(--hover-color);
  }

  .suggested-tag:focus-visible,
  .tag-remove:focus-visible {
    outline: 2px solid var(--focus-ring-color);
    outline-offset: 1px;
  }
</style>
