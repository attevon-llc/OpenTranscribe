<script lang="ts">
  /**
   * Knowledge-panel-style facts for one transcript hit: the metadata the compact result
   * card deliberately leaves out (docs/design/search_results_ux_audit.md). `panel` is the
   * sticky aside beside the results column on wide screens; `inline` is the same content
   * expanded inside the card on narrow/touch layouts, so it omits the title the card shows.
   *
   * Tag and collection chips are links to the same search filtered by that tag or
   * collection; a plain click dispatches `filterTag` / `filterCollection` so the page applies
   * the filter in place (keeping the other filters), a modified click opens the link.
   * Collections arrive already filtered to the ones the caller may see (`/api/search`).
   */
  import { createEventDispatcher } from 'svelte';
  import type { SearchHit } from '$stores/search';
  import { t } from '$stores/locale';
  import { formatClock, formatDate, formatLanguageNames } from '$lib/utils/formatting';
  import { formatFileSize } from '$lib/utils/metadataMapper';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import MenuIcon from '$components/ui/MenuIcon.svelte';

  export let hit: SearchHit;
  export let variant: 'panel' | 'inline' = 'panel';
  export let id: string | undefined = undefined;
  /** The current query, for the chips' filtered-search links. */
  export let query = '';
  /** Panel only: the hit is pinned, so the panel shows a pinned header with an Unpin control. */
  export let pinned = false;

  const dispatch = createEventDispatcher<{
    filterTag: string;
    filterCollection: string;
    unpin: string;
  }>();

  const MAX_SPEAKERS = 8;

  $: isVideo = hit.content_type?.startsWith('video/') ?? false;
  $: speakers = hit.speakers ?? [];
  $: visibleSpeakers = speakers.slice(0, MAX_SPEAKERS);
  $: hiddenSpeakers = speakers.length - visibleSpeakers.length;
  $: tags = hit.tags ?? [];
  $: collections = hit.collections ?? [];
  $: sources = (hit.match_sources ?? []).filter((s) => s !== 'semantic');
  // One text run joined here: separators written as `<span> · </span>` lost their spaces
  // (Svelte trims whitespace at a tag's edges) and rendered "7 keyword ·88% match".
  $: matchSummary = [
    hit.keyword_occurrences > 0 ? `${hit.keyword_occurrences} ${$t('search.keywordLabel')}` : '',
    hit.semantic_occurrences > 0 ? `${hit.semantic_occurrences} ${$t('search.semanticLabel')}` : '',
    hit.relevance_percent > 0 ? $t('search.relevanceShort', { percent: hit.relevance_percent }) : '',
  ]
    .filter(Boolean)
    .join(' · ');

  function sourceLabel(source: string): string {
    switch (source) {
      case 'content':
        return $t('search.contentMatch');
      case 'title':
        return $t('search.titleMatch');
      case 'speaker':
        return $t('search.speakerMatch');
      case 'metadata_speaker':
        return $t('search.metadataSpeakerMatch');
      default:
        return source;
    }
  }

  function searchHref(param: string, value: string): string {
    const q = query ? `q=${encodeURIComponent(query)}&` : '';
    return `/search?${q}${param}=${encodeURIComponent(value)}`;
  }

  // A modified or non-primary click keeps the browser's own link behaviour (new tab/window).
  function applyInPlace(event: MouseEvent, apply: () => void) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    apply();
  }
</script>

