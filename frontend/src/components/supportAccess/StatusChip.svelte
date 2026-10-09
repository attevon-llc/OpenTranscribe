<script lang="ts">
  /**
   * A small status pill whose text clears WCAG AA in BOTH themes (>= 4.5:1 at 11px).
   *
   * Not `ui/Badge`: its warning/success/error variants use the saturated brand colours as TEXT
   * (#f59e0b on a pale tint is 1.8:1, #f87171 on the dark tint is 4.46:1), which axe flags as
   * serious. Here the text is the theme-aware `*-dark` shade, which is dark in light mode and
   * pale in dark mode, over a faint tint of the same hue. Colours are the app's tokens; only
   * success (and light-mode warning) have no suitable text-shade token, so those values are spelled out.
   */
  export let tone: 'success' | 'warning' | 'error' | 'info' | 'neutral' = 'neutral';
</script>

<span class="chip {tone}"><slot /></span>

<style>
  .chip {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border: 1px solid transparent;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    line-height: 1.4;
    white-space: nowrap;
  }
  .success {
    background: rgba(var(--success-color-rgb), 0.12);
    color: #047857;
  }
  :global([data-theme='dark']) .success {
    color: #6ee7b7;
  }
  /* --warning-dark (#b45309) is 4.2:1 on the banner's amber tint; amber-800 clears 6:1. */
  .warning {
    background: rgba(var(--warning-color-rgb), 0.14);
    color: #92400e;
  }
  :global([data-theme='dark']) .warning {
    color: var(--warning-dark);
  }
  .error {
    background: rgba(var(--error-color-rgb), 0.12);
    color: var(--error-dark);
  }
  .info {
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--info-dark);
  }
  .neutral {
    background: var(--surface-secondary);
    border-color: var(--border-color);
    color: var(--text-on-tint);
  }
</style>
