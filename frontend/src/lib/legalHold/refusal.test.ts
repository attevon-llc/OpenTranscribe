/**
 * A 409 FILE_UNDER_LEGAL_HOLD that arrives after the page loaded: the translated refusal is
 * shown once, and the page hears who holds the file so it can switch to the frozen view.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/galleryToolbarMocks')).englishLocale()
);
const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import { isHoldRefusal, onHoldRefused, reportHoldRefusal } from './refusal';

const refusal = (heldBy: string[]) => ({
  response: {
    status: 409,
    data: { detail: { error: 'FILE_UNDER_LEGAL_HOLD', message: 'refused', held_by: heldBy } },
  },
});

beforeEach(() => vi.clearAllMocks());

describe('isHoldRefusal', () => {
  it('is true only for a 409 whose detail names the legal-hold code', () => {
    expect(isHoldRefusal(refusal(['platform']))).toBe(true);
    expect(isHoldRefusal({ response: { status: 409, data: { detail: 'conflict' } } })).toBe(false);
    expect(
      isHoldRefusal({
        response: { status: 400, data: { detail: { error: 'FILE_UNDER_LEGAL_HOLD' } } },
      })
    ).toBe(false);
    expect(isHoldRefusal(new Error('network'))).toBe(false);
  });
});

describe('reportHoldRefusal', () => {
  it('toasts the translated sentence for who holds it, and tells subscribers', () => {
    const heard: string[][] = [];
    const stop = onHoldRefused((heldBy) => heard.push(heldBy));

    expect(reportHoldRefusal(refusal(['organization']))).toBe(true);

    expect(toast.error).toHaveBeenCalledWith(
      expect.stringContaining('placed by an administrator of your organization')
    );
    expect(heard).toEqual([['organization']]);
    stop();
  });

  it('ignores any other error and leaves the toast to the caller', () => {
    const heard: string[][] = [];
    const stop = onHoldRefused((heldBy) => heard.push(heldBy));
    expect(reportHoldRefusal({ response: { status: 500, data: { detail: 'boom' } } })).toBe(false);
    expect(toast.error).not.toHaveBeenCalled();
    expect(heard).toEqual([]);
    stop();
  });

  it('does not replay a refusal seen before the page subscribed', () => {
    reportHoldRefusal(refusal(['platform']));
    const heard: string[][] = [];
    const stop = onHoldRefused((heldBy) => heard.push(heldBy));
    expect(heard).toEqual([]);
    reportHoldRefusal(refusal(['organization', 'platform', 'bogus']));
    expect(heard).toEqual([['organization', 'platform']]);
    stop();
  });
});
