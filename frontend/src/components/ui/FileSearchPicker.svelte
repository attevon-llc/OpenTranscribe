<!--
  FileSearchPicker.svelte - type to find files by title or file name, select one or many, see
  them as chips. The administrator screens use it to choose what to hold or quarantine, so
  nobody has to find and paste a file's internal ID.

  Built from the shared `SearchableSelect` (the debounced combobox) and `Chip`, over
  `searchAdminFiles` (`$lib/api/fileSearch`): `scope="organization"` searches every file of the
  caller's organization, `scope="platform"` every tenant's, the same reach the hold routes
  accept. Each option says whose file it is, and whether it is in the trash, taken down or
  already held. The parent owns the selection (`bind:selected`) and maps it to uuids.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import Chip from '$components/ui/Chip.svelte';
  import SearchableSelect from '$components/ui/SearchableSelect.svelte';
  import { searchAdminFiles, type AdminFileHit, type AdminFileScope } from '$lib/api/fileSearch';
  import { formatDate } from '$lib/utils/formatting';
  import { locale, t } from '$stores/locale';

  /** Whose files to search: the caller's organization, or every tenant. */
  export let scope: AdminFileScope;
  /** The chosen files, in the order they were picked. Bindable. */
  export let selected: AdminFileHit[] = [];
  /** The most files the action accepts; the search is replaced by a note once reached. */
  export let max = Number.POSITIVE_INFINITY;
  /** Visible label of the search field. */
  export let label = '';
  /** Help under the field. */
  export let help = '';
  /** While the parent is submitting: chips stay visible, nothing can be added or removed. */
  export let disabled = false;

  const dispatch = createEventDispatcher<{ change: AdminFileHit[] }>();

  let query = '';

  $: chosen = new Set(selected.map((f) => f.uuid));
  $: full = selected.length >= max;

  function nameOf(file: AdminFileHit): string {
    return file.title || file.filename;
  }

  /** Enough to tell two files apart: name, file name, owner, organization, date, and its state. */
  function optionLabel(file: AdminFileHit): string {
    const parts = [nameOf(file)];
    if (file.title && file.title !== file.filename) parts.push(file.filename);
    if (file.owner) parts.push(file.owner);
    if (file.organization_name) parts.push(file.organization_name);
    if (file.upload_time) parts.push(formatDate(file.upload_time, $locale));
    if (file.availability === 'trashed') parts.push($t('filePicker.inTrash'));
    if (file.availability === 'quarantined') parts.push($t('filePicker.quarantined'));
    if (file.hold?.held) parts.push($t('filePicker.held'));
    return parts.join(' · ');
  }

  async function search(q: string): Promise<AdminFileHit[]> {
    const found = await searchAdminFiles(scope, q);
    return found.filter((f) => !chosen.has(f.uuid));
  }

  function update(next: AdminFileHit[]) {
    selected = next;
    dispatch('change', next);
  }

  function add(event: CustomEvent<AdminFileHit>) {
    query = '';
    if (disabled || full || chosen.has(event.detail.uuid)) return;
    update([...selected, event.detail]);
  }

  function remove(uuid: string) {
    if (disabled) return;
    update(selected.filter((f) => f.uuid !== uuid));
  }
</script>

<div class="file-search-picker" data-testid="file-search-picker">
  {#if !disabled && !full}
    <label class="field">
      {#if label}<span class="field-label">{label}</span>{/if}
      <SearchableSelect
        bind:value={query}
        placeholder={$t('filePicker.placeholder')}
        emptyLabel={$t('filePicker.empty')}
        fetchFn={search}
        getLabel={optionLabel}
        on:select={add}
      />
    </label>
    {#if help}<small class="help">{help}</small>{/if}
  {:else if full}
    {#if label}<span class="field-label">{label}</span>{/if}
    <small class="help" role="status">{$t('filePicker.max', { max })}</small>
  {:else if label}
    <span class="field-label">{label}</span>
  {/if}

  {#if selected.length > 0}
    <div class="selection">
      <div class="selection-head">
        <span class="count" aria-live="polite">{$t('filePicker.selected', { count: selected.length })}</span>
        {#if !disabled}
          <button type="button" class="clear" on:click={() => update([])}>
            {$t('filePicker.clear')}
          </button>
        {/if}
      </div>
      <ul class="chips" data-testid="file-search-picker-chips">
        {#each selected as file (file.uuid)}
          <li>
            <Chip
              removable={!disabled}
              removeLabel={$t('filePicker.remove', { name: nameOf(file) })}
              on:remove={() => remove(file.uuid)}
            >
              <span class="chip-name" dir="auto" title={nameOf(file)}>{nameOf(file)}</span>
            </Chip>
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</div>

<style>
  .file-search-picker {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .field-label {
    font-weight: var(--font-weight-semibold);
  }

  .help {
    color: var(--text-secondary);
  }

  .selection {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
    margin-block-start: var(--space-1);
  }

  .selection-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .count {
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .clear {
    background: none;
    border: none;
    padding: 0;
    color: var(--primary-on-surface);
    font-size: var(--font-size-sm);
    cursor: pointer;
  }

  .clear:hover {
    text-decoration: underline;
  }

  .chips {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
    max-height: 9rem;
    overflow-y: auto;
  }

  .chip-name {
    display: inline-block;
    max-width: 18rem;
    overflow: hidden;
    text-overflow: ellipsis;
    vertical-align: bottom;
  }
</style>
