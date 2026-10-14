/**
 * Typed client for the admin orphan-cleanup routes (search index + object storage).
 *
 * Mirrors `GET|POST /admin/data-integrity[/counts]` (OpenSearch reconcile, admin.py) and
 * `/admin/data-integrity/storage[/counts|/status]` (admin_storage_integrity.py, issue #1212).
 * Counts are dry runs and delete nothing; the POSTs delete and are audit-logged server-side.
 */

import axiosInstance from '$lib/axios';

const BASE = '/admin/data-integrity';

/** Why a sweep declined to delete: an empty Postgres side, or too large a share to do unasked. */
export type SweepRefusal = 'empty_valid_set' | 'ratio_guard';

export interface IndexSweepStats {
  total_docs: number;
  orphaned_docs: number;
  deleted_docs: number;
  refused: SweepRefusal | null;
  orphan_keys: string[];
  orphan_keys_truncated: boolean;
  rechecked_present: number;
}

export interface SearchOrphanSummary {
  total_orphans_found: number;
  total_deleted: number;
  dry_run: boolean;
  force: boolean;
  refused_indices: Record<string, SweepRefusal>;
}

/** The dry run, with the per-index stats lifted out of the response's mixed-key object. */
export interface SearchOrphanReport {
  indices: Array<IndexSweepStats & { name: string }>;
  summary: SearchOrphanSummary | null;
  /** Set when the search backend could not be reached. */
  unavailable: boolean;
}

export interface StorageOrphanFile {
  file_id: number;
  user_id: number;
  objects: number;
  bytes: number;
}

export interface StorageOrphanReport {
  dry_run: boolean;
  force: boolean;
  refused: SweepRefusal | null;
  error: string | null;
  total_objects: number;
  total_bytes: number;
  orphan_files: number;
  orphan_objects: number;
  orphan_bytes: number;
  orphans: StorageOrphanFile[];
  orphans_truncated: boolean;
  skipped_recent_files: number;
  rechecked_present: number;
  deleted_files: number;
  deleted_objects: number;
  deleted_bytes: number;
  failed_objects: number;
}

export interface StorageSweepStatus {
  running: boolean;
  last_run: {
    timestamp: number;
    duration_seconds: number;
    results: StorageOrphanReport;
  } | null;
}

export interface SweepStarted {
  status: 'started' | 'already_running';
  task_id?: string | null;
  force?: boolean;
}

export async function getSearchOrphanCounts(): Promise<SearchOrphanReport> {
  const { data } = await axiosInstance.get(`${BASE}/counts`);
  const indices: SearchOrphanReport['indices'] = [];
  for (const [name, value] of Object.entries(data ?? {})) {
    if (name === 'summary' || name === 'error' || typeof value !== 'object' || value === null) {
      continue;
    }
    indices.push({ name, ...(value as IndexSweepStats) });
  }
  return {
    indices,
    summary: (data?.summary as SearchOrphanSummary | undefined) ?? null,
    unavailable: typeof data?.error === 'string',
  };
}

export async function getStorageOrphanCounts(): Promise<StorageOrphanReport> {
  const { data } = await axiosInstance.get<StorageOrphanReport>(`${BASE}/storage/counts`);
  return data;
}

export async function getStorageSweepStatus(): Promise<StorageSweepStatus> {
  const { data } = await axiosInstance.get<StorageSweepStatus>(`${BASE}/storage/status`);
  return data;
}

/** Start the search-index sweep. `force` overrides the ratio guard and needs `confirm`. */
export async function removeSearchOrphans(force: boolean): Promise<SweepStarted> {
  const params = force ? { force: true, confirm: true } : undefined;
  const { data } = await axiosInstance.post<SweepStarted>(BASE, undefined, { params });
  return data;
}

/** Start the storage sweep. The server refuses without `confirm=true`, so it is always sent. */
export async function removeStorageOrphans(force: boolean): Promise<SweepStarted> {
  const params = force ? { confirm: true, force: true } : { confirm: true };
  const { data } = await axiosInstance.post<SweepStarted>(`${BASE}/storage`, undefined, {
    params,
  });
  return data;
}
