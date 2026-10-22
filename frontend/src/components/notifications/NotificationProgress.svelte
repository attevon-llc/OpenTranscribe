<script lang="ts">
  import { t } from '$stores/locale';

  /** 0-100. */
  export let percent: number;
  /** Accessible name of the bar. */
  export let label: string;
  /** Shimmer while work is live; a finished or failed bar is still. */
  export let active = true;
  /** A failed run: the bar stops where it got to, in the error colour. */
  export let failed = false;
  export let eta = '';

  $: value = Math.round(Math.min(100, Math.max(0, percent)));
  $: valueText = $t('notifications.job.progressValue', { percent: value });
</script>

<div class="progress-container">
  <!-- No aria-live: progress ticks are not announced (JobAnnouncer speaks outcomes only). -->
  <div
    class="progress-bar-bg"
    role="progressbar"
    aria-label={label}
    aria-valuemin="0"
    aria-valuemax="100"
    aria-valuenow={value}
    aria-valuetext={valueText}
  >
    <div class="progress-bar-fill" class:active class:failed style="width: {value}%"></div>
  </div>
  <span class="progress-text" aria-hidden="true">
    {value}%
    {#if eta}
      <span class="progress-eta">{$t('notifications.job.eta', { duration: eta })}</span>
    {/if}
  </span>
</div>

<style>
  .progress-container {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
  }

  .progress-bar-bg {
    flex: 1;
    height: 4px;
    background: var(--border-color);
    border-radius: 2px;
    overflow: hidden;
  }

  .progress-bar-fill {
    height: 100%;
    background: var(--primary-color);
    border-radius: 2px;
    transition: width 0.3s ease;
  }

  .progress-bar-fill.active {
    background: linear-gradient(
      90deg,
      var(--primary-color) 0%,
      rgba(var(--primary-color-rgb), 0.8) 50%,
      var(--primary-color) 100%
    );
    background-size: 200px 100%;
    animation: progress-shimmer 1.5s infinite linear;
  }

  .progress-bar-fill.failed {
    background: var(--error-color);
  }

  @keyframes progress-shimmer {
    0% {
      background-position: -200px 0;
    }
    100% {
      background-position: calc(200px + 100%) 0;
    }
  }

  .progress-text {
    font-size: var(--font-size-2xs, 11px);
    font-weight: var(--font-weight-semibold, 600);
    color: var(--text-secondary);
    min-width: 32px;
    text-align: end;
    white-space: nowrap;
  }

  .progress-eta {
    font-weight: var(--font-weight-medium, 500);
    margin-inline-start: 4px;
  }

  @media (prefers-reduced-motion: reduce) {
    .progress-bar-fill {
      transition: none;
    }

    .progress-bar-fill.active {
      animation: none;
      background: var(--primary-color);
    }
  }
</style>
