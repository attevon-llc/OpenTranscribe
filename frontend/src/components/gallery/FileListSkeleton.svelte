<script lang="ts">
  import './file-list-grid.css';
  import '../ui/skeleton-shimmer.css';
  import { t } from '$stores/locale';

  /**
   * Loading placeholder for the gallery LIST view.
   *
   * The generic `ui/ListRowSkeleton` (round avatar, title, two action buttons)
   * was standing in for this, and it describes a different shape entirely — the
   * list view is a seven-column table. The placeholder implied a layout that
   * never arrived, so the page visibly rearranged itself the moment data landed.
   *
   * The column headers are rendered for real rather than as grey bars: they are
   * known before any file loads, so there is nothing to guess, and showing them
   * is what makes this read as "the list, still filling in" instead of "some
   * other component". Only the cells that depend on data are placeholders.
   *
   * Column widths come from `file-list-grid.css`, the same declaration
   * `VirtualList` uses, so the two cannot drift apart.
   */

  /** Number of placeholder rows. */
  export let count = 8;
  /** Mirrors the table's selection mode, which adds a leading checkbox column. */
  export let isSelecting = false;

  $: rows = Array.from({ length: count });

  /** Deterministic width variation, so rows read as text rather than as bars. */
  const width = (i: number, base: number, spread: number) =>
    `${base + ((i * 17) % spread)}%`;
</script>

<div class="file-list" role="status" aria-busy="true" aria-live="polite">
  <span class="sr-only">{$t('common.loading')}</span>

  <div class="file-list-header" class:selecting-mode={isSelecting} aria-hidden="true">
    {#if isSelecting}
      <div class="list-cell"></div>
    {/if}
    <div class="list-cell">{$t('gallery.columnType')}</div>
    <div class="list-cell">{$t('gallery.columnTitle')}</div>
    <div class="list-cell list-cell-speakers">{$t('gallery.columnSpeakers')}</div>
    <div class="list-cell list-cell-duration">{$t('gallery.columnDuration')}</div>
    <div class="list-cell list-cell-date">{$t('gallery.columnDate')}</div>
    <div class="list-cell list-cell-size">{$t('gallery.columnSize')}</div>
    <div class="list-cell">{$t('gallery.columnStatus')}</div>
  </div>

  {#each rows as _, i}
    <div class="skel-row" aria-hidden="true">
      {#if isSelecting}
        <div class="list-cell"><div class="skel-check shimmer"></div></div>
      {/if}
      <div class="list-cell"><div class="skel-icon shimmer"></div></div>
      <div class="list-cell"><div class="skel-bar shimmer" style="width: {width(i, 55, 35)}"></div></div>
      <div class="list-cell list-cell-speakers">
        <div class="skel-bar shimmer" style="width: {width(i, 40, 30)}"></div>
      </div>
      <div class="list-cell list-cell-duration">
        <div class="skel-bar shimmer" style="width: 60%"></div>
      </div>
      <div class="list-cell list-cell-date">
        <div class="skel-bar shimmer" style="width: 80%"></div>
      </div>
      <div class="list-cell list-cell-size">
        <div class="skel-bar shimmer" style="width: 55%"></div>
      </div>
      <div class="list-cell"><div class="skel-pill shimmer"></div></div>
    </div>
  {/each}
</div>

<style>
  /* Chrome copied from `VirtualList`'s `.file-list` so the placeholder occupies
     the same box: same border, radius and surface. */
  .file-list {
    border: 1px solid var(--border-color);
    border-radius: 10px;
    overflow: hidden;
    background: var(--surface-color);
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

  .file-list-header {
    display: grid;
    grid-template-columns: var(--file-list-columns);
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    background: var(--surface-color);
    border-bottom: 1px solid var(--border-color);
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
  }

  .file-list-header.selecting-mode {
    grid-template-columns: var(--file-list-columns-selecting);
  }

  .skel-row {
    display: grid;
    grid-template-columns: var(--file-list-columns);
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    align-items: center;
    /* Matches a real row's height, so the list does not grow or shrink when the
       placeholder is swapped out. */
    min-height: 52px;
    border-bottom: 1px solid var(--border-color);
  }

  .skel-row:last-child {
    border-bottom: none;
  }

  .skel-icon {
    width: 20px;
    height: 20px;
    border-radius: 4px;
  }

  .skel-check {
    width: 18px;
    height: 18px;
    border-radius: 4px;
  }

  .skel-bar {
    height: 12px;
    border-radius: 4px;
  }

  .skel-pill {
    width: 72px;
    height: 20px;
    border-radius: 999px;
  }

  /* Hidden at the same breakpoints as the real table's columns, so the
     placeholder never renders a cell the loaded list will not have. */
  @media (max-width: 1024px) {
    .list-cell-speakers,
    .list-cell-size {
      display: none;
    }
  }

  @media (max-width: 768px) {
    .list-cell-duration,
    .list-cell-date {
      display: none;
    }
  }
</style>
