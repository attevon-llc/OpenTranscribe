<!--
  Spinner.svelte — the one loading spinner (replaces the local `.spinner` copies).

  Accessibility: a spinner is decorative when visible text sits beside it ("Saving..."), so with no
  `label` it is `aria-hidden`. A spinner that stands alone passes an already-translated `label`
  and becomes a `role="status"` live region carrying that text (WAI-ARIA `status` role).
-->
<script lang="ts">
  export let size: 'small' | 'inline' | 'medium' | 'large' = 'medium';
  export let color: string = '';
  /** Translated accessible name for a spinner with no adjacent text. */
  export let label: string = '';
</script>

<span
  class="spinner-wrap"
  role={label ? 'status' : undefined}
  aria-hidden={label ? undefined : 'true'}
>
  <span
    class="spinner spinner-{size}"
    class:custom-color={!!color}
    style={color ? `border-top-color: ${color}; border-right-color: ${color}` : ''}
    aria-hidden="true"
  ></span>
  {#if label}<span class="sr-only">{label}</span>{/if}
</span>

<style>
  .spinner-wrap {
    display: inline-flex;
    vertical-align: middle;
  }

  .spinner {
    display: inline-block;
    border-radius: var(--radius-round);
    border-style: solid;
    border-color: color-mix(in srgb, var(--text-color) 10%, transparent);
    animation: spin 0.8s linear infinite;
  }

  .spinner:not(.custom-color) {
    border-top-color: var(--primary-color);
    border-right-color: var(--primary-color);
  }

  .spinner-small {
    width: 14px;
    height: 14px;
    border-width: 2px;
  }

  .spinner-inline {
    width: 1rem;
    height: 1rem;
    border-width: 2px;
  }

  .spinner-medium {
    width: 24px;
    height: 24px;
    border-width: 3px;
  }

  .spinner-large {
    width: 40px;
    height: 40px;
    border-width: 4px;
  }

  @media (prefers-reduced-motion: reduce) {
    .spinner {
      animation-duration: 2.4s;
    }
  }
</style>
