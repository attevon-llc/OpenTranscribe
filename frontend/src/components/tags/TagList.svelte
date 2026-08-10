<script context="module" lang="ts">
  import type { Tag, TagCollisionCluster } from '$lib/types/tag';

  /**
   * One selectable tag as the manager renders it.
   *
   * Deliberately looser than `TagWithCount`: collision-cluster members arrive
   * without `awaiting_review` (the backend does not compute it for that view),
   * so the flag is optional rather than faked as `false`.
   */
  export interface TagListEntry extends Tag {
    usage_count: number;
    awaiting_review?: boolean;
  }

  /**
   * How a `select` event should change the coordinator's selection.
   *
   * `replace` — this row only. `toggle` — flip this row. `range` — the span
   * from the keyboard/pointer anchor. `group` — a whole collision cluster.
   * `add` — union (used by the "also similar" suggestions).
   */
  export type TagSelectMode = 'replace' | 'toggle' | 'range' | 'group' | 'add';

  export interface TagSelectDetail {
    mode: TagSelectMode;
    uuids: string[];
  }

  /**
   * i18n key for a tag's origin. Shared with the detail and bulk panes so the
   * three surfaces can never drift on what `auto_ai` is called.
   */
  export function tagOriginKey(source?: string | null): string {
    switch (source) {
      case 'manual':
        return 'tags.manager.origin.manual';
      case 'auto_ai':
        return 'tags.manager.origin.auto';
      case 'ai_accepted':
        return 'tags.manager.origin.aiAccepted';
      default:
        return 'tags.manager.origin.unknown';
    }
  }

  type Row =
    | { kind: 'cluster'; key: string; cluster: TagCollisionCluster; uuids: string[] }
    | { kind: 'tag'; key: string; entry: TagListEntry; suggestedSurvivor: boolean };

  interface ClusterBlock {
    cluster: TagCollisionCluster;
    headerIndex: number;
    memberIndexes: number[];
  }
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Badge from '$components/ui/Badge.svelte';
  import ExpandableSection from '$components/ui/ExpandableSection.svelte';

  /** Flat list of tags. Ignored when `clusters` is non-empty. */
  export let tags: TagListEntry[] = [];
  /** Collision clusters. Non-empty switches the list into cluster mode. */
  export let clusters: TagCollisionCluster[] = [];
  /** Currently selected tag uuids (owned by the route). */
  export let selectedUuids: string[] = [];
  /** Tag uuids with a mutation in flight — dimmed and non-interactive. */
  export let pendingUuids: string[] = [];
  /** Accessible name for the listbox(es). */
  export let label = '';

  const dispatch = createEventDispatcher<{ select: TagSelectDetail }>();

  let focusIndex = 0;
  let anchorIndex = 0;
  const rowEls: HTMLElement[] = [];

  $: selected = new Set(selectedUuids);
  $: pending = new Set(pendingUuids);
  $: clusterMode = clusters.length > 0;

  /**
   * Flatten the data into one ordered row list plus the per-cluster blocks the
   * markup iterates. Both come from a single pass so a row's global index (what
   * roving tabindex and Shift+Arrow ranges use) always matches where it renders.
   */
  function buildModel(
    flatTags: TagListEntry[],
    tagClusters: TagCollisionCluster[]
  ): { rows: Row[]; blocks: ClusterBlock[] } {
    const rows: Row[] = [];
    const blocks: ClusterBlock[] = [];

    if (tagClusters.length > 0) {
      for (const cluster of tagClusters) {
        const headerIndex = rows.length;
        rows.push({
          kind: 'cluster',
          key: `cluster:${cluster.normalized_name}`,
          cluster,
          uuids: cluster.members.map((member) => member.uuid),
        });
        const memberIndexes: number[] = [];
        for (const member of cluster.members) {
          memberIndexes.push(rows.length);
          rows.push({
            kind: 'tag',
            key: `${cluster.normalized_name}:${member.uuid}`,
            entry: {
              uuid: member.uuid,
              name: member.name,
              source: member.source,
              usage_count: member.usage_count,
            },
            suggestedSurvivor: member.suggested_survivor,
          });
        }
        blocks.push({ cluster, headerIndex, memberIndexes });
      }
      return { rows, blocks };
    }

    for (const tag of flatTags) {
      rows.push({ kind: 'tag', key: tag.uuid, entry: tag, suggestedSurvivor: false });
    }
    return { rows, blocks };
  }

  $: model = buildModel(tags, clusters);
  $: rows = model.rows;
  $: blocks = model.blocks;
  // Keep the roving tabindex on a row that still exists after a refetch.
  $: if (focusIndex > rows.length - 1) focusIndex = Math.max(0, rows.length - 1);

  function uuidsForRow(index: number): string[] {
    const row = rows[index];
    if (!row) return [];
    return row.kind === 'cluster' ? row.uuids : [row.entry.uuid];
  }

  function isRowSelected(index: number): boolean {
    const uuids = uuidsForRow(index);
    return uuids.length > 0 && uuids.every((uuid) => selected.has(uuid));
  }

  function isRowPending(index: number): boolean {
    return uuidsForRow(index).some((uuid) => pending.has(uuid));
  }

  function emit(mode: TagSelectMode, uuids: string[]) {
    if (uuids.length === 0) return;
    dispatch('select', { mode, uuids });
  }

  function activate(index: number, mode: TagSelectMode) {
    if (isRowPending(index)) return;
    emit(mode, uuidsForRow(index));
  }

  /** Every uuid between two row indexes, inclusive, clusters expanded. */
  function rangeUuids(from: number, to: number): string[] {
    const low = Math.min(from, to);
    const high = Math.max(from, to);
    const uuids: string[] = [];
    for (let i = low; i <= high; i++) uuids.push(...uuidsForRow(i));
    return [...new Set(uuids)];
  }

  function defaultMode(index: number): TagSelectMode {
    return rows[index]?.kind === 'cluster' ? 'group' : 'replace';
  }

  function toggleMode(index: number): TagSelectMode {
    return rows[index]?.kind === 'cluster' ? 'group' : 'toggle';
  }

  function onRowClick(event: MouseEvent, index: number) {
    focusIndex = index;
    if (event.shiftKey) {
      if (!isRowPending(index)) emit('range', rangeUuids(anchorIndex, index));
      return;
    }
    anchorIndex = index;
    if (event.ctrlKey || event.metaKey) activate(index, toggleMode(index));
    else activate(index, defaultMode(index));
  }

  function moveFocus(next: number, extend: boolean) {
    focusIndex = next;
    rowEls[next]?.focus();
    if (extend) emit('range', rangeUuids(anchorIndex, next));
  }

  function onRowKeydown(event: KeyboardEvent, index: number) {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        moveFocus(Math.min(index + 1, rows.length - 1), event.shiftKey);
        break;
      case 'ArrowUp':
        event.preventDefault();
        moveFocus(Math.max(index - 1, 0), event.shiftKey);
        break;
      case 'Home':
        event.preventDefault();
        moveFocus(0, event.shiftKey);
        break;
      case 'End':
        event.preventDefault();
        moveFocus(rows.length - 1, event.shiftKey);
        break;
      case ' ':
        event.preventDefault();
        anchorIndex = index;
        activate(index, toggleMode(index));
        break;
      case 'Enter':
        event.preventDefault();
        anchorIndex = index;
        activate(index, defaultMode(index));
        break;
      default:
        break;
    }
  }

  function similarityPercent(similarity: number): number {
    return Math.round(similarity * 100);
  }
