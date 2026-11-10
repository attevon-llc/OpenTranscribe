<script context="module" lang="ts">
  export type BadgeVariant = 'default' | 'success' | 'warning' | 'error' | 'info';
</script>

<script lang="ts">
  /** Visual variant — maps to the app's semantic status colors. */
  export let variant: BadgeVariant = 'default';
  /**
   * `sm` is the dense table-cell pill (11px); `md` is the standard status pill (12px).
   * Text never goes below the size a caller had before it moved onto Badge.
   */
  export let size: 'sm' | 'md' = 'sm';
  /**
   * Optional hover/AT description, when the pill's one-word label needs
   * expanding. The caller passes already-translated text — a primitive never
   * bakes in copy. Omitted by default so no empty `title` reaches the DOM.
   */
  export let title: string | null = null;
  /** Extra class on the root, for selectors that already target it (e2e `.status-badge`). */
  let className = '';
  export { className as class };
</script>

<!--
  The state is carried by the slotted text, never by colour alone (WCAG 1.4.1): callers pass the
  status word. A badge that only holds a number or a glyph needs its own accessible name.
-->
<span class="badge badge-{variant} size-{size} {className}" title={title ?? undefined}>
  <slot />
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    font-weight: var(--font-weight-semibold);
    line-height: 1.4;
    white-space: nowrap;
    border: 1px solid transparent;
  }
  .size-sm {
    font-size: var(--font-size-2xs);
  }
  .size-md {
    font-size: var(--font-size-xs);
  }
  .badge-default {
    background: var(--surface-color);
    border-color: var(--border-color);
    color: var(--text-secondary);
  }
  .badge-success {
    background: rgba(var(--success-color-rgb), 0.12);
    color: var(--success-text);
  }
  .badge-warning {
    background: rgba(var(--warning-color-rgb), 0.1);
    color: var(--warning-text);
  }
  .badge-error {
    background: rgba(var(--error-color-rgb), 0.12);
    color: var(--error-text);
  }
  .badge-info {
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-on-surface);
  }
</style>
