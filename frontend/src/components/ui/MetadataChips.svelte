<script context="module" lang="ts">
  /**
   * One chip: a piece of metadata attached to some or all of a selection.
   *
   * `count`/`total` are optional — omit both for a plain chip, supply both to
   * show partial coverage ("on 3 of 5"). A chip covering only part of the
   * selection renders dashed, because "add" is not a no-op for it the way it is
   * for a chip on everything.
   */
  export interface MetadataChip {
    uuid: string;
    name: string;
    count?: number;
    total?: number;
  }
</script>

<script lang="ts">
  /**
   * The shared chip row for metadata attached to files — tags and collections.
   *
   * Both answer the same question ("what is already on this selection?") and
   * previously answered it in two different shapes. One component means the two
   * modals cannot drift apart on spacing, partial-coverage treatment, or what a
   * remove control looks like.
   */
  import { createEventDispatcher } from 'svelte';

  export let chips: MetadataChip[] = [];
  /**
   * Whether chips can be removed here.
   *
   * False for a multi-file selection: removing something that sits on three of
   * five files is ambiguous in a way adding never is, so the chips stay
   * read-only and report their coverage instead.
   */
  export let removable = false;
  /** Accessible label for a remove button, given the chip name. */
  export let removeLabel: (name: string) => string = (name) => `Remove ${name}`;
  /** Renders the "on N of M" coverage text. */
  export let coverageLabel: (count: number, total: number) => string = (count, total) =>
    `on ${count} of ${total}`;
  export let disabled = false;

  const dispatch = createEventDispatcher<{ remove: MetadataChip }>();

  function isPartial(chip: MetadataChip): boolean {
    return (
      typeof chip.count === 'number' &&
      typeof chip.total === 'number' &&
      chip.count < chip.total
    );
  }
</script>

<div class="chip-row">
  {#each chips as chip (chip.uuid)}
    <span class="metadata-chip" class:partial={isPartial(chip)}>
      <span class="chip-name">{chip.name}</span>
      {#if !removable && typeof chip.count === 'number' && typeof chip.total === 'number'}
        <span class="chip-count">{coverageLabel(chip.count, chip.total)}</span>
      {:else if removable}
        <button
          type="button"
          class="chip-remove"
          on:click={() => dispatch('remove', chip)}
          {disabled}
          title={removeLabel(chip.name)}
          aria-label={removeLabel(chip.name)}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      {/if}
    </span>
  {/each}
</div>

<style>
  .chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .metadata-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 8px;
    border: 1px solid var(--border-color);
    border-radius: 12px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 12px;
  }

  /* Partial coverage reads differently from total: otherwise adding looks like
     a no-op when it is not. */
  .metadata-chip.partial {
    border-style: dashed;
    color: var(--text-secondary);
  }

  .chip-count {
    font-size: 10px;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .chip-remove {
    display: flex;
    padding: 0;
    border: none;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .chip-remove:hover:not(:disabled) {
    color: var(--error-color, #dc2626);
  }

  .chip-remove:disabled {
    opacity: 0.5;
    cursor: default;
  }
</style>
