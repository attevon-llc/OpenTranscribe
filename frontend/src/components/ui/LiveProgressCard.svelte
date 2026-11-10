<!--
  LiveProgressCard.svelte: the card shown while a long background job runs (reindex, embedding
  migration, auto-label, speaker detection): title row, determinate or indeterminate
  ProgressBar, message + ETA row, optional stop button.

  The bar is a WAI-ARIA progressbar named by `barLabel` (a name for the bar, not a status
  string); the count/percent text sits in a polite live region.
-->
<script lang="ts">
  import { formatEta } from '$lib/utils/formatting';
  import Spinner from './Spinner.svelte';
  import ProgressBar from './ProgressBar.svelte';
  import Button from './Button.svelte';

  /** Translated heading beside the spinner; omit when the surrounding section already names it. */
  export let title = '';
  /** `starting` and a null `percent` render the indeterminate bar; `stopping` busies the stop button. */
  export let state: 'starting' | 'running' | 'stopping' = 'running';
  export let done: number | null = null;
  export let total: number | null = null;
  /** Pre-translated count text; replaces the plain "done / total". */
  export let countLabel = '';
  /** 0-100, or null for indeterminate. */
  export let percent: number | null = null;
  export let etaSeconds: number | null = null;
  /** Translated word after the ETA, e.g. "remaining". */
  export let remainingLabel = '';
  export let message = '';
  /** Accessible name of the progress bar (required). */
  export let barLabel: string;
  export let stopLabel = '';
  export let stoppingLabel = '';
  /** No stop button when null. */
  export let onStop: (() => void) | null = null;

  $: rounded = percent === null ? null : Math.round(Math.min(100, Math.max(0, percent)));
  $: count = countLabel || (done !== null && total !== null ? `${done} / ${total}` : '');
  $: eta = formatEta(etaSeconds);
  $: stopping = state === 'stopping';
</script>

<div class="live-progress-card" data-state={state}>
  {#if title || count}
    <div class="lpc-header">
      <span class="lpc-title">
        {#if title}<Spinner size="small" />{title}{/if}
      </span>
      <span class="lpc-count" aria-live="polite">
        {count}
        {#if rounded !== null}<bdi dir="ltr">{rounded}%</bdi>{/if}
      </span>
    </div>
  {/if}
  <ProgressBar percent={state === 'starting' ? null : percent} ariaLabel={barLabel} />
  {#if message || eta}
    <div class="lpc-details">
      <span class="lpc-message">{message}</span>
      {#if eta}<span class="lpc-eta">{eta} {remainingLabel}</span>{/if}
    </div>
  {/if}
  <slot />
  {#if onStop}
    <div class="lpc-actions">
      <Button variant="danger-outline" size="sm" busy={stopping} on:click={onStop}>
        {stopping ? stoppingLabel || stopLabel : stopLabel}
      </Button>
    </div>
  {/if}
</div>

<style>
  .live-progress-card {
    padding: var(--space-3);
    background: var(--surface-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
  }

  .lpc-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-2);
  }

  .lpc-title {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--primary-on-surface);
  }

  .lpc-count {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .lpc-details {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-2);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .lpc-message {
    flex: 1;
  }

  .lpc-eta {
    white-space: nowrap;
    font-weight: var(--font-weight-medium);
    color: var(--primary-on-surface);
  }

  .lpc-actions {
    display: flex;
    justify-content: flex-end;
    margin-top: var(--space-2);
  }
</style>
