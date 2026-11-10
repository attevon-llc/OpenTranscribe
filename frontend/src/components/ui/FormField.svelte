<!--
  FormField.svelte: label, control slot, hint and error for one form control.

  Layout lives in the global `.form-field*` classes (styles/form-elements.css). This component
  owns the accessibility wiring that was hand-rolled per form: the label is bound to the control
  by `for`/`id` (WCAG 1.3.1), the hint and error ids are handed to the control as `describedby`
  (WAI-ARIA `aria-describedby`), and `invalid` drives `aria-invalid` (WCAG 3.3.1 error
  identification; the error is also announced through `role="alert"`).

  The caller renders the control, so it keeps its own `id`, `name`, `type` and bindings:

    <FormField id="email" label={$t('auth.email')} {error} let:describedby let:invalid>
      <input id="email" aria-describedby={describedby} aria-invalid={invalid} bind:value={email} />
    </FormField>

  The `label-inline` slot sits inside the label (an InfoTip or a muted hint span); the `label-after` slot sits beside it.

  `required` only draws the asterisk (aria-hidden); the caller also sets `required` on the control
  so the state is announced natively. The `after-hint` slot holds a second hint line or a
  conditional warning below the hint; it gets an id and is part of `describedby`. The `label-after` slot puts controls beside the label, such
  as a show-password button.
-->
<script lang="ts">
  export let id: string;
  export let label: string;
  export let hint = '';
  export let error = '';
  export let required = false;
  export let size: 'md' | 'lg' = 'md';
  // 'md' keeps a 14px label (--font-size-md) for forms whose labels were already that large.
  export let labelSize: 'sm' | 'md' | 'base' = 'sm';
  // Extra class on the wrapper, e.g. a fixed-width or flex rule from the parent form.
  let className = '';
  export { className as class };

  $: hintId = hint ? `${id}-hint` : '';
  $: errorId = error ? `${id}-error` : '';
  $: extraId = $$slots['after-hint'] ? `${id}-extra` : '';
  $: describedby = [hintId, extraId, errorId].filter(Boolean).join(' ') || undefined;
  $: invalid = error !== '';
</script>

<div class="form-field {className}" class:form-field-lg={size === 'lg'}
  class:form-field-label-md={labelSize === 'md'}
  class:form-field-label-base={labelSize === 'base'}
>
  {#if $$slots['label-after']}
    <div class="form-field-header">
      <label for={id} class="form-field-label">{label}{#if required}<span aria-hidden="true"> *</span>{/if}<slot name="label-inline" /></label>
      <slot name="label-after" />
    </div>
  {:else}
    <label for={id} class="form-field-label">{label}{#if required}<span aria-hidden="true"> *</span>{/if}<slot name="label-inline" /></label>
  {/if}
  <slot {describedby} {invalid} />
  {#if hint}
    <p id={hintId} class="form-field-hint">{hint}</p>
  {/if}
  {#if extraId}
    <div id={extraId} class="form-field-extra"><slot name="after-hint" /></div>
  {/if}
  {#if error}
    <p id={errorId} class="form-field-error" role="alert">{error}</p>
  {/if}
</div>
