import { getTrashRetentionDays, restoreFiles, type BulkFileResult } from '$lib/api/trash';

/**
 * The gallery's "delete" (issue #1101): the selection moves to the trash, the confirmation says
 * how long it stays recoverable, and the success toast carries an Undo.
 */

type Translate = (key: string, options?: Record<string, unknown>) => string;

/**
 * "Move to trash. Recoverable for N days." with N read from the server's trash window. When
 * the window cannot be read the copy still says the files are recoverable, without a number.
 */
export async function trashConfirmMessage(t: Translate): Promise<string> {
  try {
    const days = await getTrashRetentionDays();
    return t('gallery.trashConfirmMessage', { count: days });
  } catch {
    return t('gallery.trashConfirmMessageNoWindow');
  }
}

export interface TrashOutcome {
  /** Uuids now in the trash. */
  trashed: string[];
  /** The caller may see these files but not delete them. */
  forbidden: number;
  /** A legal or organization hold keeps these files (issue #1276). */
  held: number;
  other: number;
}

export function summarizeTrashResults(results: BulkFileResult[]): TrashOutcome {
  const outcome: TrashOutcome = { trashed: [], forbidden: 0, held: 0, other: 0 };
  for (const r of results) {
    if (r.success) outcome.trashed.push(r.file_uuid);
    else if (r.error === 'FORBIDDEN') outcome.forbidden += 1;
    else if (r.error === 'FILE_UNDER_LEGAL_HOLD') outcome.held += 1;
    else outcome.other += 1;
  }
  return outcome;
}

/**
 * The Undo of one delete: restores those files once (a second click does nothing). Resolves
 * true when every file came back, false when any did not (or the request failed), so the
 * caller can point at the Trash.
 */
export function createUndo(fileUuids: string[]): () => Promise<boolean | null> {
  let used = false;
  return async () => {
    if (used) return null;
    used = true;
    try {
      const results = await restoreFiles(fileUuids);
      return results.length === fileUuids.length && results.every((r) => r.success);
    } catch {
      return false;
    }
  };
}
