<script lang="ts">
  /**
   * The gallery filter panel's toggleable chip — one primitive for what used
   * to be five near-identical button classes in `FilterSidebar.svelte`
   * (`.tag-button`, `.speaker-button`, `.file-type-button`, `.status-button`,
   * `.ownership-button`), plus the two new chip rows issue #750 adds (file
   * type icons, duration quick chips). `ui/Chip.svelte` cannot serve this role
   * — it is display/removable only, with no selected state and no body click
   * handler (see `ui/CLAUDE.md`).
   *
   * Callers may still pass a semantic `class` (e.g. `class="tag-button"`) so
   * existing test/e2e selectors keep working — this component only owns the
   * shared VISUAL styling, not identity.
   */
  export let selected = false;
  /** Optional trailing count badge (e.g. "used in 3 files"). */
  export let count: number | null = null;
  export let title: string | undefined = undefined;
  /** Overrides the default `aria-pressed={selected}` when the control isn't a pure toggle. */
  export let ariaPressed: boolean | undefined = undefined;

  let className = '';
  export { className as class };
</script>

<button
  type="button"
  class="filter-chip-btn {className}"
  class:selected
  aria-pressed={ariaPressed ?? selected}
  {title}
  {...$$restProps}
  on:click
>
  <slot name="icon" />
  <span class="filter-chip-label"><slot /></span>
  {#if count !== null && count !== undefined}
    <span class="filter-chip-count">{count}</span>
  {/if}
</button>

<style>
  .filter-chip-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.8rem;
    font-weight: 400;
    padding: 0.35rem 0.7rem;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .filter-chip-btn:hover {
    background-color: var(--hover-color);
    border-color: var(--primary-color-light);
  }

  .filter-chip-btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  .filter-chip-btn.selected {
    background-color: var(--primary-color);
    color: white;
    border-color: var(--primary-color);
  }

  .filter-chip-btn :global(svg) {
    flex-shrink: 0;
    width: 14px;
    height: 14px;
  }

  .filter-chip-count {
    font-size: 0.7rem;
    opacity: 0.8;
  }
</style>
