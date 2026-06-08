/**
 * Typed API client for scheduled database backups (admin-only, Feature C).
 *
 * Mirrors the backend Pydantic schemas in app/api/endpoints/backup_settings.py.
 * All requests go through the shared axiosInstance (cookie auth, CSRF, 401 refresh).
 * Settings are DB-backed (SystemSettings) with coded defaults — no .env vars.
 */

import axiosInstance from '$lib/axios';

const BASE = '/admin/backup';

export interface OpenSearchSnapshotResult {
  status: string; // ok | skipped | unsupported | error
  error?: string | null;
  snapshot?: string | null;
  repository?: string | null;
  duration_s?: number | null;
  pruned?: string[] | null;
}

export interface OpenSearchSnapshotStatus {
  reachable: boolean;
  repository_registered: boolean;
  last_snapshot?: string | null;
}

export interface BackupResult {
  ok: boolean;
  status: string;
  error?: string | null;
  filename?: string | null;
  path?: string | null;
  size_bytes?: number | null;
  duration_s?: number | null;
  encrypted?: boolean | null;
  pruned?: string[] | null;
  started_at?: string | null;
  opensearch?: OpenSearchSnapshotResult | null;
}

export interface DestinationStatus {
  destination: string;
  exists: boolean;
  writable: boolean;
  mounted: boolean;
}

export interface S3Status {
  bucket: string;
  prefix: string;
  endpoint_url: string;
  reachable: boolean;
  error?: string | null;
}

export type BackupDestinationType = 'local' | 's3';

export interface BackupSettings {
  enabled: boolean;
  schedule: string;
  destination: string;
  retention_daily: number;
  retention_weekly: number;
  retention_monthly: number;
  encrypt: boolean;
  passphrase_file: string;
  include_opensearch: boolean;
  destination_type: BackupDestinationType;
  s3_endpoint_url: string;
  s3_region: string;
  s3_bucket: string;
  s3_prefix: string;
  s3_access_key_id: string;
  s3_secret_key_set: boolean;
  last_run_at?: string | null;
  last_result?: BackupResult | null;
  destination_status: DestinationStatus;
  s3_status?: S3Status | null;
}

export interface BackupSettingsUpdate {
  enabled?: boolean;
  schedule?: string;
  destination?: string;
  retention_daily?: number;
  retention_weekly?: number;
  retention_monthly?: number;
  encrypt?: boolean;
  passphrase_file?: string;
  include_opensearch?: boolean;
  destination_type?: BackupDestinationType;
  s3_endpoint_url?: string;
  s3_region?: string;
  s3_bucket?: string;
  s3_prefix?: string;
  s3_access_key_id?: string;
  // Write-only: sent on save, never returned by the API.
  s3_secret_key?: string;
}

export interface BackupStatus {
  enabled: boolean;
  schedule: string;
  destination_type: BackupDestinationType;
  last_run_at?: string | null;
  last_result?: BackupResult | null;
  next_due: boolean;
  destination_status: DestinationStatus;
  s3_status?: S3Status | null;
  pg_dump_available: boolean;
  include_opensearch: boolean;
  opensearch_snapshot_status?: OpenSearchSnapshotStatus | null;
}

export interface S3ConnectionTestResponse {
  ok: boolean;
  error?: string | null;
  bucket?: string | null;
}

export interface BackupFile {
  filename: string;
  size_bytes: number;
  created_at: string;
  encrypted: boolean;
}

export interface BackupListResponse {
  backups: BackupFile[];
  destination_status: DestinationStatus;
  s3_status?: S3Status | null;
}

export interface BackupRunResponse {
  task_id: string;
  status: string;
  message: string;
}

export async function getBackupSettings(): Promise<BackupSettings> {
  const res = await axiosInstance.get<BackupSettings>(BASE);
  return res.data;
}

export async function updateBackupSettings(body: BackupSettingsUpdate): Promise<BackupSettings> {
  const res = await axiosInstance.put<BackupSettings>(BASE, body);
  return res.data;
}

export async function getBackupStatus(): Promise<BackupStatus> {
  const res = await axiosInstance.get<BackupStatus>(`${BASE}/status`);
  return res.data;
}

export async function runBackupNow(): Promise<BackupRunResponse> {
  const res = await axiosInstance.post<BackupRunResponse>(`${BASE}/run`);
  return res.data;
}

export async function listBackups(): Promise<BackupListResponse> {
  const res = await axiosInstance.get<BackupListResponse>(`${BASE}/list`);
  return res.data;
}

export async function testS3Connection(
  body: BackupSettingsUpdate
): Promise<S3ConnectionTestResponse> {
  const res = await axiosInstance.post<S3ConnectionTestResponse>(`${BASE}/test-s3`, body);
  return res.data;
}