</script>

<div class="tag-list-root">
  {#if clusterMode}
    {#each blocks as block (block.cluster.normalized_name)}
      <section class="cluster">
        <div
          class="cluster-rows"
          role="listbox"
          aria-multiselectable="true"
          aria-label={block.cluster.normalized_name}
        >
          {#each [block.headerIndex, ...block.memberIndexes] as index (rows[index].key)}
            {@const row = rows[index]}
            <div
              bind:this={rowEls[index]}
              class="tag-row"
              class:cluster-header={row.kind === 'cluster'}
              class:cluster-member={row.kind === 'tag'}
              class:is-selected={isRowSelected(index)}
              class:is-pending={isRowPending(index)}
              role="option"
              tabindex={index === focusIndex ? 0 : -1}
              aria-selected={isRowSelected(index)}
              aria-disabled={isRowPending(index)}
              on:click={(event) => onRowClick(event, index)}
              on:keydown={(event) => onRowKeydown(event, index)}
            >
              {#if row.kind === 'cluster'}
                <span class="row-main">
                  <span class="row-name">{row.cluster.normalized_name}</span>
                  <span class="row-meta">
                    {$t('tags.manager.cluster.memberCount', { count: row.cluster.members.length })}
                  </span>
                </span>
                <span class="row-hint">{$t('tags.manager.cluster.select')}</span>
              {:else}
                <span class="row-main">
                  <span class="row-name">{row.entry.name}</span>
                  <span class="row-meta">
                    <span class="row-usage"
                      >{$t('tags.manager.usage', { count: row.entry.usage_count })}</span
                    >
                    <span class="row-origin">{$t(tagOriginKey(row.entry.source))}</span>
                  </span>
                </span>
                {#if row.suggestedSurvivor}
                  <Badge variant="info">{$t('tags.manager.cluster.suggested')}</Badge>
                {/if}
              {/if}
            </div>
          {/each}
        </div>

        {#if block.cluster.suggestions.length > 0}
          <div class="cluster-suggestions">
            <ExpandableSection
              title={$t('tags.manager.cluster.alsoSimilar', {
                count: block.cluster.suggestions.length,
              })}
            >
              <ul class="suggestion-list">
                {#each block.cluster.suggestions as suggestion (suggestion.uuid)}
                  <li>
                    <button
                      type="button"
                      class="suggestion-button"
                      on:click={() => emit('add', [suggestion.uuid])}
                    >
                      <span class="row-name">{suggestion.name}</span>
                      <span class="row-meta">
                        <span class="row-usage"
                          >{$t('tags.manager.usage', { count: suggestion.usage_count })}</span
                        >
                        <span class="row-origin">{$t(tagOriginKey(suggestion.source))}</span>
                        <span class="row-similarity">
                          {$t('tags.manager.cluster.similarity', {
                            percent: similarityPercent(suggestion.similarity),
                          })}
                        </span>
                      </span>
                      <span class="suggestion-add"
                        >{$t('tags.manager.cluster.addSuggestion', { name: suggestion.name })}</span
                      >
                    </button>
                  </li>
                {/each}
              </ul>
            </ExpandableSection>
          </div>
        {/if}
      </section>
    {/each}
  {:else}
    <div class="flat-rows" role="listbox" aria-multiselectable="true" aria-label={label}>
      {#each rows as row, index (row.key)}
        <div
          bind:this={rowEls[index]}
          class="tag-row"
          class:is-selected={isRowSelected(index)}
          class:is-pending={isRowPending(index)}
          role="option"
          tabindex={index === focusIndex ? 0 : -1}
          aria-selected={isRowSelected(index)}
          aria-disabled={isRowPending(index)}
          on:click={(event) => onRowClick(event, index)}
          on:keydown={(event) => onRowKeydown(event, index)}
        >
          <span class="row-main">
            <span class="row-name">{row.kind === 'tag' ? row.entry.name : ''}</span>
            <span class="row-meta">
              {#if row.kind === 'tag'}
                <span class="row-usage"
                  >{$t('tags.manager.usage', { count: row.entry.usage_count })}</span
                >
                <span class="row-origin">{$t(tagOriginKey(row.entry.source))}</span>
              {/if}
            </span>
          </span>
          {#if row.kind === 'tag' && row.entry.is_shared}
            <Badge variant="info" title={$t('tags.manager.sharedTooltip')}>
              {$t('tags.manager.sharedBadge')}
            </Badge>
          {/if}
          {#if row.kind === 'tag' && row.entry.awaiting_review}
            <Badge variant="warning">{$t('tags.manager.awaitingReview')}</Badge>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  <div class="sr-only" role="status" aria-live="polite">
    {selectedUuids.length === 0
      ? $t('tags.manager.selectionNone')
      : $t('tags.manager.selectionCount', { count: selectedUuids.length })}
  </div>
</div>

<style>
  .tag-list-root {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .flat-rows,
  .cluster-rows {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .cluster {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background: var(--background-color);
  }

  .tag-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    cursor: pointer;
    transition:
      border-color 0.15s ease,
      background 0.15s ease;
  }

  .tag-row:hover {
    border-color: var(--primary-color);
  }

  .tag-row:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .tag-row.is-selected {
    border-color: var(--primary-color);
    background: rgba(var(--primary-color-rgb), 0.1);
  }

  .tag-row.is-pending {
    opacity: 0.5;
    pointer-events: none;
    cursor: default;
  }

  .tag-row.cluster-header {
    background: transparent;
    border-style: dashed;
    font-weight: 600;
  }

  .tag-row.cluster-member {
    margin-left: 20px;
  }

  .row-main {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .row-name {
    font-size: 14px;
    color: var(--text-color);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .row-hint {
    font-size: 12px;
    color: var(--text-secondary);
  }

  .cluster-suggestions {
    padding: 0 4px 0 20px;
  }

  .suggestion-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .suggestion-button {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    width: 100%;
    padding: 8px 10px;
    border: 1px dashed var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-color);
    text-align: left;
    cursor: pointer;
    box-shadow: none;
  }

  .suggestion-button:hover {
    border-color: var(--primary-color);
    transform: none;
    box-shadow: none;
  }

  .suggestion-button:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .suggestion-add {
    font-size: 11px;
    font-weight: 600;
    color: var(--primary-color);
  }

  @media (max-width: 768px) {
    .tag-row.cluster-member {
      margin-left: 10px;
    }
    .cluster-suggestions {
      padding-left: 10px;
    }
  }
</style>
