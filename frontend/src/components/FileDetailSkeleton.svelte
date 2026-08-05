<script lang="ts">
  import { t } from '$stores/locale';

  // Skeleton loader for the file detail page.
  // Mirrors the final layout (file header + 2-column grid with video on left,
  // transcript on right) so the user sees "progress" and structure immediately
  // rather than a generic spinner.
  //
  // Apple HIG / Nielsen Norman research shows skeleton screens are perceived
  // as 17-25% faster than spinners for the same actual load time, because the
  // brain starts anticipating the layout instead of fixating on "waiting".
</script>

<div class="skeleton-page" role="status" aria-busy="true" aria-live="polite">
  <span class="sr-only">{$t('fileDetail.loading')}</span>

  <!-- File header (title, metadata strip) -->
  <div class="skeleton-file-header">
    <div class="skeleton-title shimmer"></div>
    <div class="skeleton-meta-row">
      <div class="skeleton-meta-chip shimmer"></div>
      <div class="skeleton-meta-chip shimmer"></div>
      <div class="skeleton-meta-chip shimmer"></div>
      <div class="skeleton-meta-chip shimmer"></div>
    </div>
  </div>

  <!-- Main content grid: video on left, transcript on right -->
  <div class="skeleton-grid">
    <!-- Left column: video player + tags -->
    <section class="skeleton-video-column">
      <div class="skeleton-column-header">
        <div class="skeleton-small-label shimmer"></div>
        <div class="skeleton-header-buttons">
          <div class="skeleton-button shimmer"></div>
          <div class="skeleton-button shimmer"></div>
        </div>
      </div>

      <!-- Video/audio player placeholder -->
      <div class="skeleton-player shimmer">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="48"
          height="48"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
          class="skeleton-player-icon"
          aria-hidden="true"
        >
          <polygon points="5 3 19 12 5 21 5 3"></polygon>
        </svg>
      </div>

      <!-- Tags/chips row -->
      <div class="skeleton-chips-row">
        <div class="skeleton-chip shimmer"></div>
        <div class="skeleton-chip shimmer"></div>
        <div class="skeleton-chip shimmer"></div>
      </div>
    </section>

    <!-- Right column: transcript -->
    <section class="skeleton-transcript-column">
      <div class="skeleton-column-header">
        <div class="skeleton-small-label shimmer"></div>
      </div>

      <div class="skeleton-transcript-list">
        {#each Array(6) as _, i}
          <div class="skeleton-transcript-item">
            <div class="skeleton-speaker-dot shimmer"></div>
            <div class="skeleton-transcript-body">
              <div class="skeleton-speaker-line shimmer"></div>
              <div class="skeleton-text-line shimmer" style="width: {70 + (i * 5) % 25}%"></div>
              <div class="skeleton-text-line shimmer" style="width: {55 + (i * 7) % 30}%"></div>
            </div>
          </div>
        {/each}
      </div>
    </section>
  </div>
</div>

<style>
  .skeleton-page {
    width: 100%;
    max-width: 100%;
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

  /* ── File header ── */
  .skeleton-file-header {
    margin-bottom: 24px;
  }

  .skeleton-title {
    width: 45%;
    max-width: 480px;
    height: 28px;
    border-radius: 6px;
    margin-bottom: 12px;
  }

  .skeleton-meta-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .skeleton-meta-chip {
    width: 72px;
    height: 18px;
    border-radius: 999px;
  }

  .skeleton-meta-chip:nth-child(2) { width: 96px; }
  .skeleton-meta-chip:nth-child(3) { width: 60px; }
  .skeleton-meta-chip:nth-child(4) { width: 84px; }

  /* ── Main grid ── */
  .skeleton-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 32px;
    align-items: start;
  }

  .skeleton-video-column,
  .skeleton-transcript-column {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }

  .skeleton-column-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 32px;
  }

  .skeleton-small-label {
    width: 72px;
    height: 16px;
    border-radius: 4px;
  }

  .skeleton-header-buttons {
    display: flex;
    gap: 8px;
  }

  .skeleton-button {
    width: 96px;
    height: 36px;
    border-radius: 8px;
  }

  /* ── Video player placeholder ── */
  .skeleton-player {
    width: 100%;
    aspect-ratio: 16 / 9;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
  }

  .skeleton-player-icon {
    color: rgba(100, 116, 139, 0.35);
    position: relative;
    z-index: 1;
  }

  :global(.dark) .skeleton-player-icon {
    color: rgba(148, 163, 184, 0.35);
  }

  /* ── Chips row (tags) ── */
  .skeleton-chips-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 4px;
  }

  .skeleton-chip {
    height: 22px;
    border-radius: 999px;
  }

  .skeleton-chip:nth-child(1) { width: 68px; }
  .skeleton-chip:nth-child(2) { width: 92px; }
  .skeleton-chip:nth-child(3) { width: 54px; }

  /* ── Transcript list ── */
  .skeleton-transcript-list {
    display: flex;
    flex-direction: column;
    gap: 18px;
    padding: 12px 0;
  }

  .skeleton-transcript-item {
    display: flex;
    gap: 12px;
    align-items: flex-start;
  }

  .skeleton-speaker-dot {
    flex-shrink: 0;
    width: 28px;
    height: 28px;
    border-radius: 50%;
  }

  .skeleton-transcript-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  .skeleton-speaker-line {
    width: 110px;
    height: 14px;
    border-radius: 4px;
    margin-bottom: 2px;
  }

  .skeleton-text-line {
    height: 12px;
    border-radius: 4px;
  }

  /* ── Shimmer effect ── */
  .shimmer {
    position: relative;
    overflow: hidden;
    background: var(--skeleton-base, rgba(100, 116, 139, 0.12));
  }

  :global(.dark) .shimmer {
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

  :global(.dark) .shimmer::after {
    background: linear-gradient(
      90deg,
      transparent 0%,
      rgba(255, 255, 255, 0.06) 50%,
      transparent 100%
    );
  }

  @keyframes shimmerSlide {
    100% {
      transform: translateX(100%);
    }
  }

  /* Respect reduced motion preferences */
  @media (prefers-reduced-motion: reduce) {
    .shimmer::after {
      animation: none;
      background: none;
    }
    .shimmer {
      background: var(--skeleton-base, rgba(100, 116, 139, 0.15));
    }
  }

  /* ── Responsive ── */
  @media (max-width: 1024px) {
    .skeleton-grid {
      grid-template-columns: 1fr;
      gap: 24px;
    }
  }

  @media (max-width: 640px) {
    .skeleton-title {
      width: 70%;
      height: 24px;
    }
    .skeleton-file-header {
      margin-bottom: 16px;
    }
    .skeleton-grid {
      gap: 16px;
    }
  }
</style>
