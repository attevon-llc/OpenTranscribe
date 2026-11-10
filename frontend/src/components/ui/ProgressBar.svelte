<!--
  ProgressBar.svelte — Shared progress bar component.

  Supports determinate (with percentage) and indeterminate modes.
  Replaces @keyframes indeterminate-slide/indeterminate duplicated in 5+ components.
-->
<script lang="ts">
  /** Progress percentage (0-100), or null for indeterminate mode. */
  export let percent: number | null = null;
  /** Optional visible label displayed above the bar. */
  export let label: string = '';
  /** Accessible name when there is no visible `label` (WAI-ARIA requires one). */
  export let ariaLabel: string = '';
  /** Semantic fill colour. */
  export let tone: 'primary' | 'success' | 'warning' | 'error' = 'primary';

  $: clamped = percent === null ? null : Math.min(100, Math.max(0, percent));
  $: name = ariaLabel || label || undefined;
  // A progressbar without an accessible name is an axe violation (aria-progressbar-name); props
  // are legacy `export let`, so a required-prop type cannot express "label OR ariaLabel".
  $: if (import.meta.env.DEV && !name) {
    console.warn('ProgressBar needs a label or ariaLabel (WAI-ARIA progressbar accessible name).');
  }
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
  <div
    class="progress-track"
    role="progressbar"
    aria-label={name}
    aria-valuemin="0"
    aria-valuemax="100"
    aria-valuenow={clamped === null ? undefined : Math.round(clamped)}
    aria-valuetext={clamped === null ? undefined : `${Math.round(clamped)}%`}
  >
    {#if clamped !== null}
      <div class="progress-fill tone-{tone}" style="width: {clamped}%;"></div>
    {:else}
      <div class="progress-fill tone-{tone} indeterminate"></div>
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
    margin-bottom: var(--space-1);
    font-size: 0.85rem;
    color: var(--text-secondary);
  }

  .progress-value {
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
  }

  .progress-track {
    height: 6px;
    background: var(--color-bg-tertiary);
    border-radius: var(--radius-pill);
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    /* --primary-color (#2563eb) is 2.0:1 on the dark track (#334155); WCAG 1.4.11 needs 3:1.
       --primary-on-surface is the blue that clears it in both themes. */
    background-color: var(--primary-on-surface);
    border-radius: var(--radius-pill);
    transition: width var(--duration-slow) ease;
  }

  .progress-fill.tone-success {
    background-color: var(--success-color);
  }

  .progress-fill.tone-warning {
    background-color: var(--warning-color);
  }

  .progress-fill.tone-error {
    background-color: var(--error-color);
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
