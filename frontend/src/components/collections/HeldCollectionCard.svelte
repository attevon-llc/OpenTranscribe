<!--
  HeldCollectionCard.svelte - the computed "Legal hold" collection (legal-hold plan §5, Lane D).

  Not a stored collection: the server computes which files are held. Choosing it shows the
  gallery filtered to held files (`held=true`). It has no share, edit or delete control, and
  it is rendered only while the caller can see a held file (`count > 0`), so it disappears by
  itself when the last hold is released. It names no reason.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { t } from '$stores/locale';

  export let count = 0;
  export let selected = false;

  const dispatch = createEventDispatcher<{ select: void }>();
</script>

{#if count > 0}
  <button
    type="button"
    class="held-collection"
    class:selected
    aria-pressed={selected}
    title={$t('holds.virtual.collectionHint')}
    data-testid="held-collection"
    on:click={() => dispatch('select')}
  >
    <span class="icon" aria-hidden="true"><MenuIcon name="shield" size={18} /></span>
    <span class="info">
      <span class="name">{$t('holds.virtual.collection')}</span>
      <span class="hint">{$t('holds.virtual.collectionHint')}</span>
    </span>
    <span class="count">{$t('holds.virtual.collectionCount', { count })}</span>
    <span class="lock" aria-hidden="true"><MenuIcon name="lock" size={14} /></span>
  </button>
{/if}

<style>
  .held-collection {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    width: 100%;
    margin-block-end: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border: 1px dashed var(--color-warning-border);
    border-radius: var(--radius-lg);
    background: var(--color-warning-bg);
    color: var(--text-primary);
    text-align: start;
    font-weight: var(--font-weight-regular);
    box-shadow: none;
    cursor: pointer;
  }

  .held-collection:hover,
  .held-collection.selected {
    border-style: solid;
    background: var(--color-warning-bg);
    box-shadow: none;
  }

  .held-collection:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .icon,
  .lock {
    display: inline-flex;
    flex-shrink: 0;
    color: var(--color-warning-text);
  }

  .info {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    flex: 1;
    min-width: 0;
  }

  .name {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
  }

  .hint {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .count {
    flex-shrink: 0;
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }
</style>
