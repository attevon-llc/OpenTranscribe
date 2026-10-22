import { describe, it, expect } from 'vitest';
import {
  emptyBasis,
  emptyDraft,
  isBasisValid,
  isDraftValid,
  todayLocal,
  toNewNotice,
} from './takedownBasis';

describe('takedown basis', () => {
  it('starts with no usable basis: the choice is required, not defaulted', () => {
    expect(isBasisValid(emptyBasis())).toBe(false);
  });

  it('an existing notice needs a selection', () => {
    expect(isBasisValid({ ...emptyBasis(), mode: 'existing', noticeUuid: '' })).toBe(false);
    expect(isBasisValid({ ...emptyBasis(), mode: 'existing', noticeUuid: 'n-1' })).toBe(true);
  });

  it('an operator action needs a non-blank stated reason', () => {
    const base = { ...emptyBasis(), mode: 'operator' as const };
    expect(isBasisValid({ ...base, operatorReason: '   ' })).toBe(false);
    expect(isBasisValid({ ...base, operatorReason: 'routine review' })).toBe(true);
  });

  it('a new notice needs a reference, a basis and a date that is not in the future', () => {
    const ok = { ...emptyDraft(), reference: 'CASE-1', basis: 'complete notice' };
    expect(isDraftValid(ok)).toBe(true);
    expect(isDraftValid({ ...ok, reference: ' ' })).toBe(false);
    expect(isDraftValid({ ...ok, basis: '' })).toBe(false);
    expect(isDraftValid({ ...ok, receivedDate: '' })).toBe(false);
    expect(isDraftValid({ ...ok, receivedDate: '2999-01-01' })).toBe(false);
  });

  it('todayLocal is the viewer-local calendar date, zero padded', () => {
    expect(todayLocal(new Date(2026, 0, 5, 23, 59))).toBe('2026-01-05');
  });

  it('sends local midnight of the chosen day, which is never later than now', () => {
    const body = toNewNotice({
      noticeType: 'court_order',
      receivedDate: todayLocal(),
      reference: '  CASE-7 ',
      basis: ' b ',
    });
    expect(body).toMatchObject({
      notice_type: 'court_order',
      requester_reference: 'CASE-7',
      basis: 'b',
    });
    expect(new Date(body.received_at).getTime()).toBeLessThanOrEqual(Date.now());
    expect(body.received_at).toMatch(/Z$/);
  });
});
