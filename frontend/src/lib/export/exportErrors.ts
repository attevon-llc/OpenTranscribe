/**
 * Which translated sentence tells the user why an export did not happen.
 *
 * Every export surface (a download, the clipboard copy, a DOCX/PDF job) ends in one of three
 * ways the user can act on, and used to end in `console.error` only, which left a 409 or 503 as
 * a button that silently did nothing. The mapping is by KEY so the call sites stay literal:
 *
 * - HTTP 409: the redaction scan has not produced spans yet, so the export could not be masked
 * - HTTP 503: the redaction policy could not be resolved, so the export is withheld
 * - a job `reason` from the worker (`documentExport.error*`)
 * - anything else: a generic failure
 */
import { ExportJobError } from '$lib/export/exportJobStream';

const REASON_KEYS = {
  redaction_pending: 'documentExport.errorRedactionPending',
  too_long: 'documentExport.errorTooLong',
  unavailable: 'documentExport.errorUnavailable',
  fonts_unavailable: 'documentExport.errorFontsUnavailable',
  failed: 'documentExport.errorFailed',
} as const;

function httpStatus(error: unknown): number | null {
  if (typeof error !== 'object' || error === null) return null;
  const status = (error as { response?: { status?: unknown } }).response?.status;
  return typeof status === 'number' ? status : null;
}

export function exportErrorKey(error: unknown): string {
  if (error instanceof ExportJobError && error.reason) return REASON_KEYS[error.reason];
  const status = httpStatus(error);
  if (status === 409) return 'transcript.exportRedactionPending';
  if (status === 503) return 'transcript.exportPolicyUnavailable';
  return 'transcript.exportFailed';
}
