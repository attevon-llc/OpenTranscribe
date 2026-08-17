/**
 * Typed API client for the periodic LDAP reconciliation/deprovisioning sweep (issue #484).
 *
 * Mirrors the backend Pydantic schemas in app/api/endpoints/directory_sync_settings.py.
 * All requests go through the shared axiosInstance (cookie auth, CSRF, 401 refresh).
 * Settings are DB-backed (SystemSettings) with coded defaults — no .env vars.
 */

import axiosInstance from '$lib/axios';

const BASE = '/admin/directory-sync';

export interface DirectorySyncResult {
  status?: string | null; // ok | directory_unavailable
  error?: string | null;
  dry_run?: boolean | null;
  candidates?: number | null;
  checked?: number | null;
  disabled?: number | null;
  would_disable?: number | null;
  capped?: boolean | null;
  reconciled?: number | null;
  max_disables_per_run?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface DirectorySyncSettings {
  enabled: boolean;
  schedule: string;
  dry_run: boolean;
  max_disables_per_run: number;
  last_run_at?: string | null;
  last_result?: DirectorySyncResult | null;
}

export interface DirectorySyncSettingsUpdate {
  enabled?: boolean;
  schedule?: string;
  dry_run?: boolean;
  max_disables_per_run?: number;
}

export interface DirectorySyncStatus extends DirectorySyncSettings {
  next_due: boolean;
}

export interface DirectorySyncRunResponse {
  task_id: string;
  status: string;
  message: string;
}

export async function getDirectorySyncSettings(): Promise<DirectorySyncSettings> {
  const res = await axiosInstance.get<DirectorySyncSettings>(BASE);
  return res.data;
}

export async function updateDirectorySyncSettings(
  body: DirectorySyncSettingsUpdate
): Promise<DirectorySyncSettings> {
  const res = await axiosInstance.put<DirectorySyncSettings>(BASE, body);
  return res.data;
}

export async function getDirectorySyncStatus(): Promise<DirectorySyncStatus> {
  const res = await axiosInstance.get<DirectorySyncStatus>(`${BASE}/status`);
  return res.data;
}

export async function runDirectorySyncNow(): Promise<DirectorySyncRunResponse> {
  const res = await axiosInstance.post<DirectorySyncRunResponse>(`${BASE}/run`);
  return res.data;
}
