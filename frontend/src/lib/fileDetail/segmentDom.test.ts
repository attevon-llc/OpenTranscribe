import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  findSegmentAt,
  markActiveSegment,
  scrollToAndHighlight,
  scrollToSegmentAtTime,
  segmentDomId,
} from './segmentDom';

const SEGMENTS = [
  { uuid: 'a', start_time: 0, end_time: 2 },
  { uuid: 'b', start_time: 2, end_time: 5 },
  { uuid: 'c', start_time: 6, end_time: 8 },
];

function renderSegments(ids: string[]): void {
  document.body.innerHTML = ids
    .map((id) => `<div class="transcript-segment" data-segment-id="${id}"></div>`)
    .join('');
}

function el(id: string): Element {
  const found = document.querySelector(`[data-segment-id="${id}"]`);
  if (!found) throw new Error(`no segment ${id}`);
  return found;
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  document.body.innerHTML = '';
});

describe('segmentDomId', () => {
  it('prefers uuid, then id, then the time range', () => {
    expect(segmentDomId({ uuid: 'u', id: 7, start_time: 1, end_time: 2 })).toBe('u');
    expect(segmentDomId({ id: 7, start_time: 1, end_time: 2 })).toBe('7');
    expect(segmentDomId({ start_time: 1, end_time: 2 })).toBe('1-2');
  });
});

describe('findSegmentAt', () => {
  it('is inclusive at both ends and takes the first match on a shared boundary', () => {
    expect(findSegmentAt(SEGMENTS, 0)?.uuid).toBe('a');
    expect(findSegmentAt(SEGMENTS, 2)?.uuid).toBe('a');
    expect(findSegmentAt(SEGMENTS, 5)?.uuid).toBe('b');
    expect(findSegmentAt(SEGMENTS, 5.5)).toBeUndefined();
  });
});

describe('markActiveSegment', () => {
  it('moves the active class to the segment under the playhead only', () => {
    renderSegments(['a', 'b', 'c']);
    el('a').classList.add('active-segment');

    markActiveSegment(SEGMENTS, 3);

    expect(el('a').classList.contains('active-segment')).toBe(false);
    expect(el('b').classList.contains('active-segment')).toBe(true);
    expect(el('c').classList.contains('active-segment')).toBe(false);
  });

  it('clears the highlight in a gap between segments, and never scrolls', () => {
    renderSegments(['a', 'b', 'c']);
    el('b').classList.add('active-segment');

    markActiveSegment(SEGMENTS, 5.5);

    expect(document.querySelectorAll('.active-segment')).toHaveLength(0);
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();
  });

  it('leaves the DOM alone when there is no segment array', () => {
    renderSegments(['a']);
    el('a').classList.add('active-segment');

    markActiveSegment(undefined, 1);

    expect(el('a').classList.contains('active-segment')).toBe(true);
  });
});

describe('scrollToAndHighlight', () => {
  it('scrolls, flashes, and removes the flash through the scheduled callback', () => {
    renderSegments(['a', 'b']);
    const scheduled: Array<[() => void, number]> = [];

    scrollToAndHighlight('b', (cb, ms) => scheduled.push([cb, ms]));

    expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({
      behavior: 'smooth',
      block: 'center',
    });
    expect(el('b').classList.contains('highlight-flash')).toBe(true);
    expect(scheduled).toHaveLength(1);
    expect(scheduled[0][1]).toBe(2000);
    scheduled[0][0]();
    expect(el('b').classList.contains('highlight-flash')).toBe(false);
  });

  it('schedules nothing for a segment that is not rendered', () => {
    renderSegments(['a']);
    const schedule = vi.fn();
    scrollToAndHighlight('missing', schedule);
    expect(schedule).not.toHaveBeenCalled();
  });
});

describe('scrollToSegmentAtTime', () => {
  it('scrolls the segment at that time into view after 300ms', () => {
    renderSegments(['a', 'b', 'c']);
    const scheduled: Array<[() => void, number]> = [];

    scrollToSegmentAtTime(SEGMENTS, 7, (cb, ms) => scheduled.push([cb, ms]));

    expect(scheduled.map(([, ms]) => ms)).toEqual([300]);
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();
    scheduled[0][0]();
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
    expect(vi.mocked(Element.prototype.scrollIntoView).mock.contexts[0]).toBe(el('c'));
  });

  it('schedules nothing when no segment covers that time', () => {
    const schedule = vi.fn();
    scrollToSegmentAtTime(SEGMENTS, 99, schedule);
    scrollToSegmentAtTime(null, 1, schedule);
    expect(schedule).not.toHaveBeenCalled();
  });
});
