/**
 * Upload size limits — the single source of truth for the whole SPA.
 *
 * These used to be declared three separate times (twice in `FileUploader.svelte`, once in
 * `MediaFilePanel.svelte`) with two different values, so the same 5 GB file uploaded when
 * dropped on its own and was rejected as "too large" when dropped alongside another file
 * (issue #298). Import from here — do not re-declare a limit in a component.
 *
 * The hard limit is admin-configurable server-side (`MAX_UPLOAD_BYTES` in
 * `backend/app/core/config.py`, enforced by `files/upload.py::validate_file_size_for_tenant`)
 * — it used to be a hardcoded 15 GB literal here that silently went stale the moment an admin
 * changed the env var (issue G10). `GET /api/system/capabilities` (`$stores/capabilities`,
 * fetched once at app bootstrap) now carries the live value; this module reads it, falling
 * back to `DEFAULT_MAX_UPLOAD_BYTES` (the backend's own coded default) only before that fetch
 * resolves or if it fails — never falling open to "no limit" on a fetch failure.
 */

import { get } from 'svelte/store';
import { capabilities } from '$stores/capabilities';

/**
 * Fallback hard-limit, matching the backend's coded default
 * (`_int_env("MAX_UPLOAD_BYTES", 15 * 1024 ** 3)` in `backend/app/core/config.py`). Used only
 * until the live value from `/system/capabilities` has loaded, or if that fetch fails.
 */
export const DEFAULT_MAX_UPLOAD_BYTES = 15 * 1024 * 1024 * 1024; // 15 GB

/**
 * Soft advisory threshold. Files above this still upload — the user is warned that it
 * will take a while. This is a warning on every path, never a rejection on any of them.
 * Not admin-configurable, unlike the hard limit below.
 */
export const LARGE_UPLOAD_WARNING_BYTES = 2 * 1024 * 1024 * 1024; // 2 GB

/**
 * The live hard-rejection threshold, in bytes. `null` means the admin explicitly disabled
 * the limit (`MAX_UPLOAD_BYTES=0` server-side) — no file is too large. Falls back to
 * {@link DEFAULT_MAX_UPLOAD_BYTES} until `$stores/capabilities` has loaded (or if it failed to).
 */
export function getMaxUploadBytes(): number | null {
  const { maxUploadBytes } = get(capabilities);
  return maxUploadBytes === undefined ? DEFAULT_MAX_UPLOAD_BYTES : maxUploadBytes;
}

/** True when the file exceeds the hard limit and must be rejected. */
export function exceedsUploadLimit(size: number): boolean {
  const limit = getMaxUploadBytes();
  return limit !== null && size > limit;
}

/** True when the file is large enough to warrant a slow-upload warning (but is allowed). */
export function warrantsLargeUploadWarning(size: number): boolean {
  return size > LARGE_UPLOAD_WARNING_BYTES;
}
