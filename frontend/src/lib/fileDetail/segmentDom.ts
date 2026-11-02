/**
 * DOM navigation over the rendered transcript on the file-detail page: the playback
 * highlight, jump-to-segment scrolling and the highlight flash.
 *
 * Segments are found by their `data-segment-id` attribute, which `TranscriptSegmentList`
 * renders. Deferred work goes through the page's `schedule` (see `pageTimers`) so the
 * page's `onDestroy` can cancel it.
 */

/** The timing/identity fields of a transcript segment these helpers read. */
export interface TimedSegment {
  uuid?: string;
  id?: number | string;
  start_time: number;
  end_time: number;
}

export type Schedule = (callback: () => void, delayMs: number) => void;

/** The `data-segment-id` value a segment is rendered with. */
export function segmentDomId(segment: TimedSegment): string {
  return String(segment.uuid || segment.id || `${segment.start_time}-${segment.end_time}`);
}

/** The first segment whose [start, end] range (inclusive) contains `time`. */
export function findSegmentAt(segments: TimedSegment[], time: number): TimedSegment | undefined {
  return segments.find((segment) => time >= segment.start_time && time <= segment.end_time);
}

/**
 * Move the `active-segment` class to the segment under the playhead. Never scrolls:
 * the reader may be scrolled elsewhere on purpose.
 */
export function markActiveSegment(segments: unknown, time: number): void {
  if (!Array.isArray(segments)) return;

  document.querySelectorAll('.transcript-segment').forEach((segment) => {
    segment.classList.remove('active-segment');
  });

  const current = findSegmentAt(segments, time);
  if (current) {
    document
      .querySelector(`[data-segment-id="${segmentDomId(current)}"]`)
      ?.classList.add('active-segment');
  }
}

/** Scroll a segment (by uuid) into view and flash it for two seconds. */
export function scrollToAndHighlight(segmentUuid: string, schedule: Schedule): void {
  const el = document.querySelector(`[data-segment-id="${segmentUuid}"]`);
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('highlight-flash');
    schedule(() => el.classList.remove('highlight-flash'), 2000);
  }
}

/**
 * Scroll the segment playing at `time` into view, after the DOM has had time to apply
 * the active-segment class (the `?t=` deep link seeks first, then calls this).
 */
export function scrollToSegmentAtTime(segments: unknown, time: number, schedule: Schedule): void {
  if (!Array.isArray(segments)) return;
  const segment = findSegmentAt(segments, time);
  if (!segment) return;
  const segId = segmentDomId(segment);
  schedule(() => {
    const el = document.querySelector(`[data-segment-id="${segId}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, 300);
}
