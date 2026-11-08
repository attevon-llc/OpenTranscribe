<!--
  ProgressBar.svelte — Shared progress bar component.

  Supports determinate (with percentage) and indeterminate modes.
  Replaces @keyframes indeterminate-slide/indeterminate duplicated in 5+ components.
-->
<script lang="ts">
  /** Progress percentage (0-100), or null for indeterminate mode. */
  export let percent: number | null = null;
  /** Optional label displayed above the bar. */
  export let label: string = '';
  /** Bar color. Falls back to CSS variable --accent-color. */
  export let color: string = '';
</script>

<div class="progress-container">
  {#if label}
    <div class="progress-label">
      <span>{label}</span>
      {#if percent !== null}
        <span class="progress-value" dir="ltr">{Math.round(percent)}%</span>
      {/if}
    </div>
  {/if}
  <div class="progress-track">
    {#if percent !== null}
      <div
        class="progress-fill"
        style="width: {Math.min(100, Math.max(0, percent))}%;{color ? ` background-color: ${color}` : ''}"
      ></div>
    {:else}
      <div
        class="progress-fill indeterminate"
        style={color ? `background-color: ${color}` : ''}
      ></div>
    {/if}
  </div>
</div>

<style>
  .progress-container {
    width: 100%;
  }

  .progress-label {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.25rem;
    font-size: 0.85rem;
    color: var(--text-secondary, #666);
  }

  .progress-value {
    font-weight: 600;
    color: var(--text-primary, #1a1a1a);
  }

  .progress-track {
    height: 6px;
    background: var(--bg-tertiary, #e8e8e8);
    border-radius: 3px;
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    background-color: var(--accent-color, #4a90d9);
    border-radius: 3px;
    transition: width 0.3s ease;
  }

  .progress-fill.indeterminate {
    width: 30%;
    animation: indeterminate-slide 1.5s ease-in-out infinite;
  }

  @keyframes indeterminate-slide {
    0% {
      transform: translateX(-100%);
    }
    100% {
      transform: translateX(400%);
    }
  }
</style>
