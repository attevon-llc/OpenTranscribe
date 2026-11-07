import type { TrashedFile } from '$lib/api/trash';

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * What a trash row says about its deletion (issue #1101).
 *
 * - `held`: a hold freezes the file in the trash; it is never deleted forever while held, and
 *   it stays restorable even past its date.
 * - `due`: its time has ended and the scheduled job is deleting it; the server refuses a
 *   restore (`NOT_RESTORABLE`), so the page does not offer one.
 * - `remaining`: whole days left, rounded up, never below 1 while the date is in the future.
 */
export type TrashRowState =
  | { kind: 'held' }
  | { kind: 'due' }
  | { kind: 'remaining'; days: number };

export function trashRowState(
  file: Pick<TrashedFile, 'held' | 'purge_after'>,
  now: number
): TrashRowState {
  if (file.held) return { kind: 'held' };
  const left = new Date(file.purge_after).getTime() - now;
  if (!Number.isFinite(left) || left <= 0) return { kind: 'due' };
  return { kind: 'remaining', days: Math.max(1, Math.ceil(left / DAY_MS)) };
}

/** Restore is offered when the server allows it and the file is not already being deleted. */
export function canOfferRestore(file: TrashedFile, now: number): boolean {
  return file.can_restore && trashRowState(file, now).kind !== 'due';
}

/**
 * Show the owner column only when the list holds someone else's file: an organization
 * administrator's org trash, or a single-tenant administrator's whole trash.
 */
export function showsOtherOwners(items: TrashedFile[], currentUserUuid: string | null): boolean {
  return items.some((f) => f.owner_uuid !== null && f.owner_uuid !== currentUserUuid);
}
