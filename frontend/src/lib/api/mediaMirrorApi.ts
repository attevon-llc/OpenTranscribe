/**
 * Typed API client for the incremental media mirror (admin-only, issue #242).
 *
 * Mirrors the backend Pydantic schemas in app/api/endpoints/media_mirror_settings.py.
 * All requests go through the shared axiosInstance (cookie auth, CSRF, 401 refresh).
 * Settings are DB-backed (backup.mirror_* SystemSettings) with coded defaults — the
 * only env var is the BACKUP_MIRROR_HOST_PATH mount for the folder destination.
 */

import axiosInstance from '$lib/axios';

const BASE = '/admin/backup/mirror';

export type MirrorDestinationType = 'local' | 's3';

export interface MirrorResult {
  ok: boolean;
  status: string; // success | no_destination | error
  error?: string | null;
  objects_scanned?: number | null;
  objects_excluded?: number | null;
  objects_copied?: number | null;
  objects_skipped?: number | null;
  objects_failed?: number | null;
  bytes_copied?: number | null;
  duration_s?: number | null;
  started_at?: string | null;
  // Bounded sample of per-object error messages (counts stay exact).
  errors?: string[] | null;
}

export interface MirrorDestinationStatus {
  destination: string;
  exists: boolean;
  writable: boolean;
  mounted: boolean;
}

export interface MirrorS3Status {
  bucket: string;
  prefix: string;
  endpoint_url: string;
  reachable: boolean;
  error?: string | null;
}

export interface MediaMirrorSettings {
  enabled: boolean;
  schedule: string;
  destination_type: MirrorDestinationType;
  destination: string;
  throttle_ms: number;
  s3_endpoint_url: string;
  s3_region: string;
  s3_bucket: string;
  s3_prefix: string;
  s3_access_key_id: string;
  s3_secret_key_set: boolean;
  last_run_at?: string | null;
  last_result?: MirrorResult | null;
  destination_status: MirrorDestinationStatus;
  s3_status?: MirrorS3Status | null;
  running: boolean;
}

export interface MediaMirrorSettingsUpdate {
  enabled?: boolean;
  schedule?: string;
  destination_type?: MirrorDestinationType;
  destination?: string;
  throttle_ms?: number;
  s3_endpoint_url?: string;
  s3_region?: string;
  s3_bucket?: string;
  s3_prefix?: string;
  s3_access_key_id?: string;
  // Write-only: sent on save, never returned by the API.
  s3_secret_key?: string;
}

export interface MirrorRunResponse {
  task_id: string;
  status: string;
  message: string;
}

export interface MirrorS3TestResponse {
  ok: boolean;
  error?: string | null;
  bucket?: string | null;
}

export async function getMediaMirrorSettings(): Promise<MediaMirrorSettings> {
  const res = await axiosInstance.get<MediaMirrorSettings>(BASE);
  return res.data;
}

export async function updateMediaMirrorSettings(
  body: MediaMirrorSettingsUpdate
): Promise<MediaMirrorSettings> {
  const res = await axiosInstance.put<MediaMirrorSettings>(BASE, body);
  return res.data;
}

export async function runMediaMirrorNow(): Promise<MirrorRunResponse> {
  const res = await axiosInstance.post<MirrorRunResponse>(`${BASE}/run`);
  return res.data;
}

export async function testMirrorS3Connection(
  body: MediaMirrorSettingsUpdate
): Promise<MirrorS3TestResponse> {
  const res = await axiosInstance.post<MirrorS3TestResponse>(`${BASE}/test-s3`, body);
  return res.data;
}
