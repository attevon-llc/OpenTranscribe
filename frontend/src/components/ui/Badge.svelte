<script lang="ts">
  /** Visual variant — maps to the app's semantic status colors. */
  export let variant: 'default' | 'success' | 'warning' | 'error' | 'info' = 'default';
  /**
   * Optional hover/AT description, when the pill's one-word label needs
   * expanding. The caller passes already-translated text — a primitive never
   * bakes in copy. Omitted by default so no empty `title` reaches the DOM.
   */
  export let title: string | null = null;
</script>

<span class={`badge badge-${variant}`} title={title ?? undefined}>
  <slot />
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    line-height: 1.4;
    white-space: nowrap;
    border: 1px solid transparent;
  }
  .badge-default {
    background: var(--surface-color);
    border-color: var(--border-color);
    color: var(--text-secondary);
  }
  .badge-success {
    background: rgba(var(--success-color-rgb, 34, 197, 94), 0.12);
    color: var(--success-color, #16a34a);
  }
  .badge-warning {
    background: rgba(var(--warning-color-rgb, 245, 158, 11), 0.14);
    color: var(--warning-color, #d97706);
  }
  .badge-error {
    background: rgba(var(--error-color-rgb, 239, 68, 68), 0.12);
    color: var(--error-color, #dc2626);
  }
  .badge-info {
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-color);
  }
</style>
