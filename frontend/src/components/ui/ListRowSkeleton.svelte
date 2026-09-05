<script lang="ts">
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

  {#each rows as _, i}
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
    gap: 12px;
    width: 100%;
    padding: 8px 0;
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
    gap: 14px;
    padding: 14px 16px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 10px;
  }

  .skel-row.compact {
    padding: 10px 14px;
    gap: 10px;
  }

  .skel-avatar {
    flex-shrink: 0;
    width: 40px;
    height: 40px;
    border-radius: 50%;
  }

  .skel-row.compact .skel-avatar {
    width: 32px;
    height: 32px;
  }

  .skel-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  .skel-title {
    height: 15px;
    border-radius: 4px;
  }

  .skel-subtitle {
    height: 12px;
    border-radius: 4px;
  }

  .skel-actions {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }

  .skel-action {
    width: 32px;
    height: 32px;
    border-radius: 8px;
  }

  /* ── Shimmer effect ── */
  .shimmer {
    position: relative;
    overflow: hidden;
    background: rgba(100, 116, 139, 0.12);
  }

  :global([data-theme='dark']) .shimmer {
    background: rgba(148, 163, 184, 0.1);
  }

  .shimmer::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(
      90deg,
      transparent 0%,
      rgba(255, 255, 255, 0.35) 50%,
      transparent 100%
    );
    transform: translateX(-100%);
    animation: shimmerSlide 1.4s ease-in-out infinite;
  }

  :global([data-theme='dark']) .shimmer::after {
    background: linear-gradient(
      90deg,
      transparent 0%,
      rgba(255, 255, 255, 0.06) 50%,
      transparent 100%
    );
  }

  @keyframes shimmerSlide {
    100% { transform: translateX(100%); }
  }

  @media (prefers-reduced-motion: reduce) {
    .shimmer::after { animation: none; background: none; }
    .shimmer { background: rgba(100, 116, 139, 0.15); }
  }

  /* ── Responsive ── */
  @media (max-width: 640px) {
    .skel-actions .skel-action:nth-child(2) {
      display: none;
    }
  }
</style>
