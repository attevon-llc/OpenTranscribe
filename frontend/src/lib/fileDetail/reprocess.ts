/**
 * The file-detail page's optimistic state for a selective reprocess (the API call itself is
 * made by `SelectiveReprocessModal`; websocket notifications carry the rest).
 */

/** Stages that change the file's status and so show it as processing. */
export function isDestructiveReprocess(stages: string[] | undefined): boolean {
  return !!(stages?.includes('transcription') || stages?.includes('rediarize'));
}

/**
 * The file as it should render the moment a reprocess starts: processing at 0% for a
 * destructive run, and no transcript or summary when full transcription is re-run.
 */
export function applyReprocessStart<T extends Record<string, unknown>>(
  file: T,
  stages: string[] | undefined
): T {
  const next: Record<string, unknown> = { ...file };
  if (isDestructiveReprocess(stages)) {
    next.status = 'processing';
    next.progress = 0;
  }
  if (stages && stages.includes('transcription')) {
    next.transcript_segments = [];
    next.has_summary = false;
    next.summary_opensearch_id = null;
  }
  return next as T;
}

/** Whether the run regenerates the summary, so its button should show as generating. */
export function reprocessRegeneratesSummary(
  stages: string[] | undefined,
  llmAvailable: boolean
): boolean {
  return !!(stages && stages.includes('summarization') && llmAvailable);
}
