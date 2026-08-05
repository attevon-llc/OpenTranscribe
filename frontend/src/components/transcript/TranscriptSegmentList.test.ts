/**
 * Reading progress used to be recomputed from an unthrottled `scroll` handler
 * that ran `querySelectorAll` + `offsetTop` over the whole list on every event.
 * It is now derived from an IntersectionObserver. These tests pin the resulting
 * contract: no scroll listener, every segment row observed, and progress driven
 * by the topmost visible segment index.
 *
 * jsdom has no IntersectionObserver, so one is stubbed and driven by hand.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import TranscriptSegmentList from './TranscriptSegmentList.svelte';

type IOCallback = (entries: IntersectionObserverEntry[]) => void;

let observers: { callback: IOCallback; targets: Element[] }[] = [];

class StubIntersectionObserver {
  private record: { callback: IOCallback; targets: Element[] };

  constructor(callback: IOCallback) {
    this.record = { callback, targets: [] };
    observers.push(this.record);
  }

  observe(target: Element) {
    this.record.targets.push(target);
  }

  disconnect() {
    this.record.targets = [];
  }

  unobserve() {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

/** Fire an intersection callback for the observer that watches `[data-seg-index]`. */
function fireVisibility(entries: { target: Element; isIntersecting: boolean }[]) {
  const observer = observers.find((o) => o.targets.length > 0);
  observer?.callback(entries as unknown as IntersectionObserverEntry[]);
}

function segment(uuid: string, index: number) {
  return {
    uuid,
    start_time: index * 5,
    end_time: index * 5 + 5,
    text: `Segment ${index}`,
    speaker_label: 'SPEAKER_00',
  };
}

function group(uuid: string, index: number) {
  return {
    isOverlapGroup: false,
    startSegmentIndex: index,
    startTime: index * 5,
    endTime: index * 5 + 5,
    segments: [segment(uuid, index)],
  };
}

const groups = [group('a', 0), group('b', 1), group('c', 2), group('d', 3)];

const props = {
  file: { uuid: 'file-1', transcript_segments: groups.map((g) => g.segments[0]) },
  groupedTranscriptSegments: groups,
  transcriptSegments: groups.map((g) => g.segments[0]),
  // Keeps SegmentSpeakerDropdown (which talks to the API) out of the tree.
  diarizationDisabled: true,
  totalSegments: 100,
};

describe('TranscriptSegmentList', () => {
  beforeEach(() => {
    observers = [];
    vi.stubGlobal('IntersectionObserver', StubIntersectionObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders one row per segment under a keyed each block', () => {
    render(TranscriptSegmentList, { props });
    // A duplicate/missing key would make Svelte throw during render.
    expect(screen.getByText('Segment 0')).toBeInTheDocument();
    expect(screen.getByText('Segment 3')).toBeInTheDocument();
    expect(document.querySelectorAll('[data-segment-id]')).toHaveLength(4);
  });

  it('attaches no scroll listener to the scroll container', () => {
    const spy = vi.spyOn(Element.prototype, 'addEventListener');
    render(TranscriptSegmentList, { props });
    const scrollListeners = spy.mock.calls.filter(([type]) => type === 'scroll');
    expect(scrollListeners).toHaveLength(0);
    spy.mockRestore();
  });

  it('observes every segment row for reading progress', async () => {
    render(TranscriptSegmentList, { props });
    await tick();
    await tick();

    const observed = observers.flatMap((o) => o.targets);
    expect(observed).toHaveLength(4);
    expect(observed.every((el) => el.hasAttribute('data-seg-index'))).toBe(true);
  });

  it('derives progress from the topmost visible segment index', async () => {
    const { container } = render(TranscriptSegmentList, { props });
    await tick();
    await tick();

    const rows = Array.from(container.querySelectorAll('[data-seg-index]'));
    fireVisibility([
      { target: rows[2], isIntersecting: true },
      { target: rows[3], isIntersecting: true },
    ]);
    await tick();

    // Topmost visible is index 2 of 100 total segments.
    const fill = container.querySelector('.reading-progress-fill') as HTMLElement;
    expect(fill.style.width).toBe('2%');

    // Scrolling row 2 out leaves row 3 on top.
    fireVisibility([{ target: rows[2], isIntersecting: false }]);
    await tick();
    expect(fill.style.width).toBe('3%');
  });
});
