<!--
  PrivacySwitchCard.svelte — one privacy feature: its switch, who locked it, and its purge.

  Shared by the organization panel, the instance card and the user's own card so the three
  surfaces cannot drift apart. The switch is CONTROLLED: it shows `checked` and asks the parent
  to change it (`onToggle`); a refused change simply leaves it where it was, so a failed request
  never leaves the control lying about the server's state.

  Switching a feature off only hides data. Deleting it is the separate `Delete stored data`
  button, enabled (`canPurge`) only once the switch is off, because the API refuses a purge
  while the feature is still on (it would grow straight back).
-->
<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';

  export let id: string;
  export let title: string;
  export let description: string;
  /** The switch position: this layer's own setting. */
  export let checked: boolean;
  /** A layer above this one has the feature off: the switch cannot be moved. */
  export let locked = false;
  /** Why it is locked, already translated. Shown only while `locked`. */
  export let lockedReason = '';
  export let busy = false;
  /** False for a card whose switch lives elsewhere and which only offers the purge. */
  export let showSwitch = true;
  /** Extra line under the description (the gender notice), already translated. */
  export let notice = '';
  export let onToggle: (next: boolean) => void;
  /** Purge control; omit `purgeLabel` for a card with nothing to delete. */
  export let purgeLabel = '';
  export let purgeHint = '';
  export let canPurge = false;
  export let onPurge: () => void = () => {};
</script>

<section class="card" data-testid={`privacy-card-${id}`} aria-labelledby={`${id}-title`}>
  <div class="row">
    <div class="info">
      <h4 id={`${id}-title`} class="title">{title}</h4>
      <p class="description" id={`${id}-desc`}>{description}</p>
      {#if notice}
        <p class="notice" id={`${id}-notice`} data-testid={`${id}-notice`}>{notice}</p>
      {/if}
      {#if locked && lockedReason}
        <p class="locked" id={`${id}-locked`} data-testid={`${id}-locked`}>{lockedReason}</p>
      {/if}
    </div>
    {#if showSwitch}
      <span class="switch">
        <Toggle
          checked={checked}
          disabled={locked}
          {busy}
          labelledby={`${id}-title`}
          describedby={`${id}-desc${notice ? ` ${id}-notice` : ''}${locked && lockedReason ? ` ${id}-locked` : ''}`}
          testid={`${id}-switch`}
          onchange={(next) => onToggle(next)}
        />
      </span>
    {/if}
  </div>

  {#if purgeLabel}
    <div class="purge">
      <button
        type="button"
        class="btn btn-danger"
        disabled={!canPurge || busy}
        aria-describedby={`${id}-purge-hint`}
        data-testid={`${id}-purge`}
        on:click={onPurge}
      >
        {purgeLabel}
      </button>
      <small id={`${id}-purge-hint`}>{purgeHint}</small>
    </div>
  {/if}
</section>

<style>
  .card {
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--surface-color);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-4);
  }

  .info {
    flex: 1;
    min-width: 0;
  }

  .title {
    margin: 0 0 var(--space-1);
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .description,
  .notice,
  .locked {
    margin: 0;
    font-size: var(--font-size-sm);
    line-height: 1.5;
    color: var(--text-secondary);
  }

  .notice,
  .locked {
    margin-top: var(--space-2);
  }

  .locked {
    color: var(--text-color);
    font-weight: var(--font-weight-medium);
  }

  .switch {
    margin-top: var(--space-0-5);
    flex-shrink: 0;
  }

  .purge {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-color);
  }

  small {
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    flex: 1;
    min-width: 12rem;
  }
</style>
