<script lang="ts">
  // Test host: proves `bind:value` flows both ways through RangeSlider and that
  // component-level `on:input` / `on:change` receive the native event.
  import RangeSlider from './RangeSlider.svelte';

  export let value = 0.5;
  let lastInput = '';
  let lastChange = '';
</script>

<RangeSlider
  id="host-slider"
  label="Host"
  bind:value
  on:input={(e) => (lastInput = (e.target as HTMLInputElement).value)}
  on:change={(e) => (lastChange = (e.target as HTMLInputElement).value)}
/>
<output data-testid="host-value">{value}</output>
<output data-testid="host-input">{lastInput}</output>
<output data-testid="host-change">{lastChange}</output>
<button data-testid="host-set" on:click={() => (value = 0.9)}>set</button>
