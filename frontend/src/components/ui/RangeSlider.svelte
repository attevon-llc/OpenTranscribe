<script lang="ts">
  import { t } from '$stores/locale';

  /** Element id; the visible label is associated through `for`/`id`. */
  export let id: string;
  export let label: string;
  export let value: number;
  export let min = 0;
  export let max = 1;
  export let step = 0.05;
  export let disabled = false;
  /** Help line under the control (e.g. "Recommended 0.30-0.50. Higher = stricter"). */
  export let hint = '';
  /** Decimals shown in the badge when `format` is not given. */
  export let decimals = 2;
  /** Appended to the badge text ("ms", "chars"). Ignored when `format` is given. */
  export let unit = '';
  /** Full control over the badge text, e.g. `(v) => `${Math.round(v * 100)}%``. */
  export let format: ((v: number) => string) | undefined = undefined;
  /** Replaces the badge text outright (e.g. "inherit" while no value is set). */
  export let valueText: string | undefined = undefined;
  /** When set, shows a muted "System default: X" line, formatted like the badge. */
  export let defaultValue: number | undefined = undefined;
  /** Extra class for the wrapper, for layout in the caller. */
  let className = '';
  export { className as class };

  /** Position 0..1, painted by styles/range-slider.css as the filled part of the track. */
  $: span = Number(max) - Number(min);
  $: fill = span > 0 ? Math.min(1, Math.max(0, (Number(value) - Number(min)) / span)) : 0;
  $: formatted = format ? format(value) : `${Number(value).toFixed(decimals)}${unit}`;
  $: badge = valueText ?? formatted;
  $: defaultText =
    defaultValue === undefined
      ? ''
      : format
        ? format(defaultValue)
        : `${Number(defaultValue).toFixed(decimals)}${unit}`;
</script>

<div class="range-slider {className}">
  <label class="range-slider__label" for={id}>
    {label}
    <slot name="label-extra" />
    <span class="range-slider__badge">{badge}</span>
  </label>
  <input
    {id}
    class="range-input"
    type="range"
    {min}
    {max}
    {step}
    {disabled}
    style:--range-fill={fill}
    bind:value
    on:input
    on:change
    {...$$restProps}
  />
  {#if hint}
    <p class="range-slider__hint">{hint}</p>
  {/if}
  {#if defaultValue !== undefined}
    <p class="range-slider__default">
      {$t('ui.rangeSlider.systemDefault', { value: defaultText })}
    </p>
  {/if}
</div>

<style>
  .range-slider__label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
    margin-bottom: 0.375rem;
  }

  .range-slider__badge {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--slider-badge-text);
    background: var(--slider-badge-bg);
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
  }

  .range-slider__hint,
  .range-slider__default {
    margin: 0.5rem 0 0 0;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .range-slider__default {
    margin-top: 0.25rem;
  }
</style>
