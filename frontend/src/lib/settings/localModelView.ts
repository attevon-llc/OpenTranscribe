/**
 * What a model-library row shows and offers (issue #1246), as plain data.
 *
 * The server owns the catalog and the download state; this only maps them to the controls a row
 * renders. Every error is a code translated by `settings.localModels.error.<code>`, never the
 * server's text.
 */
import type {
  LanguageSupport,
  LocalModelDownloadState,
  LocalModelEntry,
} from '$lib/api/localModels';

/** Every code the library can show: download failures plus the refusals of its four routes. */
export const LOCAL_MODEL_ERROR_CODES = [
  'hf_auth_failed',
  'hf_repo_not_found',
  'hf_unreachable',
  'disk_full',
  'cache_not_writable',
  'downloads_disabled_offline',
  'download_timed_out',
  'download_failed',
  'insufficient_disk_space',
  'model_download_in_progress',
  'unknown_local_model',
  'local_model_not_downloaded',
  'local_model_already_downloaded',
  'no_model_download',
  'model_download_unavailable',
  'gpu_worker_not_found',
  'local_asr_unavailable',
] as const;

const FALLBACK_CODE = 'download_failed';

export function errorKeyFor(code: string | null | undefined): string {
  const known = (LOCAL_MODEL_ERROR_CODES as readonly string[]).includes(code ?? '');
  return `settings.localModels.error.${known ? code : FALLBACK_CODE}`;
}

/** `detail.code` of an API error body, or null (a string detail has no code). */
export function apiErrorCode(err: unknown): string | null {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (detail && typeof detail === 'object') {
    const code = (detail as { code?: unknown }).code;
    if (typeof code === 'string') return code;
  }
  return null;
}

/** Extra numbers a refusal carries (`insufficient_disk_space`), for the translated sentence. */
export function apiErrorNumbers(err: unknown): { required: number; free: number } | null {
  const detail = (err as { response?: { data?: { detail?: Record<string, unknown> } } })?.response
    ?.data?.detail;
  const required = detail?.required_bytes;
  const free = detail?.free_bytes;
  return typeof required === 'number' && typeof free === 'number' ? { required, free } : null;
}

const IN_FLIGHT = new Set(['queued', 'downloading', 'verifying']);

export function isDownloadActive(download: LocalModelDownloadState | null): boolean {
  return download !== null && IN_FLIGHT.has(download.status);
}

export type RowStatus =
  | 'active'
  | 'downloading'
  | 'queued'
  | 'verifying'
  | 'partial'
  | 'downloaded'
  | 'notDownloaded';

/** Literal keys (no key is built from a variable: `keyMaps.test.ts`). */
export const STATUS_KEYS: Record<RowStatus, string> = {
  active: 'settings.localModels.status.active',
  downloading: 'settings.localModels.status.downloading',
  queued: 'settings.localModels.status.queued',
  verifying: 'settings.localModels.status.verifying',
  partial: 'settings.localModels.status.partial',
  downloaded: 'settings.localModels.status.downloaded',
  notDownloaded: 'settings.localModels.status.notDownloaded',
};

export const LANGUAGE_KEYS: Record<LanguageSupport, string> = {
  multilingual: 'settings.localModels.language.multilingual',
  english_only: 'settings.localModels.language.english_only',
  english_optimized: 'settings.localModels.language.english_optimized',
};

export interface RowView {
  status: RowStatus;
  /** 0-100 while downloading with a known size, else null (indeterminate bar). */
  percent: number | null;
  inFlight: boolean;
  showDownload: boolean;
  downloadDisabled: boolean;
  showCancel: boolean;
  showActivate: boolean;
  activateDisabled: boolean;
  errorKey: string | null;
}

export function rowView(
  entry: LocalModelEntry,
  download: LocalModelDownloadState | null,
  options: { cacheWritable?: boolean } = {}
): RowView {
  const cacheWritable = options.cacheWritable ?? true;
  const anyActive = isDownloadActive(download);
  const mine = download !== null && download.model_id === entry.id;
  const inFlight = mine && anyActive;

  let status: RowStatus;
  if (inFlight) status = download!.status as RowStatus;
  else if (entry.is_active) status = 'active';
  else if (entry.downloaded) status = 'downloaded';
  else if (entry.partial) status = 'partial';
  else status = 'notDownloaded';

  return {
    status,
    percent: inFlight ? download!.percent ?? null : null,
    inFlight,
    showDownload: !entry.downloaded && !inFlight,
    downloadDisabled: anyActive || !cacheWritable,
    showCancel: inFlight,
    showActivate: !entry.is_active,
    activateDisabled: !entry.downloaded || inFlight,
    errorKey: mine && download!.status === 'failed' ? errorKeyFor(download!.error_code) : null,
  };
}

/** "485 MB" / "3.1 GB" in the active language. */
export function formatBytes(bytes: number, locale: string): string {
  const gigabyte = bytes >= 1_000_000_000;
  const value = gigabyte ? bytes / 1_000_000_000 : bytes / 1_000_000;
  return new Intl.NumberFormat(locale, {
    style: 'unit',
    unit: gigabyte ? 'gigabyte' : 'megabyte',
    unitDisplay: 'short',
    maximumFractionDigits: gigabyte ? 1 : 0,
  }).format(value);
}
