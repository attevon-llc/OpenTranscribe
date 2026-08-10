<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorStatus, getErrorCode } from '$lib/utils/apiError';
  import { addTagToFile, listTags, removeTagFromFile } from '$lib/api/tags';
  import {
    MAX_TAG_NAME_LENGTH,
    cleanTagName,
    findSimilarTagName,
    normalizeTagName,
  } from '$lib/utils/tagName';
  import AISuggestionsDropdown from './AISuggestionsDropdown.svelte';
  import SearchableMultiSelect from './SearchableMultiSelect.svelte';

  import type { Tag } from '$lib/types/tag';

  type AISuggestion = { name: string; confidence: number; rationale?: string };

  export let fileId = "";
  export let tags: Tag[] = [];
  export let aiSuggestions: AISuggestion[] = [];

  // Duplicate filtering matches the backend's normalization, not exact
  // lowercase: hyphens, underscores, and whitespace runs collapse too, so a
  // suggestion for `Q3-Review` resolves onto a `q3 review` already on the file.
  // Lowercase alone would still offer it, and the click would be swallowed by
  // the "already present" guard with no feedback at all.
  $: appliedNormalizedNames = new Set(tags.map(tag => normalizeTagName(tag.name)));

  $: filteredAISuggestions = aiSuggestions.filter(suggestion =>
    !appliedNormalizedNames.has(normalizeTagName(suggestion.name))
  );

  $: autoAppliedTagNames = tags
    .filter(tag => tag.source === 'auto_ai')
    .map(tag => tag.name);

  let allTags: Tag[] = [];
  let newTagInput = '';
  let loading = false;

  /**
   * Which tag the last applied name actually became, when it was not the name
   * that was typed. Mirrored into an `aria-live` region beside the toast.
   */
  let resolvedNotice = '';

  /**
   * A near match waiting on the user. Never applied on its own (R18): at the
   * backend's 0.85 threshold `q3-earnings` and `q4-earnings` score 0.909, and
   * nothing in the product can split two tags back apart afterwards.
   */
  let nearMatch: { typed: string; name: string } | null = null;

  // Event dispatcher
  const dispatch = createEventDispatcher<{
    tagsUpdated: { tags: Tag[] };
    aiSuggestionAccepted: { suggestion: AISuggestion };
  }>();

  // Fetch all available tags
  async function fetchAllTags() {
    try {
      const fetched = await listTags();

      // Ensure all tags have valid IDs before adding them to the allTags array
      allTags = (fetched || []).filter(
        (tag) => tag && typeof tag === 'object' && !!tag.uuid && !!tag.name
      );
    } catch (err: unknown) {
      console.error('[TagsEditor] Error fetching tags:', err);
      const status = getErrorStatus(err);
      if (status === 401) {
        toastStore.error($t('tags.unauthorizedLogin'));
      } else if (getErrorCode(err) === 'ERR_NETWORK') {
        toastStore.error($t('tags.networkError'));
      } else {
        toastStore.error($t('tags.failedToLoad'));
      }
    }
  }

  // Add a tag to the file
  async function addTag(tagId: string) {
    // The notice describes one applied name; another action makes it stale.
    resolvedNotice = '';
    loading = true;
    try {
      // Adding tag to file

      // Get the tag name by ID and send tag_data with name field as required by backend
      const tagToAdd = allTags.find(t => t.uuid === tagId);
      if (!tagToAdd) {
        console.error(`[TagsEditor] Tag with ID ${tagId} not found in allTags`);
        throw new Error('Tag not found');
      }

      // Use the response from add_tag_to_file as the final tag object
      const finalTag = await addTagToFile(fileId, tagToAdd.name);

      // Ensure the tag has a valid UUID and name
      if (!finalTag || !finalTag.uuid || !finalTag.name) {
        console.error('[TagsEditor] Invalid tag received from server:', finalTag);
        throw new Error('Server returned an invalid tag');
      }

      // Only add the tag if it's not already present
      if (!tags.some(t => t.uuid === finalTag.uuid)) {
        tags = [...tags, finalTag];
        dispatch('tagsUpdated', { tags });
      }
    } catch (err: unknown) {
      if (getErrorStatus(err) === 401) {
        toastStore.error($t('tags.unauthorizedLogin'));
      } else {
        toastStore.error($t('tags.failedToAdd'));
      }
      console.error('[TagsEditor] Error adding tag:', err);
    } finally {
      loading = false;
    }
  }

  /** Announce a resolved name through the toast store and the live region. */
  function announce(message: string) {
    resolvedNotice = message;
    if (message) toastStore.info(message);
  }

  /**
   * Apply one name to the file and report which tag it actually became.
   *
   * The server is the authority on resolution: `POST /files/{uuid}/tags`
   * resolves the supplied name by normalized-exact match and returns the tag it
   * landed on, so typing `Interview` comes back as the existing `interview`.
   * That name is compared against what was typed and announced rather than
   * applied silently (R2) — the editor never decides which row a name is.
   *
   * @param name - The name to send. Either what the user typed, or the name of
   *   a near match they accepted.
   * @param typed - What the user actually typed, for the announcement.
   */
  async function applyTagName(name: string, typed: string) {
    loading = true;
    try {
      const finalTag = await addTagToFile(fileId, name);
      if (!finalTag || !finalTag.uuid || !finalTag.name) {
        console.error('[TagsEditor] Invalid tag received after adding to file:', finalTag);
        throw new Error('Server returned an invalid tag after adding to file');
      }

      if (!allTags.some(t => t.uuid === finalTag.uuid)) {
        allTags = [...allTags, finalTag];
      }

      if (tags.some(t => t.uuid === finalTag.uuid)) {
        // The name resolved onto a tag the file already carries. That is a
        // successful no-op, but it must not be a silent one — name the tag that
        // absorbed it, or the click looks broken.
        announce($t('tags.alreadyOnFile', { name: finalTag.name }));
      } else {
        tags = [...tags, finalTag];
        dispatch('tagsUpdated', { tags });
        announce(
          finalTag.name === typed ? '' : $t('tags.resolvedTo', { name: finalTag.name, typed })
        );
      }
      newTagInput = '';
    } catch (err: unknown) {
      // Safely check for error properties
      if (err && typeof err === 'object') {
        const errorObj = err as any;

        if (errorObj.response && errorObj.response.status === 401) {
          toastStore.error($t('tags.unauthorizedLogin'));
        } else if (
          errorObj.response &&
          typeof errorObj.response === 'object' &&
          'data' in errorObj.response &&
          errorObj.response.data &&
          typeof errorObj.response.data === 'object' &&
          'detail' in errorObj.response.data
        ) {
          toastStore.error($t('tags.failedToCreateWithDetail', { detail: errorObj.response.data.detail }));
        } else {
          toastStore.error($t('tags.failedToCreate'));
        }
      } else {
        toastStore.error($t('tags.failedToCreate'));
      }
      console.error('[TagsEditor] Error creating tag:', err);
    } finally {
      loading = false;
    }
  }

  /**
   * Decide what the typed name may do before anything is sent.
   *
   * A normalized-exact hit (or no match at all) goes straight to the server,
   * which resolves it. A name that only *nearly* matches an existing tag is
   * offered instead of applied — accepting it is the user's call, and declining
   * creates the tag they actually typed (R18).
   */
  async function submitTypedName() {
    if (loading) return;

    // Mirror the server's own rule rather than inventing a client allowlist:
    // it clamps to the stored column width and rejects a name that is empty
    // once normalized.
    const cleaned = cleanTagName(newTagInput);
    if (!normalizeTagName(cleaned)) {
      toastStore.error($t('tags.nameRequired'));
      return;
    }

    resolvedNotice = '';
    nearMatch = null;

    const normalized = normalizeTagName(cleaned);
    const resolvesExactly = allTags.some(tag => normalizeTagName(tag.name) === normalized);
    if (!resolvesExactly) {
      const similar = findSimilarTagName(cleaned, allTags.map(tag => tag.name));
      if (similar) {
        nearMatch = { typed: cleaned, name: similar };
        return;
      }
    }

    await applyTagName(cleaned, cleaned);
  }

  /** Take the offered near match: the existing tag is applied, and announced. */
  function acceptNearMatch() {
    const pending = nearMatch;
    if (!pending) return;
    nearMatch = null;
    void applyTagName(pending.name, pending.typed);
  }

  /** Decline the offered near match: the typed name becomes its own tag. */
  function declineNearMatch() {
    const pending = nearMatch;
    if (!pending) return;
    nearMatch = null;
    void applyTagName(pending.typed, pending.typed);
  }

  // Remove a tag from the file
  async function removeTag(tagId: string) {
    resolvedNotice = '';
    nearMatch = null;
    loading = true;
    try {
      // Removing tag from file

      // Find the tag by ID to get its name - the backend needs tag name, not ID
      const tagToRemove = tags.find(t => t.uuid === tagId);
      if (!tagToRemove) {
        console.error(`[TagsEditor] Tag with ID ${tagId} not found in tags list`);
        throw new Error('Tag not found');
      }

      await removeTagFromFile(fileId, tagToRemove.name);

      // Update the local tags array
      // Filter out the removed tag using a safe comparison
      const updatedTags = tags.filter(t =>
        t.uuid !== undefined && t.uuid !== null && t.uuid !== tagId
      );

      // Only update if something actually changed
      if (updatedTags.length !== tags.length) {
        tags = updatedTags;
        dispatch('tagsUpdated', { tags });
      }
    } catch (err: unknown) {
      console.error('[TagsEditor] Error removing tag:', err);
      if (getErrorStatus(err) === 401) {
        toastStore.error($t('tags.unauthorizedLogin'));
      } else if (getErrorCode(err) === 'ERR_NETWORK') {
        toastStore.error($t('tags.networkError'));
      } else {
        toastStore.error($t('tags.failedToRemove'));
      }
    } finally {
      loading = false;
    }
  }

  // Handle keydown event in the input field
  function handleInputKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter' && newTagInput.trim()) {
      event.preventDefault();
      submitTypedName();
    }
  }

  let suggestedTags: Tag[] = [];
  let dropdownTags: Array<{ id: string; name: string; count: number }> = []; // All available tags for dropdown

  $: aiSuggestionNormalizedNames = new Set(
    filteredAISuggestions.map(suggestion => normalizeTagName(suggestion.name))
  );

  // Everything offerable: neither already on the file nor already offered above
  // as an AI suggestion. Both comparisons use the backend's normalization, so a
  // tag that a name would merely resolve onto is treated as already applied.
  $: availableTags = allTags.filter(tag => {
    const normalized = normalizeTagName(tag.name);
    if (tag.uuid && tags.some(t => t.uuid === tag.uuid)) return false;
    if (appliedNormalizedNames.has(normalized)) return false;
    return !aiSuggestionNormalizedNames.has(normalized);
  });

  // Get top 5 suggested tags as chips (most used)
  $: suggestedTags = [...availableTags]
    .sort((a, b) => (b.usage_count || 0) - (a.usage_count || 0)) // Sort by usage count
    .slice(0, 5); // Top 5 most used

  // Get all available tags for dropdown (excluding already assigned and AI suggestions)
  $: dropdownTags = availableTags.map(tag => ({
    id: tag.uuid,
    name: tag.name,
    count: tag.usage_count || 0
  }));

  // Handle multiselect tag selection
  async function handleTagSelect(event: CustomEvent<{ id: string | number }>) {
    const { id } = event.detail;
    await addTag(String(id));
  }

  // Handle AI suggestion acceptance
  async function handleAcceptAISuggestion(event: CustomEvent<{ suggestion: AISuggestion }>) {
    const { suggestion } = event.detail;
    // A person accepted this name, so it takes the same path a typed name does:
    // a near match is offered, never substituted (R18).
    newTagInput = suggestion.name;
    await submitTypedName();
    // Don't remove from aiSuggestions - let reactive filtering handle it
    // This allows suggestions to reappear if the tag is later removed
    dispatch('aiSuggestionAccepted', { suggestion });
  }

  onMount(() => {
    fetchAllTags();
  });
