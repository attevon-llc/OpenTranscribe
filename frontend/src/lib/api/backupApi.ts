/**
 * Typed API client for scheduled database backups (admin-only, Feature C).
 *
 * Mirrors the backend Pydantic schemas in app/api/endpoints/backup_settings.py.
 * All requests go through the shared axiosInstance (cookie auth, CSRF, 401 refresh).
 * Settings are DB-backed (SystemSettings) with coded defaults — no .env vars.
 */

import axiosInstance from '$lib/axios';

const BASE = '/admin/backup';

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
}

export interface DestinationStatus {
  destination: string;
  exists: boolean;
  writable: boolean;
  mounted: boolean;
}

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
  last_run_at?: string | null;
  last_result?: BackupResult | null;
  destination_status: DestinationStatus;
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
}

export interface BackupStatus {
  enabled: boolean;
  schedule: string;
  last_run_at?: string | null;
  last_result?: BackupResult | null;
  next_due: boolean;
  destination_status: DestinationStatus;
  pg_dump_available: boolean;
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
