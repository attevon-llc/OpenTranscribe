/**
 * Typed API client for Watch Sources (auto-import from local / S3 / SMB).
 *
 * Mirrors the backend Pydantic schemas. All requests go through the shared
 * axiosInstance (cookie auth, CSRF, 401 refresh); pagination/filtering are
 * pushed to the server. Errors surface via $lib/utils/apiError at call sites.
 */

import axiosInstance from '$lib/axios';

export type SourceType = 'local' | 's3' | 'smb';
export type EmailProvider = 'smtp' | 'm365' | 'exchange';

/** Observer mode a source actually ended up with (issue #294). */
type FsEventsMode = 'native' | 'polling' | 'error' | 'unavailable';

/** Observer selection policy (global admin setting). */
type FsEventsPolicy = 'auto' | 'native' | 'polling' | 'off';

/**
 * Live FS-watching status published by the beat supervisor.
 *
 * Absent/null means nothing is watching the source right now, so the scheduled
 * scan is the only mechanism — the status key expires on its own if the beat
 * container stops, so this never claims a watcher that is not running.
 */
interface FsEventsStatus {
  mode: FsEventsMode;
  active: boolean;
  detail?: string | null;
  fs_type?: string | null;
  debounce_seconds?: number | null;
  poll_seconds?: number | null;
  since?: string | null;
  last_event_at?: string | null;
  events_seen: number;
  scans_dispatched: number;
  updated_at?: string | null;
}

