<script context="module" lang="ts">
  import type { Tag } from '$lib/types/tag';

  /** A row of the tag manager: a tag plus how many files carry it. */
  export interface TagListEntry extends Tag {
    usage_count: number;
  }

  /**
   * How a `select` event should change the coordinator's selection:
   * `replace` / `range` set it to exactly `uuids`, `toggle` flips each,
   * `group` adds all unless all are selected (then removes all), `add` unions.
   */
  export type TagSelectMode = 'replace' | 'toggle' | 'range' | 'group' | 'add';

  export interface TagSelectDetail {
    mode: TagSelectMode;
    uuids: string[];
  }

  const ORIGIN_KEYS: Record<string, string> = {
    manual: 'tags.manager.origin.manual',
    auto_ai: 'tags.manager.origin.auto',
    ai_accepted: 'tags.manager.origin.aiAccepted',
  };

  /** The i18n key naming where a tag came from. */
  export function tagOriginKey(source?: string | null): string {
    return (source && ORIGIN_KEYS[source]) || 'tags.manager.origin.unknown';
  }
</script>

<script lang="ts">
  /**
   * The tag manager's listbox, flat or grouped into collision clusters.
   *
   * Presentational: it owns focus and the range anchor, never the selection.
   * Every pointer or keyboard action is sent up as a `select` intent and the
   * coordinator answers with new `selectedUuids`.
   */
  import { createEventDispatcher, tick } from 'svelte';
  import { t } from '$stores/locale';
  import Badge from '$components/ui/Badge.svelte';
  import ExpandableSection from '$components/ui/ExpandableSection.svelte';
  import type { TagCollisionCluster } from '$lib/types/tag';

  export let tags: TagListEntry[] = [];
  /** A non-empty list switches to cluster mode; `tags` is then ignored. */
  export let clusters: TagCollisionCluster[] = [];
  export let selectedUuids: string[] = [];
  /** Tags with a mutation in flight: dimmed and inert. */
  export let pendingUuids: string[] = [];
  /** Accessible name of the flat listbox. */
  export let label = '';

  const dispatch = createEventDispatcher<{ select: TagSelectDetail }>();

  /** One focusable row. A cluster header stands for every member of its cluster. */
  interface Row {
    key: string;
    header: boolean;
    uuids: string[];
  }

  /** Where each cluster's header sits in the shared row sequence. */
  interface Section {
    cluster: TagCollisionCluster;
    start: number;
  }

  $: clustered = clusters.length > 0;

  $: sections = clusters.reduce<Section[]>((acc, cluster) => {
    const previous = acc[acc.length - 1];
    const start = previous ? previous.start + previous.cluster.members.length + 1 : 0;
    acc.push({ cluster, start });
    return acc;
  }, []);

  $: rows = clustered
    ? clusters.flatMap((cluster): Row[] => [
        {
          key: `cluster:${cluster.normalized_name}`,
          header: true,
          uuids: cluster.members.map((member) => member.uuid),
        },
        ...cluster.members.map((member) => ({
          key: `member:${member.uuid}`,
          header: false,
          uuids: [member.uuid],
        })),
      ])
    : tags.map((tag) => ({ key: `tag:${tag.uuid}`, header: false, uuids: [tag.uuid] }));

  // Per-row state is computed here, not by a helper called from the markup: in
  // legacy mode a function call in the template is not tracked, so the rows
  // would keep their first-render state when the props change.
  $: selectedSet = new Set(selectedUuids);
  $: pendingSet = new Set(pendingUuids);
  $: rowSelected = rows.map(
    (row) => row.uuids.length > 0 && row.uuids.every((uuid) => selectedSet.has(uuid))
  );
  $: rowPending = rows.map((row) => row.uuids.some((uuid) => pendingSet.has(uuid)));

  let focusIndex = 0;
  let anchorIndex: number | null = null;
  const rowEls: HTMLElement[] = [];

  // A refetch can shorten the list under the focused row.
  $: if (focusIndex > Math.max(0, rows.length - 1)) focusIndex = Math.max(0, rows.length - 1);

  function emit(mode: TagSelectMode, uuids: string[]) {
    if (uuids.length === 0) return;
    dispatch('select', { mode, uuids });
  }

  function rangeUuids(from: number, to: number): string[] {
    const [low, high] = from <= to ? [from, to] : [to, from];
    const spanned = rows.slice(low, high + 1).flatMap((row) => row.uuids);
    return spanned.filter((uuid, index) => spanned.indexOf(uuid) === index);
  }

  function activate(index: number, mode: 'replace' | 'toggle') {
    const row = rows[index];
    if (!row || rowPending[index]) return;
    anchorIndex = index;
    emit(row.header ? 'group' : mode, row.uuids);
  }

  function extendTo(index: number) {
    if (rowPending[index]) return;
    emit('range', rangeUuids(anchorIndex ?? index, index));
  }

  function handleClick(event: MouseEvent, index: number) {
    if (rowPending[index]) return;
    focusIndex = index;
    if (event.shiftKey) extendTo(index);
    else activate(index, event.ctrlKey || event.metaKey ? 'toggle' : 'replace');
  }

  async function moveFocus(from: number, to: number, extend: boolean) {
    const target = Math.min(Math.max(to, 0), rows.length - 1);
    if (target < 0) return;
    if (extend) {
      if (anchorIndex === null) anchorIndex = from;
      extendTo(target);
    }
    focusIndex = target;
    await tick();
    rowEls[target]?.focus();
  }

  function handleKeydown(event: KeyboardEvent, index: number) {
    const last = rows.length - 1;
    const targets: Record<string, number> = {
      ArrowDown: index + 1,
      ArrowUp: index - 1,
      Home: 0,
      End: last,
    };
    if (event.key in targets) {
      event.preventDefault();
      void moveFocus(index, targets[event.key], event.shiftKey);
    } else if (event.key === ' ' || event.key === 'Enter') {
      event.preventDefault();
      focusIndex = index;
      activate(index, event.key === ' ' ? 'toggle' : 'replace');
    }
  }

  function addSuggestion(uuid: string) {
    if (pendingSet.has(uuid)) return;
    emit('add', [uuid]);
  }
