<!--
  RecoveryActionRow.svelte — one action of the File status > Recovery tab: bold title, one line
  saying what it does and who it covers, and the button on the inline-end side (it stacks under the
  text on narrow widths). The confirmation dialog is the parent's job; this only reports the click.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let id: string;
  export let title: string;
  export let help: string;
  export let buttonLabel: string;

  const dispatch = createEventDispatcher<{ run: void }>();
</script>

<div class="action-row" data-testid="recovery-action-{id}">
  <div class="action-text">
    <h4 class="action-title">{title}</h4>
    <p class="action-help" id="recovery-help-{id}">{help}</p>
  </div>
  <button
    type="button"
    class="btn btn-secondary"
    aria-describedby="recovery-help-{id}"
    on:click={() => dispatch('run')}
  >
    {buttonLabel}
  </button>
</div>

<style>
  .action-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 0;
  }

  :global(.recovery-actions) > .action-row:not(:first-child) {
    border-top: 1px solid var(--border-color);
  }

  .action-text {
    min-width: 0;
    flex: 1;
  }

  .action-title {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .action-help {
    margin: 0.125rem 0 0;
    font-size: var(--font-size-sm);
    line-height: var(--line-height-snug);
    color: var(--text-secondary);
  }

  .action-row .btn {
    flex-shrink: 0;
    min-width: 7.5rem;
    justify-content: center;
  }

  @media (max-width: 768px) {
    .action-row {
      flex-direction: column;
      align-items: stretch;
    }

    .action-row .btn {
      justify-content: center;
      min-height: 44px;
    }
  }
</style>
