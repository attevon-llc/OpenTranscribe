<script lang="ts">
  // Test double for SummaryModal (FileSummary.test.ts only): shows whether it is open and
  // lets a test fire the modal's reprocess action.
  import { createEventDispatcher } from 'svelte';

  export let isOpen = false;
  export let fileId = '';
  export let fileName = '';

  const dispatch = createEventDispatcher();
</script>

{#if isOpen}
  <div data-testid="summary-open">{fileId} {fileName}</div>
{/if}
<button type="button" data-testid="reprocess-summary" on:click={() => dispatch('reprocessSummary')}
  >reprocess</button
>
