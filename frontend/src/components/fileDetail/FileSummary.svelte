<script lang="ts">
  /**
   * The file page's summary surface: the summary modal and its reprocess actions, the
   * header's generate action, and the `?view=summary` deep link.
   *
   * The two spinner flags are bound by the page, because websocket notifications and a
   * selective reprocess drive them too: `generatingSummary` is the request in flight,
   * `summaryGenerating` the job running. `file` is bound so a restart can clear its summary.
   * The page calls `show()` / `generate()` through `bind:this`.
   */
  import SummaryModal from '$components/SummaryModal.svelte';
  import type { SummarizeOptions } from '$lib/api/summarize';
  import { opensSummaryView } from '$lib/fileDetail/deepLinks';
  import { fetchSummaryData, startSummary } from '$lib/fileDetail/summaryActions';
  import { restoreAfterFailedRegenerate, saveSummaryState } from '$lib/summary/regenerateFailure';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { isLLMAvailable } from '$stores/llmStatus';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  export let file: {
    uuid: string;
    filename?: string;
    has_summary?: boolean | null;
    summary_opensearch_id?: string | null;
    [key: string]: unknown;
  };
  /** The page is (re)loading the file; the deep link waits for it. */
  export let isLoading = false;
  export let summaryGenerating = false;
  export let generatingSummary = false;

  let summaryData: unknown = null;
  let showSummaryModal = false;

  export async function generate() {
    if (!file?.uuid || !$isLLMAvailable) return;

    try {
      generatingSummary = true;
      // No page refresh: websocket notifications drive the status (and preserve edits).
      await startSummary(file.uuid);
    } catch (error: unknown) {
      console.error('Error generating summary:', error);
      toastStore.error(getErrorMessage(error, $t('fileDetail.failedToGenerateSummary')), 5000);
    } finally {
      generatingSummary = false;
    }
  }

  export async function show() {
    if (!summaryData && file?.uuid) summaryData = await fetchSummaryData(file.uuid);
    if (summaryData) showSummaryModal = true;
  }

  /** The summary modal's reprocess / regenerate-with-prompt actions. */
  async function restartSummary(options: SummarizeOptions, failureLog: string) {
    showSummaryModal = false;
    // Spinner on the button, and no summary on the file, until the websocket says otherwise.
    summaryGenerating = true;
    const savedSummary = saveSummaryState(file ?? {});
    if (file) {
      file = { ...file, has_summary: false, summary_opensearch_id: null };
    }

    try {
      await startSummary(file.uuid, options);
    } catch (error) {
      console.error(failureLog, error);
      toastStore.error(getErrorMessage(error, $t('fileDetail.failedToStartSummaryReprocess')), 5000);
      if (file) file = restoreAfterFailedRegenerate(file, savedSummary);
      summaryGenerating = false;
    }
  }

  // `?view=summary` opens the modal once the file has loaded (see `opensSummaryView`).
  let summaryViewHandled = false;
  $: if (!isLoading && file?.uuid) handleSummaryDeepLink();

  function handleSummaryDeepLink() {
    if (summaryViewHandled || !opensSummaryView(window.location.search)) return;
    summaryViewHandled = true;
    void show();
  }
</script>

<SummaryModal
  bind:isOpen={showSummaryModal}
  fileId={file.uuid}
  fileName={file?.filename || 'Unknown File'}
  on:close={() => (showSummaryModal = false)}
  on:reprocessSummary={() => restartSummary({ forceRegenerate: true }, 'Failed to start reprocess:')}
  on:regenerateWithPrompt={(event) =>
    restartSummary(
      { forceRegenerate: true, promptUuid: event.detail.promptUuid },
      'Failed to start regeneration with prompt:'
    )}
/>
