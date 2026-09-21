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

  describe('speaker-match highlighting (issue #837)', () => {
    // Edit mode renders the speaker label as a plain div (no SegmentSpeakerDropdown,
    // so no axios/portal/locale mocking needed) — the same speakerMatchState() helper
    // feeds both, so this exercises the wiring without the dropdown's own dependencies.
    const editProps = {
      ...props,
      diarizationDisabled: false,
      editingSegmentId: 'b',
    };

    it('adds no highlight class when there is no speaker match for the segment', () => {
      const { container } = render(TranscriptSegmentList, {
        props: { ...editProps, searchMatches: [], currentMatchIndex: -1 },
      });
      const chip = container.querySelector('[data-segment-id="b"] .segment-speaker') as HTMLElement;
      expect(chip).toBeTruthy();
      expect(chip.classList.contains('search-match')).toBe(false);
      expect(chip.classList.contains('current-match')).toBe(false);
    });

    it('adds .search-match when a type: speaker match targets this segment', () => {
      const { container } = render(TranscriptSegmentList, {
        props: {
          ...editProps,
          searchMatches: [{ segmentIndex: 1, start: 0, length: 4, type: 'speaker' }],
          currentMatchIndex: -1,
        },
      });
      const chip = container.querySelector('[data-segment-id="b"] .segment-speaker') as HTMLElement;
      expect(chip.classList.contains('search-match')).toBe(true);
      expect(chip.classList.contains('current-match')).toBe(false);
    });

    it('adds .current-match instead of .search-match when it is the active match', () => {
      const { container } = render(TranscriptSegmentList, {
        props: {
          ...editProps,
          searchMatches: [{ segmentIndex: 1, start: 0, length: 4, type: 'speaker' }],
          currentMatchIndex: 0,
        },
      });
      const chip = container.querySelector('[data-segment-id="b"] .segment-speaker') as HTMLElement;
      expect(chip.classList.contains('current-match')).toBe(true);
      expect(chip.classList.contains('search-match')).toBe(false);
    });

    it('does not highlight the speaker label for a type: text match on the same segment', () => {
      const { container } = render(TranscriptSegmentList, {
        props: {
          ...editProps,
          searchMatches: [{ segmentIndex: 1, start: 0, length: 4, type: 'text' }],
          currentMatchIndex: 0,
        },
      });
      const chip = container.querySelector('[data-segment-id="b"] .segment-speaker') as HTMLElement;
      expect(chip.classList.contains('search-match')).toBe(false);
      expect(chip.classList.contains('current-match')).toBe(false);
    });
  });

  describe('query text highlighting (regression guard)', () => {
    // Pins the contract that broke: the index a segment resolves to via
    // getOriginalSegmentIndex() (uuid lookup against file.transcript_segments) must equal
    // the segmentIndex highlightTextWithMatches() is asked to filter on. TranscriptSearch's
    // computeMatches() assigns segmentIndex by iterating file.transcript_segments in order,
    // so `searchMatches` here mirrors that: segment 'b' is transcript_segments[1].
    const searchProps = {
      ...props,
      searchQuery: 'segment',
      searchMatches: [
        { segmentIndex: 0, start: 0, length: 7, type: 'text' as const },
        { segmentIndex: 1, start: 0, length: 7, type: 'text' as const },
        { segmentIndex: 2, start: 0, length: 7, type: 'text' as const },
        { segmentIndex: 3, start: 0, length: 7, type: 'text' as const },
      ],
      currentMatchIndex: 0,
    };

    it('renders a highlight span for every matched segment', () => {
      const { container } = render(TranscriptSegmentList, { props: searchProps });
      const highlights = container.querySelectorAll('.transcript-search-highlight');
      expect(highlights).toHaveLength(4);
    });

    it('marks the highlight for the current match with the "current" class', () => {
      const { container } = render(TranscriptSegmentList, { props: searchProps });
      const segmentA = container.querySelector('[data-segment-id="a"] .segment-text');
      const highlight = segmentA?.querySelector('.transcript-search-highlight');
      expect(highlight?.classList.contains('current')).toBe(true);
    });

    it('does not highlight when the group order differs from transcript_segments order', () => {
      // Groups can be reordered relative to the flat transcript_segments array (e.g. by
      // backend overlap-group resolution); the group's segment objects are still the SAME
      // refs as transcript_segments, so uuid-based lookup must find the right index
      // regardless of rendering order.
      const reordered = {
        ...searchProps,
        groupedTranscriptSegments: [groups[3], groups[1], groups[0], groups[2]],
      };
      const { container } = render(TranscriptSegmentList, { props: reordered });
      const highlights = container.querySelectorAll('.transcript-search-highlight');
      expect(highlights).toHaveLength(4);
      const segmentB = container.querySelector('[data-segment-id="b"] .segment-text');
      expect(segmentB?.querySelector('.transcript-search-highlight')).toBeTruthy();
    });

    // NOTE on what this describe block does and does NOT guard: the regression that shipped
    // (segmentHighlight() reading searchQuery/searchMatches/currentMatchIndex/
    // segmentClassification from closure instead of taking them as explicit parameters) is a
    // "stale after a LATER prop update" bug, not a "wrong value at mount" bug — every test
    // above renders with the search props already populated at mount, so it can't tell the
    // fixed code from the broken code. A `rerender()` through @testing-library/svelte's own
    // prop-passing helper (`@testing-library/svelte-core/props.svelte.js`) can't tell them
    // apart either: it stores ALL props as one `$state.raw` bag and replaces the whole bag on
    // every `rerender`, so any single prop change invalidates that one shared signal and forces
    // a full top-to-bottom re-render regardless of which individual template expression
    // statically depends on what — the exact fine-grained gap this bug lives in never gets
    // exercised. The real regression guard needs a real PARENT Svelte component passing
    // updated props down the way `TranscriptDisplay` does in production (each exported prop is
    // its own compiled reactive binding, not one bundled bag) — see
    // `TranscriptDisplay.test.ts`'s "search highlighting after the find bar populates matches
    // (regression, issue #755)" block, which fails against the pre-fix code and passes here.
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
