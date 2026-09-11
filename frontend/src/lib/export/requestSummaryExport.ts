/**
 * Ask the SERVER for a serialized AI summary (issue #885).
 *
 * `GET /api/files/{uuid}/summary/export` is the one place a summary is turned into a
 * downloadable/copyable document (`summary_export_service.build_summary_export`), fed from
 * the SAME masked copy `GET /api/files/{uuid}/summary` returns
 * (`api/endpoints/summarization.py::_redacted_summary`, issue #465) — there is exactly one
 * masking implementation for both routes.
 *
 * #885 was filed as a redaction bypass and that premise was wrong: the summary read endpoint
 * already masks correctly. The real defects were in `SummaryModal.svelte`'s client-side
 * serializer — it dropped the action-items and speaker-analysis sections `SummaryDisplay`
 * renders, and it was the last client-side re-serialization of server data left in the SPA
 * (`frontend/src/components/CLAUDE.md`'s "every copyable byte comes from a server export
 * endpoint" rule, established for the transcript by issues #673/#821).
 *
 * ⚠️ **Never add a local fallback to this function.** A `catch` that serialized in the browser
 * when the request fails would reinstate exactly the class of bug this closes — the export
 * would stop reflecting whatever the server's masking or formatting actually decided.
 *
 * The i18n labels travel as query params because the backend is deliberately
 * translation-free; it renders whatever strings the caller resolved.
 */
import axiosInstance from '$lib/axios';

/** Resolved i18n strings the server renders into the exported document. */
export interface SummaryExportLabels {
  title: string;
  executiveSummary: string;
  briefSummary: string;
  majorTopics: string;
  /** Carries a literal `'{participants}'` placeholder — the server substitutes the
   * already-masked participant names into this translated label template. */
  keyParticipants: string;
  importanceHigh: string;
  importanceMedium: string;
  importanceLow: string;
  actionItems: string;
  owner: string;
  dueDate: string;
  keyDecisions: string;
  speakerAnalysis: string;
  followUpItems: string;
  /** Fully resolved (provider/model/processing-time already interpolated) — unlike every
   * other field here, this is not a bare label. */
  disclaimer: string;
}

export interface SummaryExportRequest {
  fileUuid: string;
  /** One of the backend's `VALID_SUMMARY_EXPORT_FORMATS`. Defaults to `'md'`. */
  format?: string;
  labels: SummaryExportLabels;
}

export interface SummaryExportResult {
  data: string;
  contentType: string;
}

export async function requestSummaryExport(
  request: SummaryExportRequest
): Promise<SummaryExportResult> {
  const response = await axiosInstance.get(`/files/${request.fileUuid}/summary/export`, {
    params: {
      format: request.format ?? 'md',
      title_label: request.labels.title,
      executive_summary_label: request.labels.executiveSummary,
      brief_summary_label: request.labels.briefSummary,
      major_topics_label: request.labels.majorTopics,
      key_participants_label: request.labels.keyParticipants,
      importance_high_label: request.labels.importanceHigh,
      importance_medium_label: request.labels.importanceMedium,
      importance_low_label: request.labels.importanceLow,
      action_items_label: request.labels.actionItems,
      owner_label: request.labels.owner,
      due_date_label: request.labels.dueDate,
      key_decisions_label: request.labels.keyDecisions,
      speaker_analysis_label: request.labels.speakerAnalysis,
      follow_up_items_label: request.labels.followUpItems,
      disclaimer_label: request.labels.disclaimer,
    },
    responseType: 'text',
  });

  return {
    data: response.data as string,
    contentType: String(response.headers['content-type'] || 'text/markdown'),
  };
}
