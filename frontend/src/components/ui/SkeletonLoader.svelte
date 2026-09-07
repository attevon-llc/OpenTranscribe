<!--
  SkeletonLoader.svelte — Shared skeleton loading placeholder.

  Replaces @keyframes skeleton-pulse duplicated in 7 components.
-->
<script lang="ts">
  /** Number of skeleton rows to display. */
  export let lines: number = 3;
  /** Height of each skeleton row in pixels. */
  export let height: number = 16;
</script>

<div class="skeleton-container">
  {#each Array(lines) as _, i}
    <div
      class="skeleton-line"
      style="height: {height}px; width: {i === lines - 1 ? '60%' : '100%'}"
    ></div>
  {/each}
</div>

<style>
  .skeleton-container {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .skeleton-line {
    background: linear-gradient(
      90deg,
      var(--bg-tertiary, #e8e8e8) 25%,
      var(--bg-secondary, #f0f0f0) 50%,
      var(--bg-tertiary, #e8e8e8) 75%
    );
    background-size: 200% 100%;
    border-radius: 4px;
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  :global([data-theme='dark']) .skeleton-line {
    background: linear-gradient(
      90deg,
      rgba(255, 255, 255, 0.06) 25%,
      rgba(255, 255, 255, 0.1) 50%,
      rgba(255, 255, 255, 0.06) 75%
    );
    background-size: 200% 100%;
  }

  @keyframes skeleton-pulse {
    0% {
      background-position: 200% 0;
    }
    100% {
      background-position: -200% 0;
    }
  }
</style>
