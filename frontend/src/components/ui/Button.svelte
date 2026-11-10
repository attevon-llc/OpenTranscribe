<!--
  Button.svelte: behaviour wrapper over the global `.btn` classes in styles/form-elements.css.

  Looks live in CSS; this component exists for the behaviour that was hand-rolled per file:
  a busy state that keeps focus (aria-disabled, not `disabled`), a `type` that defaults to
  "button" (never the browser's "submit"), and an icon-only mode that requires an accessible name.
  Plain buttons can keep `<button class="btn btn-primary">`.
-->
<script lang="ts">
  import Spinner from './Spinner.svelte';

  export let variant:
    | 'primary'
    | 'secondary'
    | 'danger'
    | 'warning'
    | 'success'
    | 'ghost'
    | 'link'
    | 'danger-outline' = 'secondary';
  export let size: 'sm' | 'md' | 'lg' = 'md';
  export let type: 'button' | 'submit' | 'reset' = 'button';
  export let disabled = false;
  /** Keeps focus (aria-disabled instead of `disabled`) and swallows clicks and submits. */
  export let busy = false;
  /** Square icon button; needs `ariaLabel` for its accessible name. */
  export let iconOnly = false;
  export let ariaLabel = '';

  $: if (iconOnly && !ariaLabel) {
    console.error('Button: iconOnly requires ariaLabel');
  }

  // Registered before the forwarded on:click, so stopImmediatePropagation keeps a busy or
  // disabled button from reaching the parent's handler.
  function swallowWhenInert(event: MouseEvent) {
    if (busy || disabled) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }
</script>

<button
  {...$$restProps}
  {type}
  {disabled}
  class="btn btn-{variant}"
  class:btn-sm={size === 'sm'}
  class:btn-lg={size === 'lg'}
  class:btn-icon={iconOnly}
  aria-label={ariaLabel || undefined}
  aria-disabled={busy ? 'true' : undefined}
  aria-busy={busy ? 'true' : undefined}
  on:click={swallowWhenInert}
  on:click
>
  {#if busy}<Spinner size="small" />{/if}
  <slot name="icon" />
  <slot />
</button>
