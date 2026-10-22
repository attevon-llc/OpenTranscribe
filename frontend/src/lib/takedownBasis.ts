import type { NewTakedownNotice, NoticeType } from '$lib/api/takedownNotices';

/** How a takedown justifies itself: a recorded notice, a new one, or an explicit "no notice". */
export type BasisMode = 'existing' | 'new' | 'operator';

export interface NoticeDraft {
  noticeType: NoticeType;
  /** `YYYY-MM-DD` from a date input, in the viewer's time zone. */
  receivedDate: string;
  reference: string;
  basis: string;
}

export interface TakedownBasis {
  mode: BasisMode;
  noticeUuid: string;
  draft: NoticeDraft;
  operatorReason: string;
}

export const NOTICE_TYPES: NoticeType[] = ['dmca', 'court_order', 'abuse_report', 'aup', 'other'];
export const BASIS_MAX_LENGTH = 2000;
export const REFERENCE_MAX_LENGTH = 255;

export function emptyDraft(): NoticeDraft {
  return { noticeType: 'dmca', receivedDate: todayLocal(), reference: '', basis: '' };
}

export function emptyBasis(): TakedownBasis {
  return { mode: 'existing', noticeUuid: '', draft: emptyDraft(), operatorReason: '' };
}

/** Today's date in the viewer's time zone, as a date input expects it. */
export function todayLocal(now: Date = new Date()): string {
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${now.getFullYear()}-${month}-${day}`;
}

export function isDraftValid(draft: NoticeDraft, now: Date = new Date()): boolean {
  return (
    draft.reference.trim().length > 0 &&
    draft.basis.trim().length > 0 &&
    /^\d{4}-\d{2}-\d{2}$/.test(draft.receivedDate) &&
    draft.receivedDate <= todayLocal(now)
  );
}

export function isBasisValid(basis: TakedownBasis): boolean {
  if (basis.mode === 'existing') return basis.noticeUuid !== '';
  if (basis.mode === 'new') return isDraftValid(basis.draft);
  return basis.operatorReason.trim().length > 0;
}

/**
 * The request body for a new notice. Local midnight of the chosen day, as an instant:
 * `YYYY-MM-DDT00:00:00Z` would be in the future for a viewer ahead of UTC picking today.
 */
export function toNewNotice(draft: NoticeDraft): NewTakedownNotice {
  return {
    notice_type: draft.noticeType,
    received_at: new Date(`${draft.receivedDate}T00:00:00`).toISOString(),
    requester_reference: draft.reference.trim(),
    basis: draft.basis.trim(),
  };
}
