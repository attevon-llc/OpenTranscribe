/**
 * Ask the SERVER for a serialized transcript (issues #673, #821).
 *
 * `GET /api/files/{uuid}/export` is the one place transcript text is turned into
 * txt/json/csv/srt/vtt, because that is where the admin `export_locked` floor and the
 * reader's redaction policy are resolved (`files/transcript_export.py`). A client-side
 * serializer cannot consult either — it works from data already in the browser — so it
 * silently opts its format out of the policy. #673 moved the five download formats here;
 * #821 moved the last holdout, the transcript modal's clipboard copy, which was building
 * the whole consolidated transcript from `processedTranscriptSegments` and writing it
 * straight to the clipboard.
 *
 * ⚠️ **Never add a local fallback to this function.** A `catch` that serialized in the
 * browser when the request fails would reinstate the bypass for exactly the cases the
 * server refuses (503 = the policy could not be resolved, 409 = the redaction scan has
 * not produced spans yet). A refused export must stay refused.
 *
 * The i18n labels travel as query params because the backend is deliberately
 * translation-free; it renders whatever strings the caller resolved.
 */
import axiosInstance from '$lib/axios';

/** Resolved i18n strings the server renders into the exported document. */
export interface TranscriptExportLabels {
  speaker_default_label: string;
  user_comment_label: string;
  comment_type_label: string;
  csv_header_default: string;
  csv_header_with_comments: string;
}

export interface TranscriptExportRequest {
  fileUuid: string;
  /** One of the backend's `VALID_FORMATS`. */
  format: string;
  labels: TranscriptExportLabels;
  includeComments?: boolean;
  /** TXT only. */
  includeTimestamps?: boolean;
  /** TXT only. */
  includeSpeakers?: boolean;
  /**
   * The reader has revealed the original (owner/admin `?redact=false`). Sending this is a
   * REQUEST: the server grants it only when the admin floor permits, and refuses it
   * outright under `export_locked`.
   */
  showOriginal?: boolean;
  /** `blob` for a download, `text` for the clipboard. */
  responseType?: 'blob' | 'text';
}

export interface TranscriptExportResult<T = unknown> {
  data: T;
  contentType: string;
}

export async function requestTranscriptExport<T = unknown>(
  request: TranscriptExportRequest
): Promise<TranscriptExportResult<T>> {
  const response = await axiosInstance.get(`/files/${request.fileUuid}/export`, {
    params: {
      format: request.format,
      include_comments: request.includeComments ?? false,
      include_timestamps: request.includeTimestamps ?? true,
      include_speakers: request.includeSpeakers ?? true,
      ...request.labels,
      // Absent, not `true`: an omitted `redact` lets the server apply its own default,
      // and only an explicit reveal is worth putting on the wire.
      ...(request.showOriginal ? { redact: false } : {}),
    },
    responseType: request.responseType ?? 'blob',
  });

  return {
    data: response.data as T,
    contentType: String(response.headers['content-type'] || 'text/plain'),
  };
}
