<script lang="ts">
  /**
   * Knowledge-panel-style facts for one transcript hit: the metadata the compact result
   * card deliberately leaves out (docs/design/search_results_ux_audit.md). `panel` is the
   * sticky aside beside the results column on wide screens; `inline` is the same content
   * expanded inside the card on narrow/touch layouts, so it omits the title the card shows.
   */
  import type { SearchHit } from '$stores/search';
  import { t } from '$stores/locale';
  import { formatClock, formatDate, formatLanguageNames } from '$lib/utils/formatting';
  import { formatFileSize } from '$lib/utils/metadataMapper';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';

  export let hit: SearchHit;
  export let variant: 'panel' | 'inline' = 'panel';
  export let id: string | undefined = undefined;

  const MAX_SPEAKERS = 8;

  $: isVideo = hit.content_type?.startsWith('video/') ?? false;
  $: speakers = hit.speakers ?? [];
  $: visibleSpeakers = speakers.slice(0, MAX_SPEAKERS);
  $: hiddenSpeakers = speakers.length - visibleSpeakers.length;
  $: tags = hit.tags ?? [];
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
</script>

<section class="info-card {variant}" {id} aria-label={$t('search.resultDetails')}>
  {#if variant === 'panel'}
    <h3 class="info-title" dir="auto">
      {#if hit.title_highlighted}{@html sanitizeHighlightHtml(hit.title_highlighted)}{:else}{hit.title}{/if}
    </h3>
    <p class="info-kind">{isVideo ? $t('common.video') : $t('common.audio')}</p>
  {/if}

  <dl class="facts">
    {#if hit.duration > 0}
      <div class="fact"><dt>{$t('common.duration')}</dt><dd>{formatClock(hit.duration)}</dd></div>
    {/if}
    {#if hit.upload_time}
      <div class="fact"><dt>{$t('search.infoUploaded')}</dt><dd>{formatDate(hit.upload_time)}</dd></div>
    {/if}
    {#if hit.file_size > 0}
      <div class="fact"><dt>{$t('common.size')}</dt><dd>{formatFileSize(hit.file_size)}</dd></div>
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
        <li class="chip tag" dir="auto">{tag}</li>
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

  .chip {
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

  .chip.tag {
    border-radius: 4px;
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
