<script lang="ts">
  import './skeleton-shimmer.css';
  import { t } from '$stores/locale';

  /**
   * Reusable card-grid skeleton loader for page/section-level loads.
   *
   * Shows a responsive grid of placeholder cards with shimmer animation
   * that mirrors the target layout (thumbnail + title + metadata lines).
   *
   * Use this in place of a generic <Spinner /> for:
   * - Gallery/home page initial file load
   * - Search results
   * - Speaker profiles grid
   * - Any grid of cards with a consistent shape
   *
   * Apple HIG / Nielsen Norman research: skeleton screens feel ~20% faster
   * than spinners because users anticipate the layout instead of fixating
   * on "waiting".
   */

  /** Variant controls the card aspect ratio and content layout */
  export let variant: 'media' | 'profile' | 'search' = 'media';
  /** Number of skeleton cards to render */
  export let count = 8;
  /** Minimum card width in pixels (defaults match each variant) */
  export let minCardWidth: number | null = null;

  // Per-variant defaults
  $: defaultMinWidth =
    variant === 'search' ? 320 :
    variant === 'profile' ? 200 : 220;
  $: actualMinWidth = minCardWidth ?? defaultMinWidth;

  // Stable array reference so Svelte doesn't re-render on every tick
  $: cards = Array.from({ length: count });

  // Pseudo-random-but-deterministic widths for text lines so the skeleton
  // looks natural (not a perfectly aligned grid of identical bars)
  function lineWidth(i: number, base: number, variance: number): string {
    return `${base + ((i * 37) % variance)}%`;
  }
</script>

<div
  class="card-grid-skeleton"
  class:is-search={variant === 'search'}
  style="--min-card-width: {actualMinWidth}px"
  role="status"
  aria-busy="true"
  aria-live="polite"
>
  <span class="sr-only">{$t('common.loading')}</span>

  {#each cards as _, i}
    <div class="skel-card">
      {#if variant === 'media'}
        <!-- Media card: thumbnail (16:9) + title + meta -->
        <div class="skel-thumb shimmer">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="32"
            height="32"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
            class="skel-thumb-icon"
            aria-hidden="true"
          >
            <polygon points="23 7 16 12 23 17 23 7"></polygon>
            <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
          </svg>
        </div>
        <div class="skel-body">
          <div class="skel-title shimmer" style="width: {lineWidth(i, 62, 30)}"></div>
          <div class="skel-meta-row">
            <div class="skel-meta shimmer" style="width: {lineWidth(i, 30, 20)}"></div>
            <div class="skel-meta shimmer" style="width: {lineWidth(i, 20, 15)}"></div>
          </div>
        </div>
      {:else if variant === 'profile'}
        <!-- Profile card: avatar circle + name + description -->
        <div class="skel-profile-top">
          <div class="skel-avatar shimmer"></div>
          <div class="skel-profile-text">
            <div class="skel-title shimmer" style="width: {lineWidth(i, 55, 30)}"></div>
            <div class="skel-subtitle shimmer" style="width: {lineWidth(i, 35, 20)}"></div>
          </div>
        </div>
        <div class="skel-body">
          <div class="skel-text-line shimmer" style="width: {lineWidth(i, 80, 15)}"></div>
          <div class="skel-text-line shimmer" style="width: {lineWidth(i, 60, 25)}"></div>
        </div>
      {:else if variant === 'search'}
        <!-- Search result: larger card with thumbnail + title + matched excerpt -->
        <div class="skel-search-row">
          <div class="skel-thumb-small shimmer"></div>
          <div class="skel-body">
            <div class="skel-title shimmer" style="width: {lineWidth(i, 65, 25)}"></div>
            <div class="skel-meta-row">
              <div class="skel-chip shimmer"></div>
              <div class="skel-chip shimmer"></div>
            </div>
            <div class="skel-text-line shimmer" style="width: {lineWidth(i, 90, 8)}"></div>
            <div class="skel-text-line shimmer" style="width: {lineWidth(i, 70, 20)}"></div>
          </div>
        </div>
      {/if}
    </div>
  {/each}
</div>

<style>
  .card-grid-skeleton {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(var(--min-card-width, 220px), 1fr));
    gap: 16px;
    padding: 8px 0;
    width: 100%;
  }

  /* Search results render as a single full-width stacked column (Google-style),
     so the skeleton must match — not the responsive multi-column grid. */
  .card-grid-skeleton.is-search {
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

  .skel-card {
    border: 1px solid var(--border-color);
    background-color: var(--surface-color);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  /* ── Media card variant ── */
  .skel-thumb {
    width: 100%;
    aspect-ratio: 16 / 9;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
  }

  .skel-thumb-icon {
    color: rgba(100, 116, 139, 0.35);
    position: relative;
    z-index: 1;
  }

  :global([data-theme='dark']) .skel-thumb-icon {
    color: rgba(148, 163, 184, 0.35);
  }

  .skel-body {
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .skel-title {
    height: 16px;
    border-radius: 4px;
  }

  .skel-meta-row {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }

  .skel-meta {
    height: 12px;
    border-radius: 4px;
  }

  /* ── Profile card variant ── */
  .skel-profile-top {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 14px 0;
  }

  .skel-avatar {
    flex-shrink: 0;
    width: 44px;
    height: 44px;
    border-radius: 50%;
  }

  .skel-profile-text {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  .skel-subtitle {
    height: 12px;
    border-radius: 4px;
  }

  .skel-text-line {
    height: 11px;
    border-radius: 4px;
  }

  /* ── Search result variant ── */
  .skel-search-row {
    display: flex;
    gap: 14px;
    padding: 14px;
  }

  .skel-thumb-small {
    flex-shrink: 0;
    width: 96px;
    height: 56px;
    border-radius: 6px;
  }

  .skel-chip {
    height: 16px;
    width: 52px;
    border-radius: 999px;
  }


  /* ── Responsive ── */
  @media (max-width: 640px) {
    .card-grid-skeleton {
      gap: 12px;
    }
    .skel-search-row {
      flex-direction: column;
      gap: 10px;
    }
    .skel-thumb-small {
      width: 100%;
      height: 120px;
    }
  }
</style>