</script>

<div class="tags-editor">
  <div class="tags-list">
    {#if tags.length === 0}
      <span class="no-tags">{$t('tags.noTagsYet')}</span>
    {/if}
    {#each tags.filter(t => t && t.uuid !== undefined) as tag (tag.uuid)}
      <span class="tag" class:ai-tag={tag.source === 'auto_ai'}>
        {#if tag.source === 'auto_ai'}
          <span class="ai-icon" title={$t('autoLabel.autoAppliedTag')}>
            <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
          </span>
        {/if}
        {tag.name}
        <button class="tag-remove" on:click={() => removeTag(tag.uuid)} title={$t('tags.removeTag')}>×</button>
      </span>
    {/each}

    <div class="tag-input-container">
      <input
        type="text"
        placeholder={$t('tags.addTagPlaceholder')}
        bind:value={newTagInput}
        on:keydown={handleInputKeydown}
        class="tag-input"
        disabled={loading}
        maxlength={MAX_TAG_NAME_LENGTH}
        title={$t('tags.typeNewTagHint')}
      >
      {#if newTagInput.trim()}
        <button
          class="tag-add-button"
          on:click={submitTypedName}
          disabled={loading}
          title={$t('tags.createAndAddHint', { tagName: newTagInput.trim() })}
        >
          {$t('tags.add')}
        </button>
      {/if}
    </div>
  </div>

  <!--
    A near match is a question, never an application. Both answers are plain
    buttons so the prompt is keyboard-operable, and the whole panel is a live
    region so it is announced when it appears.
  -->
  {#if nearMatch}
    <div class="near-match" role="status" aria-live="polite">
      <p class="near-match-question">
        {$t('tags.nearMatch.question', { name: nearMatch.name, typed: nearMatch.typed })}
      </p>
      <div class="near-match-actions">
        <button
          type="button"
          class="near-match-accept"
          on:click={acceptNearMatch}
          disabled={loading}
        >
          {$t('tags.nearMatch.accept', { name: nearMatch.name })}
        </button>
        <button
          type="button"
          class="near-match-decline"
          on:click={declineNearMatch}
          disabled={loading}
        >
          {$t('tags.nearMatch.decline', { typed: nearMatch.typed })}
        </button>
      </div>
    </div>
  {/if}

  {#if resolvedNotice}
    <p class="resolved-notice" role="status" aria-live="polite">{resolvedNotice}</p>
  {/if}

  <!-- AI Suggestions Dropdown -->
  <AISuggestionsDropdown
    suggestions={filteredAISuggestions}
    type="tag"
    {loading}
    autoAppliedTags={autoAppliedTagNames}
    on:accept={handleAcceptAISuggestion}
  />

  {#if suggestedTags.length > 0}
    <div class="suggested-tags">
      <span class="suggested-label">{$t('tags.suggested')}</span>
      {#each suggestedTags.filter(t => t && t.uuid !== undefined) as tag (tag.uuid)}
        <button
          class="suggested-tag"
          on:click={() => addTag(tag.uuid)}
          disabled={loading}
          title={$t('tags.addExistingTagHint', { tagName: tag.name })}
        >
          {tag.name}
        </button>
      {/each}
    </div>
  {/if}

  {#if dropdownTags.length > 0}
    <div class="dropdown-section">
      <span class="dropdown-label">{$t('tags.selectFromAllTags')}</span>
      <SearchableMultiSelect
        options={dropdownTags}
        selectedIds={[]}
        placeholder={$t('tags.addFromLibraryPlaceholder')}
        showCounts={true}
        on:select={handleTagSelect}
      />
    </div>
  {/if}
</div>

<style>
  .tags-editor {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .no-tags {
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-style: italic;
    padding: 0.5rem 0;
  }

  .tags-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }

  .tag {
    background-color: rgba(59, 130, 246, 0.1);
    color: var(--primary-color);
    padding: 0.35rem 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }

  .tag-remove {
    background: none;
    border: none;
    color: var(--text-light);
    font-size: 1rem;
    cursor: pointer;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
  }

  .tag-remove:hover {
    color: var(--error-color);
  }

  .tag-input-container {
    display: flex;
    align-items: center;
  }

  .tag-input {
    background: transparent;
    border: none;
    border-bottom: 1px dashed var(--border-color);
    padding: 0.35rem 0;
    font-size: 0.8rem;
    width: 100px;
    color: var(--text-color);
  }

  .tag-input:focus {
    border-bottom-color: var(--primary-color);
    outline: none;
  }

  .tag-add-button {
    background-color: #3b82f6;
    color: white;
    border: none;
    border-radius: 4px;
    padding: 0.25rem 0.5rem;
    font-size: 0.8rem;
    cursor: pointer;
    margin-left: 0.5rem;
  }

  .tag-add-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /*
   * The near-match prompt and the resolved-name notice both theme off custom
   * properties only, so light and dark follow `styles/theme.css` with no
   * per-theme override of their own.
   */
  .near-match {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.5rem 0.65rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: rgba(var(--primary-color-rgb), 0.06);
  }

  .near-match-question {
    margin: 0;
    font-size: 0.8rem;
    line-height: 1.45;
    color: var(--text-color);
  }

  .near-match-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .near-match-accept,
  .near-match-decline {
    border-radius: 4px;
    padding: 0.25rem 0.6rem;
    font-size: 0.8rem;
    font-family: inherit;
    cursor: pointer;
  }

  .near-match-accept {
    border: 1px solid var(--primary-color);
    background-color: var(--primary-color);
    color: var(--surface-color);
  }

  .near-match-decline {
    border: 1px solid var(--border-color);
    background-color: transparent;
    color: var(--text-color);
  }

  .near-match-decline:hover:not(:disabled) {
    background-color: var(--button-hover);
  }

  .near-match-accept:focus-visible,
  .near-match-decline:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .near-match-accept:disabled,
  .near-match-decline:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .resolved-notice {
    margin: 0;
    font-size: 0.8rem;
    line-height: 1.45;
    color: var(--text-secondary);
  }

  .suggested-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.8rem;
  }

  .suggested-label {
    color: var(--text-light);
  }

  .suggested-tag {
    background: none;
    border: 1px dashed var(--border-color);
    border-radius: 4px;
    padding: 0.25rem 0.5rem;
    font-size: 0.8rem;
    cursor: pointer;
    color: var(--text-light);
  }

  .suggested-tag:hover {
    border-color: var(--primary-color);
    color: var(--primary-color);
    background-color: rgba(59, 130, 246, 0.05);
  }

  .suggested-tag:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .dropdown-section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .dropdown-label {
    color: var(--text-light);
    font-size: 0.8rem;
  }

  .ai-tag {
    background-color: rgba(59, 130, 246, 0.08);
    border: 1px solid rgba(59, 130, 246, 0.25);
  }

  .ai-icon {
    color: var(--ai-accent-color, var(--primary-color, #3b82f6));
    display: flex;
    align-items: center;
  }

  :global([data-theme='dark']) .ai-tag {
    background-color: rgba(96, 165, 250, 0.12);
    border-color: rgba(96, 165, 250, 0.3);
  }
</style>
