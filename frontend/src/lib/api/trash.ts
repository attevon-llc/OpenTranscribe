import axiosInstance from '$lib/axios';
import { postBulkAction, type BulkFileResult } from '$lib/api/bulkActions';

/**
 * The trash (issue #1101). Mirrors `backend/app/schemas/trash.py`.
 *
 * Deleting a file moves it here (`POST /files/management/bulk-action` with `action: 'delete'`);
 * it can be restored until `purge_after`, then a scheduled job deletes it forever. Which rows a
 * caller sees and which buttons apply are the server's answer (`can_restore`, `can_purge`):
 * the page only hides a button the API would refuse. URLs stay literal because
 * `backend/tests/unit/test_route_has_a_caller.py` finds a route's caller by its URL string.
 */

export interface TrashedFile {
  uuid: string;
  filename: string;
  title: string | null;
  owner_uuid: string | null;
  owner_name: string | null;
  deleted_at: string;
  purge_after: string;
  trash_origin: string;
  deleted_by_uuid: string | null;
  deleted_by_name: string | null;
  /** Under a legal or organization hold: restorable, never deleted forever while held. */
  held: boolean;
  can_restore: boolean;
  can_purge: boolean;
}

export interface TrashPage {
  items: TrashedFile[];
  total: number;
  page: number;
  page_size: number;
  /** The current trash window in days, for files moved to the trash from now on. */
  retention_days: number;
}

export interface TrashPurgeQueued {
  queued: number;
}

/** One file's outcome in a bulk action; a refusal is that file's result, never the batch's. */
export const TRASH_PAGE_SIZE = 50;

export async function listTrash(page = 1, pageSize = TRASH_PAGE_SIZE): Promise<TrashPage> {
  const res = await axiosInstance.get<TrashPage>('/files/trash', {
    params: { page, page_size: pageSize },
  });
  return res.data;
}

/** The trash window in days, for "recoverable for N days" copy before a delete. */
export async function getTrashRetentionDays(): Promise<number> {
  return (await listTrash(1, 1)).retention_days;
}

export async function restoreFile(fileUuid: string): Promise<void> {
  await axiosInstance.post(`/files/${fileUuid}/restore`);
}

export async function purgeFile(fileUuid: string): Promise<TrashPurgeQueued> {
  const res = await axiosInstance.delete<TrashPurgeQueued>(`/files/${fileUuid}/purge`);
  return res.data;
}

export async function emptyTrash(): Promise<TrashPurgeQueued> {
  const res = await axiosInstance.post<TrashPurgeQueued>('/files/trash/empty');
  return res.data;
}

async function bulkAction(
  fileUuids: string[],
  action: 'delete' | 'restore'
): Promise<BulkFileResult[]> {
  return postBulkAction<BulkFileResult>({ file_uuids: fileUuids, action });
}

/** Move files to the trash. Held or forbidden files come back as per-file refusals. */
export function moveFilesToTrash(fileUuids: string[]): Promise<BulkFileResult[]> {
  return bulkAction(fileUuids, 'delete');
}

/** Bring files back from the trash (the delete toast's Undo). */
export function restoreFiles(fileUuids: string[]): Promise<BulkFileResult[]> {
  return bulkAction(fileUuids, 'restore');
}
