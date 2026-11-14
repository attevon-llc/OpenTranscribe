<script lang="ts">
  /**
   * Government Classification Banner Component
   *
   * Displays a standard DoD/FedRAMP classification banner with proper styling
   * per MIL-STD-129 and DoDM 5200.01 marking requirements.
   *
   * Classification levels and their colors:
   * - UNCLASSIFIED: Green (#007a33)
   * - CUI/FOUO: Yellow/Gold (#ffc72c) with black text
   * - CONFIDENTIAL: Blue (#00538b)
   * - SECRET: Red (#c8102e)
   * - TOP SECRET: Orange (#ff6600)
   * - TOP SECRET/SCI: Yellow (#ffc72c) with black text
   *
   * The marking strip only. The AC-8 consent dialog is `$components/LoginBanner.svelte`;
   * this component used to carry a second, English-only copy of it, and the login page
   * showed both at once.
   */

  import { t } from '$stores/locale';
  import { colorsFor } from '$lib/utils/classificationColors';

  export let classification: 'UNCLASSIFIED' | 'CUI' | 'FOUO' | 'CONFIDENTIAL' | 'SECRET' | 'TOP SECRET' | 'TOP SECRET//SCI' = 'UNCLASSIFIED';
  export let position: 'top' | 'both' = 'top';

  $: colors = colorsFor(classification);
  // The marking itself stays as written: classification levels are regulatory markings, not copy.
  $: markingLabel = $t('classificationBanner.markingLabel', { level: classification });
</script>

<!-- Top Banner -->
<div
  class="classification-banner"
  style="background-color: {colors.bg}; color: {colors.text};"
  role="banner"
  aria-label={markingLabel}
>
  <span class="classification-text">{classification}</span>
</div>

{#if position === 'both'}
  <!-- Bottom Banner (sticky) -->
  <div
    class="classification-banner classification-banner-bottom"
    style="background-color: {colors.bg}; color: {colors.text};"
    role="contentinfo"
    aria-label={markingLabel}
  >
    <span class="classification-text">{classification}</span>
  </div>
{/if}

<style>
  .classification-banner {
    width: 100%;
    text-align: center;
    padding: var(--space-1) var(--space-2);
    font-family: 'Roboto Condensed', 'Arial Narrow', Arial, sans-serif;
    font-weight: 700;
    font-size: var(--font-size-md);
    letter-spacing: 0.5px;
    text-transform: uppercase;
    /* Same tier as ToastContainer (H5) — both are globally-rendered and can be
       bottom-anchored (`.classification-banner-bottom`) at once; there is no
       measured reason to prefer one above the other, so they share a layer
       rather than guessing an order. Flagged for human visual confirmation. */
    z-index: var(--z-toast);
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
  }

  .classification-banner-bottom {
    top: auto;
    bottom: 0;
  }

  .classification-text {
    display: inline-block;
  }
</style>
