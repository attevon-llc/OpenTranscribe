/**
 * Regenerating a summary clears it from the file object first, so the page shows the
 * "generating" state while the request is in flight. If the request is then refused the old
 * summary still exists on the server, so the page has to put it back, not leave the file
 * looking summary-less.
 */
export interface SummaryFlags {
  has_summary?: boolean | null;
  summary_opensearch_id?: string | null;
}

export type SavedSummaryState = {
  has_summary: boolean | null | undefined;
  summary_opensearch_id: string | null | undefined;
};

/** What to restore if the regenerate request fails; call BEFORE clearing the file. */
export function saveSummaryState(file: SummaryFlags): SavedSummaryState {
  return {
    has_summary: file.has_summary,
    summary_opensearch_id: file.summary_opensearch_id,
  };
}

/** A copy of `file` with the summary flags put back to what they were. */
export function restoreAfterFailedRegenerate<T extends SummaryFlags>(
  file: T,
  saved: SavedSummaryState
): T {
  return {
    ...file,
    has_summary: saved.has_summary,
    summary_opensearch_id: saved.summary_opensearch_id,
  };
}
