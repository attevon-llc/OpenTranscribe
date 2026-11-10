<!--
  Toggle.svelte - the one on/off switch (44x24 track).

  WAI-ARIA APG switch pattern: a native checkbox carrying role="switch", so Space toggles it
  natively and aria-checked is derived from `checked`. Enter is added because the APG lists it
  as an optional activation key for a switch and a native checkbox ignores it.

  CONTROLLED: it shows `checked` and asks the parent to change it through `onchange(next)`. A
  refused change leaves it where it was, so a failed save never leaves the control lying.
  `disabled` removes it from the tab order; `busy` keeps keyboard focus (aria-disabled) but
  ignores changes. The knob uses logical offsets, so it mirrors under dir="rtl".
-->
<script lang="ts">
  interface Props {
    checked: boolean;
    onchange: (next: boolean) => void;
    disabled?: boolean;
    busy?: boolean;
    /** Name the switch with a visible element elsewhere, or use `label`. */
    labelledby?: string;
    label?: string;
    describedby?: string;
    testid?: string;
    id?: string;
  }

  const {
    checked,
    onchange,
    disabled = false,
    busy = false,
    labelledby,
    label,
    describedby,
    testid,
    id,
  }: Props = $props();

  function change(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const next = input.checked;
    input.checked = checked;
    if (!disabled && !busy) onchange(next);
  }

  function keydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      (event.currentTarget as HTMLInputElement).click();
    }
  }
</script>

<label class="toggle">
  <input
    {id}
    type="checkbox"
    role="switch"
    {checked}
    {disabled}
    aria-checked={checked}
    aria-disabled={busy ? 'true' : undefined}
    aria-label={label}
    aria-labelledby={labelledby}
    aria-describedby={describedby}
    data-testid={testid}
    onchange={change}
    onkeydown={keydown}
  />
  <span class="slider" aria-hidden="true"></span>
</label>

<style>
  .toggle {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 24px;
    flex-shrink: 0;
  }

  input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    opacity: 0;
    cursor: pointer;
    z-index: var(--z-base);
  }

  .slider {
    position: absolute;
    inset: 0;
    background-color: var(--border-color);
    border: 1px solid var(--control-border);
    border-radius: var(--radius-round);
    transition: background-color var(--duration-base);
  }

  .slider::before {
    content: '';
    position: absolute;
    height: 18px;
    width: 18px;
    inset-inline-start: 2px;
    bottom: 2px;
    background-color: var(--text-on-solid);
    border-radius: var(--radius-round);
    transition: inset-inline-start var(--duration-base);
  }

  input:checked + .slider {
    background-color: var(--primary-color);
    border-color: var(--primary-color);
  }

  input:checked + .slider::before {
    inset-inline-start: 22px;
  }

  input:focus-visible + .slider {
    outline: 2px solid var(--focus-ring-color);
    outline-offset: 2px;
  }

  input:disabled,
  input[aria-disabled='true'] {
    cursor: not-allowed;
  }

  input:disabled + .slider,
  input[aria-disabled='true'] + .slider {
    opacity: 0.5;
  }
</style>
