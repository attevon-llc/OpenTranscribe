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

export interface DirectoryEntry {
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
  max_concurrent_imports: number;
  fs_events_enabled: boolean;
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
  status?: string
): Promise<WatchSourceFilesList> {
  const { data } = await axiosInstance.get(`${BASE}/${uuid}/files`, {
    params: { page, page_size: pageSize, status },
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
export async function linkEmailConfig(uuid: string, payload: EmailLinkCreate): Promise<void> {
  await axiosInstance.post(`${BASE}/${uuid}/emails`, payload);
}

export async function unlinkEmailConfig(uuid: string, configUuid: string): Promise<void> {
  await axiosInstance.delete(`${BASE}/${uuid}/emails/${configUuid}`);
}

export function getSourceTypeLabel(type: SourceType): string {
  return { local: 'Local Folder', s3: 'S3 Bucket', smb: 'SMB Share' }[type] ?? type;
}
