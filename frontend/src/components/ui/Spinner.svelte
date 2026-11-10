<!--
  Spinner.svelte — Shared loading spinner component.

  Replaces @keyframes spin duplicated across 11+ components.
-->
<script lang="ts">
  export let size: 'small' | 'medium' | 'large' = 'medium';
  export let color: string = '';
</script>

<span
  class="spinner spinner-{size}"
  class:custom-color={!!color}
  style={color ? `border-top-color: ${color}; border-right-color: ${color}` : ''}
></span>

<style>
  .spinner {
    display: inline-block;
    border-radius: var(--radius-round);
    border-style: solid;
    border-color: rgba(0, 0, 0, 0.1);
    animation: spin 0.8s linear infinite;
  }

  .spinner:not(.custom-color) {
    border-top-color: var(--accent-color);
    border-right-color: var(--accent-color);
  }

  .spinner-small {
    width: 14px;
    height: 14px;
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

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
