<script lang="ts">
  /**
   * Apply or strip one tag across the gallery selection.
   *
   * Two things this does that a plain "post and toast" would get wrong:
   *
   * 1. **A no-op is not a failure.** The backend reports a per-file `outcome`,
   *    so a file that already carried the tag comes back `already_present` —
   *    successful, unchanged. The summary keeps changed and unchanged apart and
   *    only calls `failed` a failure, so one refused file never reads as a
   *    failed batch.
   * 2. **The applied name may not be the typed name.** Tags resolve by
   *    normalized-exact match, so typing `Interview` applies the existing
   *    `interview` — across the whole selection at once. The add path resolves
   *    through `POST /tags` (the same resolver the bulk rail uses: existing tag
   *    or a new one, never a near match) and the summary states the resolved
   *    name *before* the counts whenever it differs from what was typed.
   *
   * The modal owns its own dialog-scoped async (tag list + the bulk call) and
   * dispatches `applied` upward; the page stays the coordinator for gallery
   * data and decides whether the listing has to be re-queried.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import MetadataChips from '$components/ui/MetadataChips.svelte';
  import SearchableSelect from '$components/ui/SearchableSelect.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { bulkTagFiles, createTag, listTags, listTagsOnFiles } from '$lib/api/tags';
  import type { TagOnSelection } from '$lib/types/tag';
  import { getErrorMessage } from '$lib/utils/apiError';
  // The modal has to answer "which existing tag will this name resolve to?"
  // *before* it can name the tag it is about to remove — the remove path
  // deliberately never creates a tag, so it cannot ask the server.
  import { normalizeTagName } from '$lib/utils/tagName';
  import type { BulkTagAction, BulkTagActionResult } from '$lib/types/tag';

  export let isOpen = false;
  /** Which direction the Organize menu asked for. */
  export let action: BulkTagAction = 'add_tag';
  /** UUIDs of the selected files, in selection order. */
  export let fileUuids: string[] = [];
  /**
   * Tag names the selected files are known to carry, used to scope the *remove*
   * suggestions. The gallery listing does not carry per-file tags today, so this
   * is usually empty — see `scopedToSelection` for what happens then.
   */
  export let presentTagNames: string[] = [];

  /**
   * The tags the selection already carries, shown as chips.
   *
   * A single selected file gets the full editor — chips you can remove, plus
   * the input to add — matching the file detail page. Several files get
   * **add only**: removing a tag that sits on three of five is ambiguous in a
   * way adding never is, so the chips render read-only with an "on N of M"
   * count instead.
   */
  let currentTags: TagOnSelection[] = [];
  let currentLoading = false;

  $: isSingleFile = fileUuids.length === 1;

  async function loadCurrentTags() {
    if (fileUuids.length === 0) {
      currentTags = [];
      return;
    }
    currentLoading = true;
    try {
      currentTags = await listTagsOnFiles(fileUuids);
    } catch {
      // Non-fatal: the add path still works without the chips.
      currentTags = [];
    } finally {
      currentLoading = false;
    }
  }

  $: if (isOpen) loadCurrentTags();

  async function removeChip(tag: TagOnSelection) {
    if (!isSingleFile || busy) return;
    busy = true;
    try {
      const results = await bulkTagFiles(fileUuids, 'remove_tag', tag.name);
      await loadCurrentTags();
      // Same envelope the submit path reports, so the gallery's listener does
      // not need a second shape to understand.
      dispatch('applied', {
        action: 'remove_tag',
        name: tag.name,
        changed: results.filter((r) => r.outcome === 'removed').length,
        unchanged: results.filter((r) => r.outcome === 'not_present').length,
        failed: results.filter((r) => r.outcome === 'failed').length,
      });
    } finally {
      busy = false;
    }
  }

  const dispatch = createEventDispatcher<{
    applied: {
      action: BulkTagAction;
      name: string;
      changed: number;
      unchanged: number;
      failed: number;
    };
    close: void;
  }>();

  interface Summary {
    typedName: string;
    resolvedName: string;
    changed: number;
    unchanged: number;
    failed: number;
  }

  let tagName = '';
  let busy = false;
  let errorMessage = '';
  let summary: Summary | null = null;
  let allTagNames: string[] = [];
  let wasOpen = false;

  $: isAdd = action === 'add_tag';
  $: presentNames = Array.from(new Set(presentTagNames.filter((name) => name.trim() !== '')));
  // Removing a tag nothing in the selection carries is a guaranteed no-op, so the
  // remove suggestions are scoped to what the selection is known to carry. When
  // the selection carries no tag data at all, every tag is offered instead —
  // an empty picker would be worse than a wide one, and the hint says which.
  $: scopedToSelection = !isAdd && presentNames.length > 0;
  $: suggestionPool = scopedToSelection ? presentNames : allTagNames;
  $: canSubmit = tagName.trim() !== '' && fileUuids.length > 0 && !busy;

  // Reopening is the reset point: the page flips `action` before it opens the
  // modal, so a stale summary from the previous run must never survive.
  $: if (isOpen !== wasOpen) {
    wasOpen = isOpen;
    if (isOpen) void openReset();
  }

  async function openReset() {
    tagName = '';
    errorMessage = '';
    summary = null;
    busy = false;
    await loadTagNames();
  }

  async function loadTagNames() {
    try {
      allTagNames = (await listTags()).map((tag) => tag.name);
    } catch (err) {
      // Suggestions are a convenience: a failed list still leaves a usable
      // free-text field, so this must not block the action.
      console.error('[BulkTagModal] Could not load tags for suggestions:', err);
      allTagNames = [];
    }
  }

  /** Local, case- and separator-insensitive match over the offered names. */
  async function suggest(query: string): Promise<string[]> {
    const needle = normalizeTagName(query);
    const matches = needle
      ? suggestionPool.filter((name) => normalizeTagName(name).includes(needle))
      : suggestionPool;
    return matches.slice(0, 25);
  }

  function summarize(
    results: BulkTagActionResult[],
    typedName: string,
    resolvedName: string
  ): Summary {
    let changed = 0;
    let unchanged = 0;
    let failed = 0;
    for (const result of results) {
      if (!result.success || result.outcome === 'failed') {
        failed += 1;
      } else if (result.outcome === 'added' || result.outcome === 'removed') {
        changed += 1;
      } else {
        unchanged += 1;
      }
    }
    return { typedName, resolvedName, changed, unchanged, failed };
  }

  /**
   * Which tag the supplied name will actually be applied as.
   *
   * Add asks the server, because only the server can tell a new tag from an
   * existing one; remove matches the offered names locally, because asking would
   * mean creating the very tag the user wants gone.
   */
  async function resolveName(typed: string): Promise<string> {
    if (isAdd) return (await createTag(typed)).name;
    const normalized = normalizeTagName(typed);
    return suggestionPool.find((name) => normalizeTagName(name) === normalized) ?? typed;
  }

  async function submit() {
    const typed = tagName.trim();
    if (!canSubmit) return;

    busy = true;
    errorMessage = '';
    summary = null;
    try {
      const resolved = await resolveName(typed);
      const results = await bulkTagFiles(fileUuids, action, resolved);
      summary = summarize(results, typed, resolved);
      dispatch('applied', {
        action,
        name: resolved,
        changed: summary.changed,
        unchanged: summary.unchanged,
        failed: summary.failed,
      });
    } catch (err) {
      errorMessage = getErrorMessage(err, $t('gallery.bulk.tagModal.requestFailed'));
    } finally {
      busy = false;
    }
  }

  function close() {
    dispatch('close');
  }
