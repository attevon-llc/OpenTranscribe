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

  function change(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const next = input.checked;
    input.checked = checked;
    if (!locked && !busy) onToggle(next);
  }
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
      <label class="toggle">
        <input
          type="checkbox"
          role="switch"
          {checked}
          disabled={locked || busy}
          aria-labelledby={`${id}-title`}
          aria-describedby={`${id}-desc${notice ? ` ${id}-notice` : ''}${locked && lockedReason ? ` ${id}-locked` : ''}`}
          data-testid={`${id}-switch`}
          on:change={change}
        />
        <span class="slider" aria-hidden="true"></span>
      </label>
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
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
  }

  .info {
    flex: 1;
    min-width: 0;
  }

  .title {
    margin: 0 0 0.25rem;
    font-size: 0.9375rem;
    font-weight: var(--font-weight-semibold, 600);
    color: var(--text-color);
  }

  .description,
  .notice,
  .locked {
    margin: 0;
    font-size: 0.8125rem;
    line-height: 1.5;
    color: var(--text-secondary);
  }

  .notice,
  .locked {
    margin-top: 0.5rem;
  }

  .locked {
    color: var(--text-color);
    font-weight: var(--font-weight-medium, 500);
  }

  .toggle {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 24px;
    flex-shrink: 0;
    margin-top: 0.1rem;
  }

  .toggle input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    opacity: 0;
    cursor: pointer;
    z-index: 1;
  }

  .slider {
    position: absolute;
    inset: 0;
    background-color: var(--border-color);
    border-radius: 24px;
    transition: background-color 0.2s;
  }

  .slider::before {
    content: '';
    position: absolute;
    height: 18px;
    width: 18px;
    inset-inline-start: 3px;
    bottom: 3px;
    background-color: white;
    border-radius: 50%;
    transition: inset-inline-start 0.2s;
  }

  .toggle input:checked + .slider {
    background-color: var(--primary-color);
  }

  .toggle input:checked + .slider::before {
    inset-inline-start: 23px;
  }

  .toggle input:focus-visible + .slider {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .toggle input:disabled {
    cursor: not-allowed;
  }

  .toggle input:disabled + .slider {
    opacity: 0.5;
  }

  .purge {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border-color);
  }

  small {
    color: var(--text-secondary);
    font-size: 0.75rem;
    flex: 1;
    min-width: 12rem;
  }
</style>
