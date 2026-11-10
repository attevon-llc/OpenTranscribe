<script lang="ts">
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import type { MenuIconName } from '$lib/ui/menuIcons';

  /** A named SVG glyph; pass the `icon` slot for anything richer. Never an emoji. */
  export let icon: MenuIconName | '' = '';
  export let title: string = '';
  export let description: string = '';
  export let padding: string = '48px 24px';
  /**
   * Title size. `body` (15px) is the default; a settings panel whose empty state used a
   * larger heading passes `lg` (17px) or `xl` (20px) so its title never shrinks.
   */
  export let titleSize: 'body' | 'lg' | 'xl' = 'body';
  /** A dashed card outline, for an empty list inside a settings panel. */
  export let bordered: boolean = false;
</script>

<!--
  One vertical rhythm for every empty state: icon -> title -> description -> actions.
  Anything passed in the default slot is an action row and gets the shared action gap
  (`--empty-state-action-gap`), so callers never add their own margin to a button here.
-->
<div class="empty-state" class:bordered style="padding: {padding}">
  {#if $$slots.icon}
    <div class="empty-icon"><slot name="icon" /></div>
  {:else if icon}
    <div class="empty-icon"><MenuIcon name={icon} size="xl" /></div>
  {/if}
  {#if title}
    <h3 class="empty-title empty-title-{titleSize}">{title}</h3>
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

  .empty-state.bordered {
    border: 2px dashed var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--card-background);
  }

  .empty-icon {
    font-size: 2.5rem;
    line-height: 1;
    margin-bottom: var(--space-3);
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

  .empty-title-lg {
    font-size: var(--font-size-lg);
  }

  .empty-title-xl {
    font-size: var(--font-size-xl);
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
    gap: var(--space-2);
    margin-top: var(--empty-state-action-gap);
  }
</style>
