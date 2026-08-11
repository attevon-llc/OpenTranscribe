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
  import Chip from './Chip.svelte';

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
  /**
   * Most chips to render before summarising the rest.
   *
   * A selection of 100 files can carry every tag in the library; rendering all
   * of them turns the dialog into a wall nobody reads. Full-coverage chips
   * (on every selected file) sort first, because those are the ones that
   * describe the selection as a whole.
   */
  export let maxVisible = 12;
  /** Renders the "+N more" summary. */
  export let overflowLabel: (count: number) => string = (count) => `+${count} more`;

  $: sorted = [...chips].sort((a, b) => (b.count ?? 0) - (a.count ?? 0));
  $: visible = sorted.slice(0, maxVisible);
  $: hidden = Math.max(0, sorted.length - visible.length);

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
  {#each visible as chip (chip.uuid)}
    <span class="chip-slot" class:partial={isPartial(chip)}>
      <Chip
        removable={removable && !disabled}
        removeLabel={removeLabel(chip.name)}
        on:remove={() => dispatch('remove', chip)}
      >
        {chip.name}{#if !removable && typeof chip.count === 'number' && typeof chip.total === 'number'}<span
            class="chip-count">{coverageLabel(chip.count, chip.total)}</span
          >{/if}
      </Chip>
    </span>
  {/each}
  {#if hidden > 0}
    <span class="chip-overflow">{overflowLabel(hidden)}</span>
  {/if}
</div>

<style>
  .chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }



  /* Partial coverage reads differently from total: otherwise adding looks like
     a no-op when it is not. Applied to the wrapper so the shared Chip
     primitive stays untouched. */
  .chip-slot.partial :global(.chip) {
    border-style: dashed;
    opacity: 0.75;
  }

  .chip-overflow {
    align-self: center;
    font-size: 12px;
    color: var(--text-secondary);
  }

  /* The count follows the name inside one chip; without this it renders as
     "Interviewon 1 of 3" — the markup has to stay whitespace-free to avoid a
     stray gap before the chip's own padding, so the gap is a margin. */
  .chip-count {
    margin-left: 5px;
    font-size: 10px;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }



</style>
