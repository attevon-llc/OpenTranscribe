/**
 * `scrollbarCalculations.ts` — `findCurrentSegment` locates the transcript segment under the
 * current playback time, used by the "Jump to current" button
 * (`TranscriptDisplay.handleJumpToPlayhead`, issue #748 §5.3).
 *
 * `calculateScrollbarPositionBySegment` and `createThrottledPositionUpdate` were removed
 * along with their tests when `ScrollbarIndicator.svelte` (their only consumer) was deleted
 * in the same issue.
 */
import { describe, it, expect } from 'vitest';
import { findCurrentSegment, type TranscriptSegment } from './scrollbarCalculations';

function segment(start: number, end: number): TranscriptSegment {
  return { start_time: start, end_time: end, text: 'x' };
}

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
