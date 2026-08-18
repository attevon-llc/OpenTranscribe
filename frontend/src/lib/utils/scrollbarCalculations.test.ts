/**
 * `scrollbarCalculations.ts` drives the transcript scrollbar playhead indicator.
 * The interpolation math and the segment-lookup tolerance are the parts most
 * likely to silently drift out of sync with the video (wrong position, no error).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  calculateScrollbarPositionBySegment,
  findCurrentSegment,
  createThrottledPositionUpdate,
  type TranscriptSegment,
} from './scrollbarCalculations';

function segment(start: number, end: number): TranscriptSegment {
  return { start_time: start, end_time: end, text: 'x' };
}

describe('calculateScrollbarPositionBySegment', () => {
  it('returns 0 for empty/invalid inputs (no segments, NaN time, negative time)', () => {
    expect(calculateScrollbarPositionBySegment(5, [])).toBe(0);
    expect(calculateScrollbarPositionBySegment(NaN, [segment(0, 10)])).toBe(0);
    expect(calculateScrollbarPositionBySegment(-1, [segment(0, 10)])).toBe(0);
  });

  it('clamps to 0 before the first segment starts and 100 at/after the last segment ends', () => {
    const segments = [segment(10, 20), segment(20, 30)];
    expect(calculateScrollbarPositionBySegment(5, segments)).toBe(0);
    expect(calculateScrollbarPositionBySegment(30, segments)).toBe(100);
    expect(calculateScrollbarPositionBySegment(999, segments)).toBe(100);
  });

  it('interpolates linearly across the full transcript span, sorting out-of-order segments first', () => {
    const segments = [segment(50, 100), segment(0, 50)]; // deliberately out of order

    expect(calculateScrollbarPositionBySegment(0, segments)).toBe(0);
    expect(calculateScrollbarPositionBySegment(50, segments)).toBe(50);
    expect(calculateScrollbarPositionBySegment(100, segments)).toBe(100);
  });

  it('returns 0 when the transcript has zero duration (single instantaneous segment)', () => {
    expect(calculateScrollbarPositionBySegment(5, [segment(5, 5)])).toBe(0);
  });
});

describe('findCurrentSegment', () => {
  const segments = [segment(0, 10), segment(10, 20), segment(20, 30)];

  it('finds the segment containing the current time', () => {
    expect(findCurrentSegment(15, segments)).toBe(segments[1]);
  });

  it('applies a 100ms tolerance at segment boundaries, returning the first array match on overlap', () => {
    expect(findCurrentSegment(9.95, segments)).toBe(segments[0]);
    // 20.05 falls within tolerance of BOTH segment[1]'s end (20+0.1) and
    // segment[2]'s start (20-0.1) — the linear scan returns the first match.
    expect(findCurrentSegment(20.05, segments)).toBe(segments[1]);
  });

  it('returns null when no segment contains the time, or the input is invalid', () => {
    expect(findCurrentSegment(100, segments)).toBeNull();
    expect(findCurrentSegment(NaN, segments)).toBeNull();
    expect(findCurrentSegment(5, [])).toBeNull();
  });
});

describe('createThrottledPositionUpdate', () => {
  let rafCallbacks: FrameRequestCallback[];

  beforeEach(() => {
    vi.useFakeTimers();
    rafCallbacks = [];
    vi.stubGlobal(
      'requestAnimationFrame',
      vi.fn((cb: FrameRequestCallback) => {
        rafCallbacks.push(cb);
        return rafCallbacks.length;
      })
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('invokes immediately on the first call', () => {
    const callback = vi.fn();
    const throttled = createThrottledPositionUpdate(callback, 16);

    throttled(50);

    expect(callback).toHaveBeenCalledWith(50);
  });

  it('collapses rapid calls within the delay window into a single rAF frame, keeping the FIRST deferred value', () => {
    const callback = vi.fn();
    const throttled = createThrottledPositionUpdate(callback, 16);

    throttled(10); // fires immediately
    callback.mockClear();

    throttled(20); // inside the window — deferred to rAF, closure captures 20
    throttled(30); // a second call while one is already scheduled: no new rAF, and
    // this position is silently dropped rather than replacing the pending one —
    // worth pinning explicitly since "coalesce to latest" would be the more
    // intuitive behavior and is NOT what this implementation does.
    expect(callback).not.toHaveBeenCalled();
    expect(rafCallbacks).toHaveLength(1); // only ONE frame scheduled, not two

    rafCallbacks[0](0);
    expect(callback).toHaveBeenCalledWith(20);
  });

  it('calls directly again once the delay window has passed', () => {
    const callback = vi.fn();
    const throttled = createThrottledPositionUpdate(callback, 16);

    throttled(10);
    vi.advanceTimersByTime(17);
    throttled(20);

    expect(callback).toHaveBeenNthCalledWith(1, 10);
    expect(callback).toHaveBeenNthCalledWith(2, 20);
    expect(rafCallbacks).toHaveLength(0); // never had to defer
  });
});
