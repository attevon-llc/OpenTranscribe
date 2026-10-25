<script lang="ts">
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import type { MenuIconName } from '$lib/ui/menuIcons';

  /** A named SVG glyph; pass the `icon` slot for anything richer. Never an emoji. */
  export let icon: MenuIconName | '' = '';
  export let title: string = '';
  export let description: string = '';
  export let padding: string = '48px 24px';
</script>

<!--
  One vertical rhythm for every empty state: icon -> title -> description -> actions.
  Anything passed in the default slot is an action row and gets the shared action gap
  (`--empty-state-action-gap`), so callers never add their own margin to a button here.
-->
<div class="empty-state" style="padding: {padding}">
  {#if $$slots.icon}
    <div class="empty-icon"><slot name="icon" /></div>
  {:else if icon}
    <div class="empty-icon"><MenuIcon name={icon} size="xl" /></div>
  {/if}
  {#if title}
    <h3 class="empty-title">{title}</h3>
  {/if}
  {#if description}
    <p class="empty-description">{description}</p>
  {/if}
  {#if $$slots.default}
    <div class="empty-actions"><slot /></div>
  {/if}
</div>

<style>
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
  }

  .empty-icon {
    font-size: 2.5rem;
    line-height: 1;
    margin-bottom: 0.75rem;
    color: var(--text-secondary);
    opacity: 0.5;
  }

  .empty-title {
    margin: 0;
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    line-height: var(--line-height-tight);
    color: var(--text-color);
  }

  .empty-description {
    margin: var(--empty-state-gap) 0 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    max-width: 360px;
    line-height: var(--line-height-body);
  }

  .empty-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 0.5rem;
    margin-top: var(--empty-state-action-gap);
  }
</style>
