import type { BulkFileResult } from '$lib/api/bulkActions';
import { getTrashRetentionDays, moveFilesToTrash, restoreFiles } from '$lib/api/trash';
import { getErrorMessage } from '$lib/utils/apiError';
import { toastStore } from '$stores/toast';

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

export interface TrashSelectionHooks {
  t: Translate;
  /** The files just trashed, to animate out of the grid. */
  removeFiles: (uuids: string[]) => void;
  /** Refetch the list after an Undo restored files. */
  refetch: () => void;
  clearSelection: () => void;
}

/**
 * Move `selected` to the trash (#1101) and report the outcome: a success toast with an Undo, and an
 * error toast per refusal class (files the caller may see but not delete, and files a legal hold
 * keeps, issue #1276).
 */
export async function trashSelection(
  selected: Set<string>,
  hooks: TrashSelectionHooks
): Promise<void> {
  const { t } = hooks;
  try {
    const outcome = summarizeTrashResults(await moveFilesToTrash(Array.from(selected)));
    if (outcome.trashed.length > 0) {
      hooks.removeFiles(outcome.trashed);
      const undo = createUndo(outcome.trashed);
      const onUndo = async () => {
        const restored = await undo();
        if (restored === null) return;
        if (restored) toastStore.success(t('gallery.undoDone'));
        else toastStore.error(t('gallery.undoFailed'));
        hooks.refetch();
      };
      toastStore.show(
        t('gallery.trashedSuccess', { count: outcome.trashed.length }),
        'success',
        8000,
        {
          action: { label: t('gallery.undo'), onClick: () => void onUndo() },
        }
      );
    }
    if (outcome.forbidden > 0)
      toastStore.error(t('gallery.deleteForbidden', { count: outcome.forbidden }));
    if (outcome.held > 0) toastStore.error(t('gallery.deleteHeld', { count: outcome.held }));
    if (outcome.other > 0) toastStore.error(t('gallery.deleteFailed', { count: outcome.other }));
    hooks.clearSelection();
  } catch (err: unknown) {
    console.error('Error deleting files:', err);
    toastStore.error(getErrorMessage(err, t('gallery.deleteError')));
  }
}