export interface WatchSource {
  uuid: string;
  name: string;
  source_type: SourceType;
  is_enabled: boolean;
  local_path?: string | null;
  delete_after_import: boolean;
  s3_endpoint_url?: string | null;
  s3_bucket_name?: string | null;
  s3_prefix?: string | null;
  s3_region?: string | null;
  s3_access_key_id?: string | null;
  s3_use_ssl: boolean;
  has_s3_secret_key: boolean;
  smb_server?: string | null;
  smb_share?: string | null;
  smb_path?: string | null;
  smb_username?: string | null;
  smb_domain?: string | null;
  smb_port: number;
  has_smb_password: boolean;
  polling_interval_minutes: number;
  use_fs_events: boolean;
  fs_events?: FsEventsStatus | null;
  file_extensions?: string | null;
  skip_files_older_than_days?: number | null;
  recursive: boolean;
  auto_transcribe: boolean;
  min_speakers?: number | null;
  max_speakers?: number | null;
  collection_ids?: string[] | null;
  tag_names?: string[] | null;
  multipart_enabled: boolean;
  multipart_regex: string;
  multipart_time_window_hours: number;
  multipart_wait_scans: number;
  upload_stitched_to_source: boolean;
  last_scan_at?: string | null;
  last_scan_status?: string | null;
  last_scan_message?: string | null;
  last_scan_files_found: number;
  last_scan_files_imported: number;
  last_scan_files_skipped: number;
  last_scan_duration_seconds?: number | null;
  total_files_imported: number;
  owner_name?: string | null;
  owner_uuid?: string | null;
  is_own: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface WatchSourceCreate {
  name: string;
  source_type: SourceType;
  is_enabled?: boolean;
  local_path?: string | null;
  delete_after_import?: boolean;
  s3_endpoint_url?: string | null;
  s3_bucket_name?: string | null;
  s3_prefix?: string | null;
  s3_region?: string | null;
  s3_access_key_id?: string | null;
  s3_secret_key?: string | null;
  s3_use_ssl?: boolean;
  smb_server?: string | null;
  smb_share?: string | null;
  smb_path?: string | null;
  smb_username?: string | null;
  smb_password?: string | null;
  smb_domain?: string | null;
  smb_port?: number;
  polling_interval_minutes?: number;
  use_fs_events?: boolean;
  file_extensions?: string | null;
  skip_files_older_than_days?: number | null;
  recursive?: boolean;
  auto_transcribe?: boolean;
  min_speakers?: number | null;
  max_speakers?: number | null;
  collection_ids?: string[] | null;
  tag_names?: string[] | null;
  multipart_enabled?: boolean;
  multipart_regex?: string;
  multipart_time_window_hours?: number;
  multipart_wait_scans?: number;
  upload_stitched_to_source?: boolean;
  assign_to_user_uuid?: string | null;
}

export type WatchSourceUpdate = Partial<
  Omit<WatchSourceCreate, 'source_type' | 'assign_to_user_uuid'>
>;

export interface WatchSourceFile {
  uuid: string;
  remote_path: string;
  filename: string;
  file_size?: number | null;
  file_modified_at?: string | null;
  imohash?: string | null;
  media_file_uuid?: string | null;
  status: string;
  skip_reason?: string | null;
  part_group?: string | null;
  part_number?: number | null;
  error_message?: string | null;
  /**
   * Two meanings, keyed by `status`: failed import ATTEMPTS on an ordinary row
   * (`_record_error` increments it), and SCANS WAITED on a `waiting_for_parts` row
   * (the multipart group's stitch timer). Label it per row — one heading for both
   * misreports one of them.
   */
  retry_count: number;
  processed_at?: string | null;
  created_at?: string | null;
}

export interface WatchSourceFilesList {
  files: WatchSourceFile[];
  total: number;
  page: number;
  page_size: number;
}

/**
 * Every tracking status the backend's `WatchFileStatus` defines, for the filter
 * dropdown. A row may still carry a value absent from this list (`skipped_too_large`
 * is written by the document ingest path and not yet an enum member — see #547), so
 * anything rendering a status must fall back to the raw string rather than blank.
 */
export const WATCH_FILE_STATUSES = [
  'pending',
  'downloading',
  'importing',
  'imported',
  'skipped_duplicate',
  'skipped_old',
  'skipped_invalid',
  'processing',
  'error',
  'stitched_part',
  'waiting_for_parts',
] as const;

/**
 * Statuses the retry endpoint accepts, mirroring `RETRYABLE_FILE_STATUSES` in
 * `schemas/watch_source.py`. The UI hides Retry for anything else so it never offers
 * a button the API will refuse — the exclusions are each a distinct harm (duplicating
 * an imported file, racing an in-flight claim, corrupting a multipart wait counter,
 * re-importing a part already folded into a stitched recording).
 */
export const RETRYABLE_FILE_STATUSES: ReadonlySet<string> = new Set([
  'error',
  'skipped_duplicate',
  'skipped_old',
  'skipped_invalid',
]);

/** Per-row outcome of a batch retry or batch delete. */
export interface WatchSourceFileActionResult {
  file_uuid: string;
  success: boolean;
  status?: string | null;
  /** Set when the action applied but is unlikely to achieve anything. */
  warning?: string | null;
  error?: string | null;
}

export interface WatchSourceFileActionResponse {
  results: WatchSourceFileActionResult[];
  scan_dispatched: boolean;
}

export interface WatchSourceStats {
  total: number;
  imported: number;
  skipped: number;
  error: number;
  pending: number;
  waiting_for_parts: number;
}

export interface ConnectionTestResult {
  success: boolean;
  message: string;
  latency_ms?: number | null;
}

export interface ScanResult {
  status: string;
  message: string;
  task_id?: string | null;
}

interface DirectoryEntry {
  name: string;
  path: string;
}

export interface DirectoryListing {
  current_path: string;
  parent_path?: string | null;
  directories: DirectoryEntry[];
}

export interface Capabilities {
  watch_source_enabled: boolean;
  local_enabled: boolean;
  fs_events_enabled: boolean;
  fs_events_mode: FsEventsPolicy;
}

export interface MultipartRegexTestResult {
  matched: boolean;
  base_name?: string | null;
  part_number?: number | null;
  extension?: string | null;
  error?: string | null;
}

export interface GlobalWatchSettings {
  enabled: boolean;
  file_stability_seconds: number;
  /** Per-scan cap on files imported, NOT a concurrency limit — imports run serially. */
  max_imports_per_scan: number;
  fs_events_enabled: boolean;
  /** How the observer is chosen per source; `auto` detects and falls back. */
  fs_events_mode: FsEventsPolicy;
  /** Stat-sweep interval for the polling-observer fallback. */
  fs_events_poll_seconds: number;
}

export interface EmailConfig {
  uuid: string;
  name: string;
  provider: EmailProvider;
  is_enabled: boolean;
  from_address?: string | null;
  default_recipients?: string | null;
  smtp_host?: string | null;
  smtp_port?: number | null;
  smtp_use_tls: boolean;
  smtp_username?: string | null;
  has_smtp_password: boolean;
  m365_tenant_id?: string | null;
  m365_client_id?: string | null;
  has_m365_secret: boolean;
  exchange_server?: string | null;
  exchange_domain?: string | null;
  exchange_username?: string | null;
  has_exchange_password: boolean;
  last_tested_at?: string | null;
  test_status?: string | null;
  test_message?: string | null;
  /**
   * How many watch sources subscribe to this config. Deleting it cascades the links
   * away, so those sources stop being notified with no warning and no way to restore
   * them — this is what makes that consequence visible before the decision.
   */
  linked_source_count?: number;
}

export interface EmailConfigCreate {
  name: string;
  provider: EmailProvider;
  is_enabled?: boolean;
  from_address?: string | null;
  default_recipients?: string | null;
  smtp_host?: string | null;
  smtp_port?: number | null;
  smtp_use_tls?: boolean;
  smtp_username?: string | null;
  smtp_password?: string | null;
  m365_tenant_id?: string | null;
  m365_client_id?: string | null;
  m365_client_secret?: string | null;
  exchange_server?: string | null;
  exchange_domain?: string | null;
  exchange_username?: string | null;
  exchange_password?: string | null;
}

export type EmailConfigUpdate = Partial<Omit<EmailConfigCreate, 'provider'>>;

export interface EmailLinkCreate {
  email_config_uuid: string;
  additional_recipients?: string | null;
  notify_on_success?: boolean;
  notify_on_error?: boolean;
}

/** An email config linked to one source, with that link's own options. */
export interface EmailLink {
  email_config_uuid: string;
  email_config_name: string;
  additional_recipients?: string | null;
  notify_on_success: boolean;
  notify_on_error: boolean;
}

/**
 * A config a source owner may link, as the backend's MINIMAL projection.
 *
 * Deliberately not `EmailConfig`: managing configs is super_admin work because they
 * hold mailbox credentials, but any source owner may subscribe their own source to
 * one — so this carries nothing an ordinary user has no business seeing. No
 * `from_address`, no `smtp_host`, no usernames.
 */
export interface EmailConfigOption {
  uuid: string;
  name: string;
  /** A disabled config is skipped at send time, so a link to one delivers nothing. */
  is_enabled: boolean;
  provider: EmailProvider;
  /**
   * Whether the config has default recipients — NOT who they are. A link whose config
   * has none and which adds none of its own resolves to an empty recipient list and is
   * silently skipped.
   */
  has_default_recipients: boolean;
}

const BASE = '/watch-sources';

// ----- sources -----
export async function getCapabilities(): Promise<Capabilities> {
  const { data } = await axiosInstance.get(`${BASE}/capabilities`);
  return data;
}

export async function getWatchSources(scope: 'own' | 'all' = 'own'): Promise<WatchSource[]> {
  const { data } = await axiosInstance.get(`${BASE}`, { params: { scope } });
  return data.sources ?? [];
}

export async function getWatchSource(uuid: string): Promise<WatchSource> {
  const { data } = await axiosInstance.get(`${BASE}/${uuid}`);
  return data;
}

export async function createWatchSource(payload: WatchSourceCreate): Promise<WatchSource> {
  const { data } = await axiosInstance.post(`${BASE}`, payload);
  return data;
}

export async function updateWatchSource(
  uuid: string,
  payload: WatchSourceUpdate
): Promise<WatchSource> {
  const { data } = await axiosInstance.put(`${BASE}/${uuid}`, payload);
  return data;
}

export async function deleteWatchSource(uuid: string): Promise<void> {
  await axiosInstance.delete(`${BASE}/${uuid}`);
}

export async function testWatchSource(uuid: string): Promise<ConnectionTestResult> {
  const { data } = await axiosInstance.post(`${BASE}/${uuid}/test`);
  return data;
}

export async function scanWatchSource(uuid: string): Promise<ScanResult> {
  const { data } = await axiosInstance.post(`${BASE}/${uuid}/scan`);
  return data;
}

// ----- files -----
export async function getWatchSourceFiles(
  uuid: string,
  page = 1,
  pageSize = 50,
  status?: string,
  q?: string
): Promise<WatchSourceFilesList> {
  const { data } = await axiosInstance.get(`${BASE}/${uuid}/files`, {
    params: { page, page_size: pageSize, status, q },
  });
  return data;
}

/**
 * Re-queue tracked files, then let the backend dispatch ONE scan for the batch.
 *
 * Batch even for a single row: `scan_single` holds a Redis lock per source, so a
 * per-file call would dispatch one scan per file and every one after the first would
 * silently no-op. The rows come back `pending` — they are *queued*, not imported, and
 * the caller should say so and watch for the real outcome rather than claim success.
 */
export async function retryWatchSourceFiles(
  uuid: string,
  fileUuids: string[]
): Promise<WatchSourceFileActionResponse> {
  const { data } = await axiosInstance.post(`${BASE}/${uuid}/files/retry`, {
    file_uuids: fileUuids,
  });
  return data;
}

/** Remove many tracking rows at once. Deletes records only, never library files. */
export async function bulkDeleteWatchSourceFiles(
  uuid: string,
  fileUuids: string[]
): Promise<WatchSourceFileActionResponse> {
  const { data } = await axiosInstance.post(`${BASE}/${uuid}/files/bulk-delete`, {
    file_uuids: fileUuids,
  });
  return data;
}

export async function getWatchSourceStats(uuid: string): Promise<WatchSourceStats> {
  const { data } = await axiosInstance.get(`${BASE}/${uuid}/files/stats`);
  return data;
}

export async function deleteWatchSourceFile(uuid: string, fileUuid: string): Promise<void> {
  await axiosInstance.delete(`${BASE}/${uuid}/files/${fileUuid}`);
}

// ----- folder browser + regex tester -----
export async function browseDirectories(path = ''): Promise<DirectoryListing> {
  const { data } = await axiosInstance.get(`${BASE}/browse`, { params: { path } });
  return data;
}

export async function testMultipartRegex(
  regex: string,
  filename: string
): Promise<MultipartRegexTestResult> {
  const { data } = await axiosInstance.post(`${BASE}/test-multipart-regex`, { regex, filename });
  return data;
}

// ----- global settings (admin) -----
export async function getGlobalSettings(): Promise<GlobalWatchSettings> {
  const { data } = await axiosInstance.get(`${BASE}/settings`);
  return data;
}

export async function updateGlobalSettings(
  payload: Partial<GlobalWatchSettings>
): Promise<GlobalWatchSettings> {
  const { data } = await axiosInstance.put(`${BASE}/settings`, payload);
  return data;
}

// ----- email configs (admin) -----
export async function getEmailConfigs(): Promise<EmailConfig[]> {
  const { data } = await axiosInstance.get(`${BASE}/email-configs`);
  return data.configs ?? [];
}

export async function createEmailConfig(payload: EmailConfigCreate): Promise<EmailConfig> {
  const { data } = await axiosInstance.post(`${BASE}/email-configs`, payload);
  return data;
}

export async function updateEmailConfig(
  uuid: string,
  payload: EmailConfigUpdate
): Promise<EmailConfig> {
  const { data } = await axiosInstance.put(`${BASE}/email-configs/${uuid}`, payload);
  return data;
}

export async function deleteEmailConfig(uuid: string): Promise<void> {
  await axiosInstance.delete(`${BASE}/email-configs/${uuid}`);
}

export async function testEmailConfig(
  uuid: string
): Promise<{ success: boolean; message: string }> {
  const { data } = await axiosInstance.post(`${BASE}/email-configs/${uuid}/test`);
  return data;
}

// ----- email links -----
/** The configs this source notifies, with each link's own options. */
export async function getEmailLinks(uuid: string): Promise<EmailLink[]> {
  const { data } = await axiosInstance.get(`${BASE}/${uuid}/emails`);
  return data ?? [];
}

/**
 * Configs this source could still be linked to, already excluding the linked ones.
 *
 * Source-scoped rather than a second `/email-configs` listing: that prefix is
 * super_admin, and this has to be readable by the source owner — who may link a
 * config but may not manage one.
 */
export async function getAvailableEmailConfigs(uuid: string): Promise<EmailConfigOption[]> {
  const { data } = await axiosInstance.get(`${BASE}/${uuid}/emails/available`);
  return data ?? [];
}

export async function linkEmailConfig(uuid: string, payload: EmailLinkCreate): Promise<void> {
  await axiosInstance.post(`${BASE}/${uuid}/emails`, payload);
}

export async function unlinkEmailConfig(uuid: string, configUuid: string): Promise<void> {
  await axiosInstance.delete(`${BASE}/${uuid}/emails/${configUuid}`);
}

export function getSourceTypeLabel(type: SourceType): string {
  return { local: 'Local Folder', s3: 'S3 Bucket', smb: 'SMB Share' }[type] ?? type;
}
