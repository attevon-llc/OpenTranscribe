<script lang="ts">
  // Renders FileSummary with its bound page state visible (FileSummary.test.ts only).
  import FileSummary from './FileSummary.svelte';

  export let file: { uuid: string; filename?: string; [key: string]: unknown };
  export let isLoading = false;

  let summaryGenerating = false;
  let generatingSummary = false;
  let inner: FileSummary;

  export function show() {
    return inner.show();
  }

  export function generate() {
    return inner.generate();
  }
</script>

<FileSummary bind:this={inner} bind:file bind:summaryGenerating bind:generatingSummary {isLoading} />
<span data-testid="flags">{summaryGenerating}|{generatingSummary}|{String(file.has_summary)}</span>
