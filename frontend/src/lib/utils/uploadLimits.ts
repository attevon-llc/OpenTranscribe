/**
 * Upload size limits — the single source of truth for the whole SPA.
 *
 * These used to be declared three separate times (twice in `FileUploader.svelte`, once in
 * `MediaFilePanel.svelte`) with two different values, so the same 5 GB file uploaded when
 * dropped on its own and was rejected as "too large" when dropped alongside another file
 * (issue #298). Import from here — do not re-declare a limit in a component.
 *
 * `MAX_UPLOAD_BYTES` matches the backend's `max_filesize` in
 * `backend/app/services/media_download_service.py`; keep the two in step.
 */

/** Hard rejection threshold. Matches the backend upload limit. */
export const MAX_UPLOAD_BYTES = 15 * 1024 * 1024 * 1024; // 15 GB

/**
 * Soft advisory threshold. Files above this still upload — the user is warned that it
 * will take a while. This is a warning on every path, never a rejection on any of them.
 */
export const LARGE_UPLOAD_WARNING_BYTES = 2 * 1024 * 1024 * 1024; // 2 GB

/** True when the file exceeds the hard limit and must be rejected. */
export function exceedsUploadLimit(size: number): boolean {
  return size > MAX_UPLOAD_BYTES;
}

/** True when the file is large enough to warrant a slow-upload warning (but is allowed). */
export function warrantsLargeUploadWarning(size: number): boolean {
  return size > LARGE_UPLOAD_WARNING_BYTES;
}
