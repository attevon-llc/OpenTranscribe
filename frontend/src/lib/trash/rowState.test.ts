/**
 * What a trash row says about its deletion (issue #1101): a held file is kept whatever its
 * date, a file past its date is being deleted and can no longer be restored, and otherwise the
 * whole days left are rounded up so "less than a day" never reads as 0.
 */
import { describe, expect, it } from 'vitest';
import type { TrashedFile } from '$lib/api/trash';
import { canOfferRestore, showsOtherOwners, trashRowState } from './rowState';

const NOW = Date.parse('2026-11-07T12:00:00Z');
const HOUR = 60 * 60 * 1000;
const at = (ms: number) => new Date(NOW + ms).toISOString();

function row(overrides: Partial<TrashedFile> = {}): TrashedFile {
  return {
    uuid: 'f1',
    filename: 'a.wav',
    title: null,
    owner_uuid: 'me',
    owner_name: 'Me',
    deleted_at: at(-HOUR),
    purge_after: at(10 * 24 * HOUR),
    trash_origin: 'user',
    deleted_by_uuid: 'me',
    deleted_by_name: 'Me',
    held: false,
    can_restore: true,
    can_purge: true,
    ...overrides,
  };
}

describe('trashRowState', () => {
  it('counts whole days left, rounding a part day up', () => {
    expect(trashRowState(row({ purge_after: at(10 * 24 * HOUR) }), NOW)).toEqual({
      kind: 'remaining',
      days: 10,
    });
    expect(trashRowState(row({ purge_after: at(9 * 24 * HOUR + 1) }), NOW)).toEqual({
      kind: 'remaining',
      days: 10,
    });
    expect(trashRowState(row({ purge_after: at(HOUR) }), NOW)).toEqual({
      kind: 'remaining',
      days: 1,
    });
  });

  it('is due at and after the purge date', () => {
    expect(trashRowState(row({ purge_after: at(0) }), NOW)).toEqual({ kind: 'due' });
    expect(trashRowState(row({ purge_after: at(-HOUR) }), NOW)).toEqual({ kind: 'due' });
  });

  it('a held file is kept, even past its date', () => {
    expect(trashRowState(row({ held: true, purge_after: at(-HOUR) }), NOW)).toEqual({
      kind: 'held',
    });
  });
});

describe('canOfferRestore', () => {
  it('follows the server flag', () => {
    expect(canOfferRestore(row(), NOW)).toBe(true);
    expect(canOfferRestore(row({ can_restore: false }), NOW)).toBe(false);
  });

  it('is withheld once the file is being deleted, but not for a held file', () => {
    expect(canOfferRestore(row({ purge_after: at(-HOUR) }), NOW)).toBe(false);
    expect(canOfferRestore(row({ held: true, purge_after: at(-HOUR) }), NOW)).toBe(true);
  });
});

describe('showsOtherOwners', () => {
  it('is false for a trash that is all mine, true once someone else owns a row', () => {
    expect(showsOtherOwners([row()], 'me')).toBe(false);
    expect(showsOtherOwners([row(), row({ uuid: 'f2', owner_uuid: 'them' })], 'me')).toBe(true);
  });
});