<section class="info-card {variant}" class:pinned={variant === 'panel' && pinned} {id} aria-label={$t('search.resultDetails')}>
  {#if variant === 'panel' && pinned}
    <div class="pinned-row">
      <span class="pinned-label"><MenuIcon name="pin" size={13} />{$t('search.pinnedLabel')}</span>
      <button
        type="button"
        class="unpin-btn"
        aria-label={$t('search.unpinDetails')}
        title={$t('search.unpinDetails')}
        on:click={() => dispatch('unpin', hit.file_uuid)}
      >
        <MenuIcon name="close" size={14} />
      </button>
    </div>
  {/if}
  {#if variant === 'panel'}
    <h3 class="info-title" dir="auto">
      {#if hit.title_highlighted}{@html sanitizeHighlightHtml(hit.title_highlighted)}{:else}{hit.title}{/if}
    </h3>
    <p class="info-kind">{isVideo ? $t('common.video') : $t('common.audio')}</p>
  {/if}

  <dl class="facts">
    {#if hit.duration > 0}
      <div class="fact"><dt>{$t('common.duration')}</dt><dd><bdi>{formatClock(hit.duration)}</bdi></dd></div>
    {/if}
    {#if hit.upload_time}
      <div class="fact"><dt>{$t('search.infoUploaded')}</dt><dd><bdi>{formatDate(hit.upload_time)}</bdi></dd></div>
    {/if}
    {#if hit.file_size > 0}
      <div class="fact"><dt>{$t('common.size')}</dt><dd><bdi>{formatFileSize(hit.file_size)}</bdi></dd></div>
    {/if}
    {#if hit.language}
      <div class="fact"><dt>{$t('filter.language')}</dt><dd>{formatLanguageNames([hit.language])}</dd></div>
    {/if}
    <div class="fact">
      <dt>{$t('search.infoMatches')}</dt>
      <dd>
        {matchSummary}
        {#if hit.total_occurrences > hit.occurrences.length}
          <span class="sub">{$t('search.showingOf', { shown: hit.occurrences.length, total: hit.total_occurrences })}</span>
        {/if}
      </dd>
    </div>
    {#if sources.length > 0}
      <div class="fact">
        <dt>{$t('search.infoMatchedIn')}</dt>
        <dd>{sources.map(sourceLabel).join(', ')}</dd>
      </div>
    {/if}
  </dl>

  {#if speakers.length > 0}
    <h4 class="group-label">{$t('filter.speakers')}</h4>
    <ul class="chips">
      {#each visibleSpeakers as speaker (speaker)}
        <li class="chip" dir="auto">{speaker}</li>
      {/each}
      {#if hiddenSpeakers > 0}
        <li class="chip more">{$t('search.moreCount', { count: hiddenSpeakers })}</li>
      {/if}
    </ul>
  {/if}

  {#if tags.length > 0}
    <h4 class="group-label">{$t('filter.tags')}</h4>
    <ul class="chips">
      {#each tags as tag (tag)}
        <li>
          <a
            class="chip chip-link tag"
            href={searchHref('tags', tag)}
            title={$t('search.filterByTag', { name: tag })}
            on:click={(e) => applyInPlace(e, () => dispatch('filterTag', tag))}
          >
            <MenuIcon name="tag" size={12} /><span dir="auto">{tag}</span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}

  {#if collections.length > 0}
    <h4 class="group-label">{$t('filter.collections')}</h4>
    <ul class="chips">
      {#each collections as collection (collection.uuid)}
        <li>
          <a
            class="chip chip-link collection"
            href={searchHref('collection', collection.uuid)}
            title={$t('search.filterByCollection', { name: collection.name })}
            on:click={(e) => applyInPlace(e, () => dispatch('filterCollection', collection.uuid))}
          >
            <MenuIcon name="folder" size={12} /><span dir="auto">{collection.name}</span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}

  {#if variant === 'panel'}
    <a class="open-file" href="/files/{hit.file_uuid}">{$t('search.openFile')}</a>
  {/if}
</section>

<style>
  .info-card {
    color: var(--text-color);
    font-size: var(--font-size-sm);
  }

  .info-card.panel {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 1rem 1.125rem;
    transition: border-color 0.15s;
  }

  .info-card.panel.pinned {
    border-color: var(--primary-color);
    box-shadow: 0 0 0 1px var(--primary-color);
  }

  .pinned-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin: -0.25rem 0 0.5rem;
  }

  .pinned-label {
    display: inline-flex;
    align-items: center;
    gap: 0.3125rem;
    color: var(--primary-on-surface);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
  }

  .pinned-label :global(.menu-icon) {
    fill: currentColor;
  }

  .unpin-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: transparent;
    box-shadow: none;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .unpin-btn:hover {
    background: var(--hover-color);
    color: var(--text-color);
    box-shadow: none;
    transform: none;
  }

  .unpin-btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .info-card.inline {
    margin-top: 0.5rem;
    padding-block-start: 0.625rem;
    border-block-start: 1px solid var(--border-color);
  }

  .info-title {
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: var(--font-weight-medium);
    line-height: var(--line-height-tight);
    overflow-wrap: break-word;
  }

  .info-kind {
    margin: 0.125rem 0 0.75rem;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .facts {
    margin: 0;
    display: grid;
    gap: 0.375rem;
  }

  .fact {
    display: grid;
    grid-template-columns: minmax(5.5rem, max-content) 1fr;
    gap: 0.75rem;
  }

  .fact dt {
    color: var(--text-secondary);
    font-weight: var(--font-weight-medium);
  }

  /* Numeric values sit in a <bdi>: in the RTL UI "115.0 MB" otherwise reorders to
     "MB 115.0". */
  .fact dd {
    margin: 0;
    overflow-wrap: break-word;
  }

  .sub {
    display: block;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .group-label {
    margin: 0.875rem 0 0.375rem;
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .chips {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
  }

  .chips > li {
    max-width: 100%;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    max-width: 100%;
    padding: 0.0625rem 0.5rem;
    border-radius: 999px;
    background: var(--hover-color);
    border: 1px solid var(--border-color);
    font-size: var(--font-size-xs);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chip.tag,
  .chip.collection {
    border-radius: 4px;
  }

  .chip-link {
    color: var(--text-color);
    text-decoration: none;
  }

  .chip-link > span {
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .chip-link :global(.menu-icon) {
    color: var(--text-secondary);
  }

  .chip-link:hover {
    border-color: var(--primary-color);
    color: var(--primary-on-surface);
  }

  .chip-link:hover :global(.menu-icon) {
    color: currentColor;
  }

  .chip-link:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  /* Inline, the card sits on the result's hover/focus fill, which IS --hover-color (and, in
     dark mode, the border colour too), so the chips vanished; lift them onto the surface. */
  .info-card.inline .chip:not(.more) {
    background: var(--surface-color);
  }

  .chip.more {
    background: transparent;
    color: var(--text-secondary);
  }

  .open-file {
    display: inline-block;
    margin-top: 1rem;
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-medium);
    text-decoration: none;
  }

  .open-file:hover {
    text-decoration: underline;
  }

  .open-file:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 2px;
  }
</style>
