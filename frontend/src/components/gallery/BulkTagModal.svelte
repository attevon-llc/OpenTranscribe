<script context="module" lang="ts">
  let instance = 0;

  function nextId(prefix: string): string {
    instance += 1;
    return `${prefix}-${instance}`;
  }
</script>

<script lang="ts">
  /**
   * Adds or removes one tag across the gallery selection.
   *
   * `action` only seeds the direction each time the dialog opens; the Add /
   * Remove tabs switch it from then on. The server decides which tag a typed
   * name becomes, so the summary always names the tag actually applied, and a
   * batch reports per-file outcomes: an unchanged file is a successful no-op,
   * and one refused file never reads as a failed batch.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Tabs, { type TabItem } from '$components/ui/Tabs.svelte';
  import SearchableSelect from '$components/ui/SearchableSelect.svelte';
  import MetadataChips, { type MetadataChip } from '$components/ui/MetadataChips.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { bulkTagFiles, createTag, listTags, listTagsOnFiles } from '$lib/api/tags';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { normalizeTagName } from '$lib/utils/tagName';
  import type { BulkTagAction, BulkTagActionResult, TagOnSelection } from '$lib/types/tag';

  export let isOpen = false;
  /** The direction the dialog opens in. */
  export let action: BulkTagAction = 'add_tag';
  export let fileUuids: string[] = [];
  /** Names the selection is already known to carry; merged with what the dialog loads. */
  export let presentTagNames: string[] = [];

  interface Counts {
    changed: number;
    unchanged: number;
    failed: number;
  }

  interface Summary extends Counts {
    action: BulkTagAction;
    typed: string;
    name: string;
  }

  const dispatch = createEventDispatcher<{
    applied: { action: BulkTagAction; name: string } & Counts;
    close: void;
  }>();

  const MAX_SUGGESTIONS = 25;
  const formId = nextId('bulk-tag-form');

  let mode: BulkTagAction = action;
  let name = '';
  let busy = false;
  let error: string | null = null;
  let summary: Summary | null = null;
  let libraryNames: string[] = [];
  let selectionTags: TagOnSelection[] = [];
  let selectionLoading = false;
  let selectionRequest = 0;
  let wasOpen = false;

  $: handleOpenChange(isOpen);
  // Keyed by content: the page hands over a fresh array on every gallery update.
  $: selectionKey = isOpen ? fileUuids.join('\n') : '';
  $: void loadSelectionTags(selectionKey);

  $: adding = mode === 'add_tag';
  $: single = fileUuids.length === 1;
  $: presentNames = uniqueNames([...presentTagNames, ...selectionTags.map((tag) => tag.name)]);
  $: scoped = !adding && presentNames.length > 0;
  $: pool = scoped ? presentNames : libraryNames;
  $: canSubmit = name.trim() !== '' && fileUuids.length > 0 && !busy;

  $: tabs = [
    {
      id: 'add_tag',
      label: $t('gallery.bulk.addTag'),
      title: $t('gallery.bulk.addTagTooltip'),
      disabled: busy,
    },
    {
      id: 'remove_tag',
      label: $t('gallery.bulk.removeTag'),
      title: $t('gallery.bulk.removeTagTooltip'),
      disabled: busy,
    },
  ] satisfies TabItem[];

  $: chips = selectionTags.map(
    (tag): MetadataChip => ({
      uuid: tag.uuid,
      name: tag.name,
      count: tag.file_count,
      total: tag.selection_size,
    })
  );

  $: hint = adding
    ? $t('gallery.bulk.tagModal.addHint')
    : scoped
      ? $t('gallery.bulk.tagModal.removeHintScoped')
      : $t('gallery.bulk.tagModal.removeHintAll');

  function handleOpenChange(open: boolean) {
    if (open && !wasOpen) {
      mode = action;
      name = '';
      error = null;
      summary = null;
      busy = false;
      void loadLibraryNames();
    }
    wasOpen = open;
  }

  async function loadLibraryNames() {
    try {
      libraryNames = (await listTags()).map((tag) => tag.name);
    } catch (err: unknown) {
      // The free-text field still works without suggestions.
      console.error('[BulkTagModal] Failed to load tag names:', err);
      libraryNames = [];
    }
  }

  async function loadSelectionTags(key: string) {
    const request = ++selectionRequest;
    selectionTags = [];
    if (!key) {
      selectionLoading = false;
      return;
    }
    selectionLoading = true;
    try {
      const tags = await listTagsOnFiles(fileUuids);
      if (request === selectionRequest) selectionTags = tags;
    } catch {
      // Non-fatal: no chips, and remove offers every tag.
    } finally {
      if (request === selectionRequest) selectionLoading = false;
    }
  }

  function uniqueNames(names: string[]): string[] {
    return [...new Set(names.filter((candidate) => candidate.trim() !== ''))];
  }

  /** Async because TagInput's `suggest` contract is (it also serves server-side lookups). */
  function suggest(query: string): Promise<string[]> {
    const wanted = normalizeTagName(query);
    return Promise.resolve(
      pool
        .filter((candidate) => !wanted || normalizeTagName(candidate).includes(wanted))
        .slice(0, MAX_SUGGESTIONS)
    );
  }

  function tally(results: BulkTagActionResult[]): Counts {
    const counts: Counts = { changed: 0, unchanged: 0, failed: 0 };
    for (const result of results) {
      if (!result.success || result.outcome === 'failed') counts.failed++;
      else if (result.outcome === 'added' || result.outcome === 'removed') counts.changed++;
      else counts.unchanged++;
    }
    return counts;
  }

  /** Removing never creates a tag, so the name is matched against the pool locally. */
  function resolveRemoveName(typed: string): string {
    const wanted = normalizeTagName(typed);
    return pool.find((candidate) => normalizeTagName(candidate) === wanted) ?? typed;
  }

  function switchMode(event: CustomEvent<string>) {
    if (busy) return;
    const next: BulkTagAction = event.detail === 'remove_tag' ? 'remove_tag' : 'add_tag';
    if (next === mode) return;
    mode = next;
    summary = null;
    error = null;
  }

  async function submit() {
    const typed = name.trim();
    if (!typed || fileUuids.length === 0 || busy) return;
    const direction = mode;
    error = null;
    summary = null;
    busy = true;
    try {
      const resolved =
        direction === 'add_tag' ? (await createTag(typed)).name : resolveRemoveName(typed);
      const counts = tally(await bulkTagFiles(fileUuids, direction, resolved));
      summary = { action: direction, typed, name: resolved, ...counts };
      dispatch('applied', { action: direction, name: resolved, ...counts });
    } catch (err: unknown) {
      error = getErrorMessage(err, $t('gallery.bulk.tagModal.requestFailed'));
    } finally {
      busy = false;
    }
  }

  async function removeChip(event: CustomEvent<MetadataChip>) {
    const [onlyFile] = fileUuids;
    if (busy || !single || onlyFile === undefined) return;
    const chip = event.detail;
    busy = true;
    error = null;
    try {
      const counts = tally(await bulkTagFiles([onlyFile], 'remove_tag', chip.name));
      await loadSelectionTags(selectionKey);
      dispatch('applied', { action: 'remove_tag', name: chip.name, ...counts });
    } catch (err: unknown) {
      error = getErrorMessage(err, $t('gallery.bulk.tagModal.requestFailed'));
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal
  {isOpen}
  title={adding
    ? $t('gallery.bulk.tagModal.addTitle', { count: fileUuids.length })
    : $t('gallery.bulk.tagModal.removeTitle', { count: fileUuids.length })}
  size="sm"
  allowOverflow
  onClose={() => dispatch('close')}
>
  <form id={formId} class="bulk-tag-form" on:submit|preventDefault={submit}>
    <Tabs
      {tabs}
      activeId={mode}
      ariaLabel={$t('gallery.bulk.addOrEditTags')}
      on:change={switchMode}
    />

    {#if selectionLoading}
      <div class="selection-loading"><Spinner label={$t('common.loading')} size="small" /></div>
    {:else if chips.length > 0}
      <div class="current-tags">
        <span class="current-label">
          {single
            ? $t('gallery.bulk.tagModal.currentSingle')
            : $t('gallery.bulk.tagModal.currentMulti')}
        </span>
        <MetadataChips
          {chips}
          removable={single}
          disabled={busy}
          removeLabel={(chipName) => $t('gallery.bulk.tagModal.removeChip', { name: chipName })}
          coverageLabel={(count, total) => $t('gallery.bulk.tagModal.onCount', { count, total })}
          overflowLabel={(count) => $t('gallery.bulk.tagModal.moreTags', { count })}
          maxVisible={single ? 40 : 10}
          on:remove={removeChip}
        />
      </div>
    {/if}

    <label class="name-field">
      <span class="field-label">{$t('gallery.bulk.tagModal.label')}</span>
      <SearchableSelect
        bind:value={name}
        fetchFn={suggest}
        getLabel={(candidate) => candidate}
        minChars={0}
        debounceMs={120}
        emptyLabel={$t('gallery.bulk.tagModal.noSuggestions')}
        placeholder={adding
          ? $t('gallery.bulk.tagModal.addPlaceholder')
          : $t('gallery.bulk.tagModal.removePlaceholder')}
        on:select={(event) => (name = event.detail)}
      />
    </label>
    <p class="hint">{hint}</p>

    {#if error}
      <p class="error" role="alert">{error}</p>
    {/if}

    {#if summary}
      <section class="result" role="status" aria-live="polite">
        <h3>{$t('gallery.bulk.tagModal.resultTitle')}</h3>
        {#if summary.name !== summary.typed}
          <p>
            {$t('gallery.bulk.tagModal.resolved', { name: summary.name, typed: summary.typed })}
          </p>
        {/if}
        {#if summary.action === 'add_tag'}
          <p>
            {$t('gallery.bulk.tagModal.addedCount', { count: summary.changed, name: summary.name })}
          </p>
        {:else}
          <p>
            {$t('gallery.bulk.tagModal.removedCount', {
              count: summary.changed,
              name: summary.name,
            })}
          </p>
        {/if}
        {#if summary.unchanged > 0}
          <p>
            {summary.action === 'add_tag'
              ? $t('gallery.bulk.tagModal.alreadyPresentCount', { count: summary.unchanged })
              : $t('gallery.bulk.tagModal.notPresentCount', { count: summary.unchanged })}
          </p>
        {/if}
        {#if summary.failed > 0}
          <p class="failed">{$t('gallery.bulk.tagModal.failedCount', { count: summary.failed })}</p>
        {/if}
      </section>
    {/if}
  </form>

  <svelte:fragment slot="footer">
    <button
      type="button"
      class="btn btn-secondary btn-lg"
      disabled={busy}
      on:click={() => dispatch('close')}
    >
      {summary ? $t('gallery.bulk.tagModal.done') : $t('gallery.bulk.tagModal.cancel')}
    </button>
    <button
      type="submit"
      form={formId}
      class="btn btn-primary btn-lg"
      disabled={!canSubmit}
    >
      {#if busy}
        <Spinner size="small" />
        {$t('gallery.bulk.tagModal.applying')}
      {:else}
        {adding ? $t('gallery.bulk.tagModal.addSubmit') : $t('gallery.bulk.tagModal.removeSubmit')}
      {/if}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .bulk-tag-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    color: var(--text-color);
  }

  .current-tags {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .current-label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .field-label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .selection-loading {
    display: flex;
    justify-content: center;
  }

  .name-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .error {
    margin: 0;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--error-border);
    border-radius: var(--radius-md);
    background-color: var(--error-bg);
    color: var(--error-text);
    font-size: var(--font-size-sm);
  }

  .result {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding: var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background-color: var(--surface-secondary);
    font-size: var(--font-size-sm);
  }

  .result h3 {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
  }

  .result p {
    margin: 0;
  }

  .result .failed {
    color: var(--warning-text);
  }
</style>