</script>

<BaseModal
  {isOpen}
  allowOverflow
  maxWidth="480px"
  title={isAdd
    ? $t('gallery.bulk.tagModal.addTitle', { count: fileUuids.length })
    : $t('gallery.bulk.tagModal.removeTitle', { count: fileUuids.length })}
  onClose={close}
>
  <form class="bulk-tag-form" on:submit|preventDefault={submit}>
    <!-- What the selection already has. Chips are removable for a single file
         (the file detail page's editor, same shape) and read-only with an
         "on N of M" count for several, because removing across a mixed
         selection is ambiguous. -->
    {#if currentLoading}
      <div class="current-loading"><Spinner size="small" /></div>
    {:else if currentTags.length > 0}
      <div class="current-tags">
        <span class="current-label">
          {isSingleFile
            ? $t('gallery.bulk.tagModal.currentSingle')
            : $t('gallery.bulk.tagModal.currentMulti')}
        </span>
        <MetadataChips
          chips={currentTags.map((tag) => ({
            uuid: tag.uuid,
            name: tag.name,
            count: tag.file_count,
            total: tag.selection_size,
          }))}
          removable={isSingleFile}
          disabled={busy}
          removeLabel={(name) => $t('gallery.bulk.tagModal.removeChip', { name })}
          coverageLabel={(count, total) =>
            $t('gallery.bulk.tagModal.onCount', { count, total })}
          overflowLabel={(count) => $t('gallery.bulk.tagModal.moreTags', { count })}
          maxVisible={isSingleFile ? 40 : 10}
          on:remove={(event) => {
            const match = currentTags.find((tag) => tag.uuid === event.detail.uuid);
            if (match) removeChip(match);
          }}
        />
      </div>
    {/if}

    <!--
      The label wraps the combobox so it names the input inside `SearchableSelect`
      (which owns no id of its own) without reaching into that component.
    -->
    <label class="field">
      <span class="field-label">{$t('gallery.bulk.tagModal.label')}</span>
      <SearchableSelect
        bind:value={tagName}
        fetchFn={suggest}
        getLabel={(name) => String(name)}
        minChars={0}
        debounceMs={120}
        emptyLabel={$t('gallery.bulk.tagModal.noSuggestions')}
        placeholder={isAdd
          ? $t('gallery.bulk.tagModal.addPlaceholder')
          : $t('gallery.bulk.tagModal.removePlaceholder')}
        on:select={(event) => (tagName = String(event.detail))}
      />
    </label>

    <p class="hint">
      {#if isAdd}
        {$t('gallery.bulk.tagModal.addHint')}
      {:else if scopedToSelection}
        {$t('gallery.bulk.tagModal.removeHintScoped')}
      {:else}
        {$t('gallery.bulk.tagModal.removeHintAll')}
      {/if}
    </p>

    {#if errorMessage}
      <p class="error" role="alert">{errorMessage}</p>
    {/if}

    {#if summary}
      <section class="result" role="status" aria-live="polite">
        <h3 class="result-title">{$t('gallery.bulk.tagModal.resultTitle')}</h3>
        {#if summary.resolvedName !== summary.typedName}
          <p class="resolved">
            {$t('gallery.bulk.tagModal.resolved', {
              name: summary.resolvedName,
              typed: summary.typedName,
            })}
          </p>
        {/if}
        <ul class="result-list">
          <li>
            {isAdd
              ? $t('gallery.bulk.tagModal.addedCount', {
                  count: summary.changed,
                  name: summary.resolvedName,
                })
              : $t('gallery.bulk.tagModal.removedCount', {
                  count: summary.changed,
                  name: summary.resolvedName,
                })}
          </li>
          {#if summary.unchanged > 0}
            <li class="unchanged">
              {isAdd
                ? $t('gallery.bulk.tagModal.alreadyPresentCount', { count: summary.unchanged })
                : $t('gallery.bulk.tagModal.notPresentCount', { count: summary.unchanged })}
            </li>
          {/if}
          {#if summary.failed > 0}
            <li class="failed">
              {$t('gallery.bulk.tagModal.failedCount', { count: summary.failed })}
            </li>
          {/if}
        </ul>
      </section>
    {/if}
  </form>

  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-ghost" on:click={close} disabled={busy}>
      {summary ? $t('gallery.bulk.tagModal.done') : $t('gallery.bulk.tagModal.cancel')}
    </button>
    <button type="button" class="btn btn-primary" on:click={submit} disabled={!canSubmit}>
      {#if busy}
        <Spinner size="small" />
        <span>{$t('gallery.bulk.tagModal.applying')}</span>
      {:else}
        <span>
          {isAdd
            ? $t('gallery.bulk.tagModal.addSubmit')
            : $t('gallery.bulk.tagModal.removeSubmit')}
        </span>
      {/if}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .current-tags {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 4px;
  }

  .current-label {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-secondary);
  }







  .current-loading {
    padding: 2px 0;
  }

  .bulk-tag-form {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .field-label {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-color);
  }

  .hint {
    margin: 0;
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-secondary);
  }

  .error {
    margin: 0;
    padding: 8px 10px;
    border: 1px solid var(--error-color, #dc2626);
    border-radius: 8px;
    background: rgba(var(--error-color-rgb, 239, 68, 68), 0.08);
    font-size: 13px;
    color: var(--text-color);
  }

  .result {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px 12px;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background: var(--surface-color);
  }

  .result-title {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-color);
  }

  .resolved {
    margin: 0;
    font-size: 13px;
    color: var(--text-color);
  }

  .result-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
    color: var(--text-color);
  }

  .unchanged {
    color: var(--text-secondary);
  }

  .failed {
    color: var(--error-color, #dc2626);
  }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 14px;
    border-radius: 8px;
    border: 1px solid transparent;
    font-size: 13px;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
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
</style>
