<script lang="ts">
  import './skeleton-shimmer.css';
  import { t } from '$stores/locale';

  /**
   * Reusable list-row skeleton for vertically-stacked items.
   *
   * Use this for speaker clusters, inbox items, or any list where
   * each item is a full-width horizontal row (not a grid card).
   */

  /** Number of skeleton rows to render */
  export let count = 5;
  /** Row height variant */
  export let size: 'compact' | 'comfortable' = 'comfortable';

  $: rows = Array.from({ length: count });
</script>

<div class="list-skeleton" role="status" aria-busy="true" aria-live="polite">
  <span class="sr-only">{$t('common.loading')}</span>

  {#each rows as _, i (i)}
    <div class="skel-row" class:compact={size === 'compact'}>
      <div class="skel-avatar shimmer"></div>
      <div class="skel-body">
        <div class="skel-title shimmer" style="width: {55 + ((i * 17) % 30)}%"></div>
        <div class="skel-subtitle shimmer" style="width: {30 + ((i * 23) % 25)}%"></div>
      </div>
      <div class="skel-actions">
        <div class="skel-action shimmer"></div>
        <div class="skel-action shimmer"></div>
      </div>
    </div>
  {/each}
</div>

<style>
  .list-skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    width: 100%;
    padding: var(--space-2) 0;
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

  .skel-row {
    display: flex;
    align-items: center;
    gap: var(--space-3-5);
    padding: var(--space-3-5) var(--space-4);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-xl);
  }

  .skel-row.compact {
    padding: var(--space-2-5) var(--space-3-5);
    gap: var(--space-2-5);
  }

  .skel-avatar {
    flex-shrink: 0;
    width: 40px;
    height: 40px;
    border-radius: var(--radius-round);
  }

  .skel-row.compact .skel-avatar {
    width: 32px;
    height: 32px;
  }

  .skel-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
    min-width: 0;
  }

  .skel-title {
    height: 15px;
    border-radius: var(--radius-sm);
  }

  .skel-subtitle {
    height: 12px;
    border-radius: var(--radius-sm);
  }

  .skel-actions {
    display: flex;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  .skel-action {
    width: 32px;
    height: 32px;
    border-radius: var(--radius-lg);
  }


  /* ── Responsive ── */
  @media (max-width: 640px) {
    .skel-actions .skel-action:nth-child(2) {
      display: none;
    }
  }
</style>
