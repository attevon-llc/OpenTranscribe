/**
 * Ask the SERVER for a DOCX or PDF of a transcript (issue #1278).
 *
 * `POST /api/files/{uuid}/export/document` queues a render on the download worker and returns a
 * signed job id; {@link openExportJobStream} then delivers the presigned URL (or the reason the
 * job failed). The document is built, masked and numbered on the server, because that is where
 * the admin `export_locked` floor and the reader's redaction policy are resolved.
 *
 * ⚠️ Never add a local fallback. A `catch` that built a document in the browser when the job
 * fails would reinstate the bypass for exactly the cases the server refuses (409 = the redaction
 * scan is unfinished, 503 = the policy could not be resolved, a job `redaction_pending`). A
 * refused export stays refused, and the caller shows why (see `exportErrors.ts`).
 *
 * Labels travel as already-translated strings because the backend is translation-free. The two
 * page-furniture labels are TEMPLATES the server fills in; build them with
 * {@link documentTemplateLabels} so `{page}`/`{pages}`/`{date}` reach it literally.
 */
import axiosInstance from '$lib/axios';
import {
  openExportJobStream,
  type EventSourceFactory,
  type ExportJobResult,
  type ExportJobStream,
} from '$lib/export/exportJobStream';
import type { LineNumbering, PageSize } from '$lib/export/documentExportPrefs';

export interface DocumentExportLabels {
  speaker_default_label: string;
  user_comment_label: string;
  page_label: string;
  exported_label: string;
  redacted_label: string;
}

export interface DocumentExportRequest {
  fileUuid: string;
  format: 'docx' | 'pdf';
  includeComments: boolean;
  includeTimestamps: boolean;
  includeSpeakers: boolean;
  lineNumbering: LineNumbering;
  pageSize: PageSize;
  /** A REQUEST for the original text; the server grants it only to the owner and never under the floor. */
  showOriginal?: boolean;
  labels: DocumentExportLabels;
}

/** The translated page-furniture labels, with the server's placeholders passed through intact. */
export function documentTemplateLabels(
  translate: (key: string, params?: Record<string, string>) => string
): Pick<DocumentExportLabels, 'page_label' | 'exported_label'> {
  return {
    page_label: translate('documentExport.pageLabel', { page: '{page}', pages: '{pages}' }),
    exported_label: translate('documentExport.exportedLabel', { date: '{date}' }),
  };
}

/** Queue the job and start listening; the caller owns `close()` when it supersedes the job. */
export async function startDocumentExport(
  request: DocumentExportRequest,
  factory?: EventSourceFactory
): Promise<ExportJobStream> {
  const { data } = await axiosInstance.post(`/files/${request.fileUuid}/export/document`, {
    format: request.format,
    include_comments: request.includeComments,
    include_timestamps: request.includeTimestamps,
    include_speakers: request.includeSpeakers,
    line_numbering: request.lineNumbering,
    page_size: request.pageSize,
    // Absent, not `false`: only an explicit reveal is worth putting on the wire.
    ...(request.showOriginal ? { reveal_original: true } : {}),
    labels: request.labels,
  });
  return openExportJobStream(String(data.job_id), factory);
}

/** Queue, wait, and resolve with the presigned URL and the server's file name. */
export async function requestDocumentExport(
  request: DocumentExportRequest,
  factory?: EventSourceFactory
): Promise<ExportJobResult> {
  const stream = await startDocumentExport(request, factory);
  return stream.result;
}