</script>

<div class="tag-list">
  {#if clustered}
    {#each sections as section (section.cluster.normalized_name)}
      {@const cluster = section.cluster}
      <section class="cluster">
        <div
          class="listbox"
          role="listbox"
          aria-multiselectable="true"
          aria-label={cluster.normalized_name}
        >
          <div
            bind:this={rowEls[section.start]}
            class="row cluster-header"
            class:is-selected={rowSelected[section.start]}
            class:is-pending={rowPending[section.start]}
            role="option"
            aria-selected={rowSelected[section.start] ?? false}
            aria-disabled={rowPending[section.start] ?? false}
            tabindex={focusIndex === section.start ? 0 : -1}
            on:click={(event) => handleClick(event, section.start)}
            on:keydown={(event) => handleKeydown(event, section.start)}
          >
            <span class="row-name">{cluster.normalized_name}</span>
            <span class="tag-meta">
              <span>
                {$t('tags.manager.cluster.memberCount', { count: cluster.members.length })}
              </span>
              <span aria-hidden="true">·</span>
              <span>{$t('tags.manager.cluster.select')}</span>
            </span>
          </div>
          {#each cluster.members as member, offset (member.uuid)}
            {@const index = section.start + offset + 1}
            <div
              bind:this={rowEls[index]}
              class="row cluster-member"
              class:is-selected={rowSelected[index]}
              class:is-pending={rowPending[index]}
              role="option"
              aria-selected={rowSelected[index] ?? false}
              aria-disabled={rowPending[index] ?? false}
              tabindex={focusIndex === index ? 0 : -1}
              on:click={(event) => handleClick(event, index)}
              on:keydown={(event) => handleKeydown(event, index)}
            >
              <span class="row-head">
                <span class="row-name">{member.name}</span>
                {#if member.suggested_survivor}
                  <Badge variant="info">{$t('tags.manager.cluster.suggested')}</Badge>
                {/if}
                {#if member.ownership === 'system'}
                  <Badge variant="info" title={$t('tags.manager.sharedTooltip')}>
                    {$t('tags.manager.sharedBadge')}
                  </Badge>
                {:else if member.ownership === 'shared_with_me'}
                  <Badge variant="default" title={$t('tags.manager.sharedWithMeTooltip')}>
                    {$t('tags.manager.sharedWithMeBadge')}
                  </Badge>
                {/if}
              </span>
              <span class="tag-meta">
                <span>{$t('tags.manager.usage', { count: member.usage_count })}</span>
                <span aria-hidden="true">·</span>
                <span>{$t(tagOriginKey(member.source))}</span>
              </span>
            </div>
          {/each}
        </div>
        {#if cluster.suggestions.length > 0}
          <div class="suggestions">
            <ExpandableSection
              title={$t('tags.manager.cluster.alsoSimilar', { count: cluster.suggestions.length })}
            >
              <ul class="suggestion-list">
                {#each cluster.suggestions as suggestion (suggestion.uuid)}
                  <li>
                    <button
                      type="button"
                      class="suggestion"
                      disabled={pendingSet.has(suggestion.uuid)}
                      on:click={() => addSuggestion(suggestion.uuid)}
                    >
                      <span class="row-head">
                        <span class="row-name">{suggestion.name}</span>
                        <span class="similarity">
                          {$t('tags.manager.cluster.similarity', {
                            percent: Math.round(suggestion.similarity * 100),
                          })}
                        </span>
                      </span>
                      <span class="tag-meta">
                        <span>{$t('tags.manager.usage', { count: suggestion.usage_count })}</span>
                        <span aria-hidden="true">·</span>
                        <span>{$t(tagOriginKey(suggestion.source))}</span>
                      </span>
                      <span class="suggestion-action">
                        {$t('tags.manager.cluster.addSuggestion', { name: suggestion.name })}
                      </span>
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
    <div class="listbox" role="listbox" aria-multiselectable="true" aria-label={label}>
      {#each tags as tag, index (tag.uuid)}
        <div
          bind:this={rowEls[index]}
          class="row"
          class:is-selected={rowSelected[index]}
          class:is-pending={rowPending[index]}
          role="option"
          aria-selected={rowSelected[index] ?? false}
          aria-disabled={rowPending[index] ?? false}
          tabindex={focusIndex === index ? 0 : -1}
          on:click={(event) => handleClick(event, index)}
          on:keydown={(event) => handleKeydown(event, index)}
        >
          <span class="row-head">
            <span class="row-name">{tag.name}</span>
            {#if tag.ownership === 'system'}
              <Badge variant="info" title={$t('tags.manager.sharedTooltip')}>
                {$t('tags.manager.sharedBadge')}
              </Badge>
            {:else if tag.ownership === 'shared_with_me'}
              <Badge variant="default" title={$t('tags.manager.sharedWithMeTooltip')}>
                {$t('tags.manager.sharedWithMeBadge')}
              </Badge>
            {/if}
          </span>
          <span class="tag-meta">
            <span>{$t('tags.manager.usage', { count: tag.usage_count })}</span>
            <span aria-hidden="true">·</span>
            <span>{$t(tagOriginKey(tag.source))}</span>
          </span>
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
  .tag-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .listbox {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
  }

  .cluster {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding-block-end: var(--space-2);
    border-block-end: 1px solid var(--border-color);
  }

  .row {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    padding: var(--space-2) var(--space-3);
    border: 1px solid transparent;
    border-radius: var(--radius-md);
    color: var(--text-color);
    cursor: pointer;
    user-select: none;
    transition: background-color var(--duration-fast) ease;
  }

  .row:hover {
    background-color: var(--hover-color);
  }

  .row:focus-visible,
  .suggestion:focus-visible {
    outline: 2px solid var(--focus-ring-color);
    outline-offset: 1px;
  }

  .row.is-selected {
    background-color: rgba(var(--primary-color-rgb), 0.12);
    border-color: rgba(var(--primary-color-rgb), 0.4);
  }

  .row.is-pending {
    opacity: 0.5;
    pointer-events: none;
  }

  .cluster-header {
    background-color: var(--surface-secondary);
  }

  .cluster-header .row-name {
    font-weight: var(--font-weight-semibold);
  }

  .cluster-member {
    margin-inline-start: var(--space-6);
  }

  .row-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1-5);
    min-width: 0;
  }

  .row-name {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    overflow-wrap: anywhere;
  }

  .tag-meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .suggestions {
    margin-inline-start: var(--space-6);
  }

  .suggestion-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .suggestion {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-0-5);
    width: 100%;
    padding-block: var(--space-2);
    padding-inline: var(--space-3);
    border: 1px dashed var(--border-color);
    border-radius: var(--radius-md);
    background-color: var(--surface-color);
    color: var(--text-color);
    text-align: start;
    box-shadow: none;
  }

  .similarity {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .suggestion-action {
    font-size: var(--font-size-xs);
    color: var(--primary-on-surface);
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

  @media (max-width: 768px) {
    .cluster-member,
    .suggestions {
      margin-inline-start: var(--space-3);
    }
  }
</style>
