<!--
  SettingsSkeleton.svelte — the one loading placeholder for a settings panel.

  Settings panels used to load three different ways: plain "Loading..." text, a large spinner, and
  per-panel grey chip rows that resembled nothing on the page. This draws the shape of what is
  about to appear, with the shared shimmer, so every section loads alike:

    form   label + field rows (most panels)
    rows   label + toggle rows (preference lists)
    chips  a status-chip row
    dashboard  a status-chip row above two content cards

  `label` makes it a status region with that name; leave it off when a parent (LazySection) already
  announces the load.
-->
<script lang="ts">
  import './skeleton-shimmer.css';

  export let variant: 'form' | 'rows' | 'chips' | 'dashboard' = 'form';
  /** Rows for `form` / `rows`; cards (max 2) for `dashboard`. */
  export let count = 3;
  export let label = '';

  $: items = Array.from({ length: Math.max(1, count) });
</script>

<div
  class="settings-skeleton"
  role={label ? 'status' : undefined}
  aria-busy="true"
  aria-label={label || undefined}
  data-testid="settings-skeleton"
  data-variant={variant}
>
  {#if variant === 'chips' || variant === 'dashboard'}
    <div class="chips-row">
      <div class="sk-chip shimmer"></div>
      <div class="sk-chip wide shimmer"></div>
      <div class="sk-chip shimmer"></div>
    </div>
  {/if}
  {#if variant === 'chips'}
    <!-- chip row only -->
  {:else if variant === 'dashboard'}
    {#each items.slice(0, 2) as _, i (i)}
      <div class="sk-card">
        <div class="sk-title shimmer"></div>
        <div class="sk-text shimmer"></div>
        <div class="sk-text short shimmer"></div>
      </div>
    {/each}
  {:else if variant === 'rows'}
    {#each items as _, i (i)}
      <div class="sk-toggle-row">
        <div class="sk-label shimmer" style="width: {32 + ((i * 19) % 26)}%"></div>
        <div class="sk-toggle shimmer"></div>
      </div>
    {/each}
  {:else}
    {#each items as _, i (i)}
      <div class="sk-field">
        <div class="sk-label shimmer" style="width: {22 + ((i * 13) % 20)}%"></div>
        <div class="sk-input shimmer"></div>
      </div>
    {/each}
  {/if}
</div>

<style>
  .settings-skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding: var(--space-1) 0;
  }

  .sk-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .sk-label {
    height: 12px;
    border-radius: var(--radius-sm);
  }

  .sk-input {
    height: 38px;
    border-radius: var(--radius-md);
  }

  .sk-toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    padding: var(--space-2) 0;
  }

  .sk-toggle {
    flex: none;
    width: 40px;
    height: 22px;
    border-radius: var(--radius-pill);
  }

  .chips-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .sk-chip {
    width: 90px;
    height: 28px;
    border-radius: var(--radius-pill);
  }

  .sk-chip.wide {
    width: 130px;
  }

  .sk-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-2-5);
    padding: var(--space-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
  }

  .sk-title {
    width: 38%;
    height: 16px;
    border-radius: var(--radius-sm);
  }

  .sk-text {
    height: 12px;
    border-radius: var(--radius-sm);
  }

  .sk-text.short {
    width: 62%;
  }
</style>
