<script lang="ts">
  // Renders TranscriptExportDialogs with its bound page state visible
  // (TranscriptExportDialogs.test.ts only).
  import TranscriptExportDialogs from './TranscriptExportDialogs.svelte';

  export let file: { uuid: string; filename: string } | null = null;
  export let diarizationDisabled = false;
  export let showOriginal = false;
  export let schedule: (callback: () => void, delayMs: number) => void;

  let copyStatus: 'idle' | 'copying' | 'copied' | 'failed' | 'empty' = 'idle';
  let documentBusy = false;
  let inner: TranscriptExportDialogs;

  export function openExport(format: string, hasComments: boolean) {
    return inner.openExport(format, hasComments);
  }

  export function copyTranscript() {
    return inner.copyTranscript();
  }
</script>

<TranscriptExportDialogs
  bind:this={inner}
  bind:copyStatus
  bind:documentBusy
  {file}
  {diarizationDisabled}
  {showOriginal}
  {schedule}
/>
<span data-testid="copy-status">{copyStatus}</span>
<span data-testid="document-busy">{documentBusy}</span>
