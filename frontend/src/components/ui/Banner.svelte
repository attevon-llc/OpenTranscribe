<!--
  Banner.svelte — the one inline notice box (info / success / warning / error / neutral).

  Replaces the local `.banner` / `.error-message` rules. The tone is never colour alone
  (WCAG 1.4.1): every tone has its own glyph and the message is text.

  Live-region semantics (WAI-ARIA `status` / `alert` roles). A banner is usually rendered
  by an `{#if}` after something happened, which is exactly when a live region announces:
    - `live="auto"` (default): `error` -> role="alert" (assertive); every other tone ->
      role="status" (polite).
    - `live="assertive"` / `"polite"` force `alert` / `status`.
    - `live="off"` renders no role, for a banner that is already on the page at load and
      should not be announced (the page's static state, not an event).
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import type { MenuIconName } from '$lib/ui/menuIcons';

  export let tone: 'info' | 'success' | 'warning' | 'error' | 'neutral' = 'info';
  export let title: string = '';
  export let dismissible: boolean = false;
  /** Translated accessible name of the dismiss button; defaults to "Close". */
  export let dismissLabel: string = '';
  export let live: 'auto' | 'off' | 'polite' | 'assertive' = 'auto';
  /** Bottom margin after the banner; `false` where the parent already spaces its children. */
  export let spaced: boolean = true;
  /** `data-testid` for the root, so tests can keep addressing a banner by name. */
  export let testid: string = '';
  let className = '';
  export { className as class };

  const dispatch = createEventDispatcher<{ dismiss: void }>();

  const ICONS: Record<typeof tone, MenuIconName> = {
    info: 'info',
    success: 'status-success',
    warning: 'warning',
    error: 'status-error',
    neutral: 'info',
  };

  $: role =
    live === 'off'
      ? undefined
      : live === 'assertive' || (live === 'auto' && tone === 'error')
        ? 'alert'
        : 'status';
  $: closeName = dismissLabel || $t('common.close');
</script>

<div
  class="banner banner-{tone} {className}"
  class:spaced
  {role}
  data-testid={testid || undefined}
>
  <span class="banner-icon"><MenuIcon name={ICONS[tone]} size="sm" /></span>
  <div class="banner-content">
    {#if title}<div class="banner-title">{title}</div>{/if}
    <div class="banner-body"><slot /></div>
  </div>
  {#if $$slots.actions}
    <div class="banner-actions"><slot name="actions" /></div>
  {/if}
  {#if dismissible}
    <button type="button" class="banner-dismiss" aria-label={closeName} on:click={() => dispatch('dismiss')}>
      <MenuIcon name="close" size="sm" />
    </button>
  {/if}
</div>

<style>
  .banner {
    display: flex;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    border-radius: var(--radius-md);
    border-inline-start: 3px solid var(--banner-accent);
    background: var(--banner-bg);
    color: var(--text-color);
    font-size: var(--font-size-md);
    line-height: 1.45;
  }

  .banner.spaced {
    margin-bottom: var(--space-3);
  }

  .banner-info {
    --banner-accent: var(--info-color);
    --banner-bg: var(--info-bg);
    --banner-title: var(--info-text);
  }
  .banner-success {
    --banner-accent: var(--success-color);
    --banner-bg: var(--success-bg);
    --banner-title: var(--success-text);
  }
  .banner-warning {
    --banner-accent: var(--warning-color);
    --banner-bg: var(--warning-bg);
    --banner-title: var(--warning-text);
  }
  .banner-error {
    --banner-accent: var(--error-color);
    --banner-bg: var(--error-bg);
    --banner-title: var(--error-text);
  }
  .banner-neutral {
    --banner-accent: var(--border-color);
    --banner-bg: var(--surface-color);
    --banner-title: var(--text-secondary);
  }

  .banner-icon {
    display: inline-flex;
    margin-top: var(--space-0-5);
    color: var(--banner-title);
  }

  .banner-content {
    flex: 1 1 12rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .banner-title {
    font-weight: var(--font-weight-semibold);
    color: var(--banner-title);
    margin-bottom: var(--space-1);
  }

  .banner-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  .banner-dismiss {
    display: inline-flex;
    padding: var(--space-1);
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .banner-dismiss:hover {
    color: var(--text-color);
  }

  .banner-dismiss:focus-visible {
    outline: 2px solid var(--primary-on-surface);
    outline-offset: 1px;
  }
</style>
