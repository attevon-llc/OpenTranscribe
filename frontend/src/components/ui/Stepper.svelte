<!--
  Stepper.svelte — a progress-only step indicator (not navigation).

  Visual language shared with the upload and watch-source steppers: a 24px dot with a 2px ring,
  primary fill for complete and current steps (complete shows a check, current its number plus a
  halo), a 2px connector that turns primary once the step before it is complete. Labels sit under
  the dots; in a narrow container only the current step's name is shown, as a caption.

  The current step is `aria-current="step"`; complete steps carry a visually hidden `doneLabel`.
  The connector is drawn with logical insets, so RTL mirrors without a rule of its own.
-->
<script lang="ts">
  import MenuIcon from './MenuIcon.svelte';

  export let steps: { id: string; label: string; done: boolean }[] = [];
  /** Zero-based index of the step being shown. */
  export let current = 0;
  /** Accessible name of the list, e.g. "Setup progress". */
  export let label = '';
  /** Visually hidden suffix announced on complete steps, e.g. "done". */
  export let doneLabel = '';

  $: states = steps.map((step, i) =>
    i === current ? 'current' : step.done ? 'complete' : 'upcoming'
  );
</script>

<div class="stepper">
  <ol class="stepper-list" aria-label={label}>
    {#each steps as step, i (step.id)}
      <li
        class="stepper-step"
        data-state={states[i]}
        data-done={step.done}
        data-line={i > 0 && states[i - 1] === 'complete' && states[i] !== 'upcoming' ? 'complete' : undefined}
        aria-current={i === current ? 'step' : undefined}
      >
        <span class="stepper-dot" aria-hidden="true">
          {#if states[i] === 'complete'}
            <MenuIcon name="check" size={14} />
          {:else}
            {i + 1}
          {/if}
        </span>
        <span class="stepper-label">{step.label}</span>
        {#if states[i] === 'complete' && doneLabel}
          <span class="sr-only">{doneLabel}</span>
        {/if}
      </li>
    {/each}
  </ol>
  <p class="stepper-caption" aria-hidden="true">{steps[current]?.label ?? ''}</p>
</div>

<style>
  .stepper {
    container: stepper / inline-size;
  }

  .stepper-list {
    display: grid;
    grid-auto-columns: minmax(0, 1fr);
    grid-auto-flow: column;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .stepper-step {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.375rem;
    min-width: 0;
  }

  /* Connector from the previous dot's centre to this one's, leaving a small gap at each dot. */
  .stepper-step + .stepper-step::before {
    content: '';
    position: absolute;
    top: 11px;
    inset-inline-start: calc(-50% + 16px);
    inset-inline-end: calc(50% + 16px);
    height: 2px;
    border-radius: 1px;
    background-color: var(--border-color);
  }

  .stepper-step[data-line='complete']::before {
    background-color: var(--primary-color);
  }

  .stepper-dot {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border: 2px solid var(--border-color);
    border-radius: 50%;
    background-color: var(--surface-color);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-semibold);
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }

  .stepper-step[data-state='complete'] .stepper-dot,
  .stepper-step[data-state='current'] .stepper-dot {
    border-color: var(--primary-color);
    background-color: var(--primary-color);
    color: white;
  }

  .stepper-step[data-state='current'] .stepper-dot {
    box-shadow: 0 0 0 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .stepper-label {
    max-width: 100%;
    padding-inline: 0.25rem;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    line-height: var(--line-height-tight);
    text-align: center;
    overflow-wrap: break-word;
  }

  /* --primary-on-surface, not --primary-color: the fill blue is under AA as small text on the
     dark surface (the same finding as the upload stepper, #972). */
  .stepper-step[data-state='complete'] .stepper-label {
    color: var(--primary-on-surface);
  }

  .stepper-step[data-state='current'] .stepper-label {
    color: var(--text-primary);
    font-weight: var(--font-weight-semibold);
  }

  .stepper-caption {
    display: none;
    margin: 0.5rem 0 0;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-semibold);
    text-align: center;
  }

  @container stepper (max-width: 520px) {
    .stepper-label {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
    }

    .stepper-caption {
      display: block;
    }
  }

  @media (prefers-reduced-motion: no-preference) {
    .stepper-dot,
    .stepper-step::before {
      transition:
        background-color 0.2s ease,
        border-color 0.2s ease,
        box-shadow 0.2s ease;
    }
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
</style>
