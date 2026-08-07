/**
 * End-to-end proof for issue #352.
 *
 * The transcript renders from `file.grouped_segments`, which used to embed its own copies
 * of every segment. Optimistic updates patched `file.transcript_segments` — a different
 * set of objects — so a rename saved to the database and then rendered nothing until a
 * full page reload. Groups now carry only uuid references, so there is one segment object
 * and a patch cannot miss.
 *
 * These tests fail against the old dual-copy payload, which is the point: a fixture that
 * shares object references between the two arrays would pass either way.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import TranscriptDisplay from './TranscriptDisplay.svelte';
import { renameSpeakersInFile, appendSegmentPage } from '$lib/fileDetail/segmentSync';

class StubIntersectionObserver {
  observe() {}
  disconnect() {}
  unobserve() {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

function segment(uuid: string, index: number, speakerName: string) {
  return {
    uuid,
    start_time: index * 5,
    end_time: index * 5 + 5,
    text: `Segment ${index}`,
    speaker_id: 'spk-1',
    speaker_label: 'SPEAKER_01',
    resolved_speaker_name: speakerName,
    speaker: { uuid: 'spk-1', name: 'SPEAKER_01', display_name: speakerName },
  };
}

function group(uuid: string, index: number) {
  return {
    is_overlap_group: false,
    overlap_group_id: null as string | null,
    start_time: index * 5,
    end_time: index * 5 + 5,
    start_segment_index: index,
    segment_uuids: [uuid],
  };
}

// A plain display name, not a raw `SPEAKER_01`, so `translateSpeakerLabel` passes it
// through unchanged and the assertions read the value under test.
const ORIGINAL_NAME = 'Guest Three';

function makeFile() {
  return {
    uuid: 'file-1',
    status: 'completed',
    transcript_segments: [segment('a', 0, ORIGINAL_NAME), segment('b', 1, ORIGINAL_NAME)],
    grouped_segments: [group('a', 0), group('b', 1)],
  };
}

const baseProps = { totalSegments: 2, speakerList: [] };

describe('TranscriptDisplay', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', StubIntersectionObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders segments resolved from the grouping', () => {
    render(TranscriptDisplay, { props: { ...baseProps, file: makeFile() } });

    expect(screen.getByText('Segment 0')).toBeInTheDocument();
    expect(screen.getByText('Segment 1')).toBeInTheDocument();
  });

  it('repaints a speaker rename with no refetch (#352)', async () => {
    const { rerender } = render(TranscriptDisplay, { props: { ...baseProps, file: makeFile() } });

    expect(screen.getAllByText(ORIGINAL_NAME).length).toBeGreaterThan(0);

    await rerender({
      ...baseProps,
      file: renameSpeakersInFile(makeFile(), [
        { uuid: 'spk-1', label: 'SPEAKER_01', displayName: 'Joe Rogan' },
      ]),
    });
    await tick();

    expect(screen.getAllByText('Joe Rogan').length).toBeGreaterThan(0);
    expect(screen.queryByText(ORIGINAL_NAME)).not.toBeInTheDocument();
  });

  it('renders appended pages and keeps row indices globally unique', async () => {
    const { rerender, container } = render(TranscriptDisplay, {
      props: { ...baseProps, file: makeFile() },
    });

    await rerender({
      ...baseProps,
      file: appendSegmentPage(makeFile(), {
        transcript_segments: [segment('c', 2, ORIGINAL_NAME)],
        grouped_segments: [group('c', 2)],
      }),
      totalSegments: 3,
    });
    await tick();

    expect(screen.getByText('Segment 2')).toBeInTheDocument();

    const indices = Array.from(container.querySelectorAll('[data-seg-index]')).map((el) =>
      el.getAttribute('data-seg-index')
    );
    expect(indices).toEqual(['0', '1', '2']);
  });

  it('survives two groups sharing one overlap id', () => {
    // The backend groups each page independently, so an overlap run split across a page
    // boundary yields two groups carrying the same `overlap_group_id`. Keying group rows
    // by that id produced duplicate keys, which Svelte rejects at render time and takes
    // the entire transcript list down with it. Rows are keyed by their first segment's
    // uuid instead, so this renders.
    //
    // Observed on the dev stack: file 019f2951 has run 2e64ac0b split at segment 500.
    const file = makeFile();
    file.grouped_segments = [
      { ...group('a', 0), is_overlap_group: true, overlap_group_id: 'shared-run' },
      { ...group('b', 1), is_overlap_group: true, overlap_group_id: 'shared-run' },
    ];

    render(TranscriptDisplay, { props: { ...baseProps, file } });

    expect(document.querySelectorAll('[data-segment-id]')).toHaveLength(2);
    expect(screen.getByText('Segment 0')).toBeInTheDocument();
    expect(screen.getByText('Segment 1')).toBeInTheDocument();
  });

  it('never renders one segment twice, even if a group repeats a uuid', () => {
    const file = makeFile();
    // A uuid in two groups would mount the same row twice under the same keyed each.
    file.grouped_segments = [...file.grouped_segments, group('a', 2)];

    render(TranscriptDisplay, { props: { ...baseProps, file } });

    const ids = Array.from(document.querySelectorAll('[data-segment-id]')).map(
      (el) => (el as HTMLElement).dataset.segmentId
    );
    expect(ids).toEqual([...new Set(ids)]);
  });

  it('skips groups whose segments have not been paginated in yet', () => {
    const file = makeFile();
    // A group referencing a segment from a page that has not loaded must not render an
    // empty row — the template dereferences `group.segments[0].uuid`.
    file.grouped_segments = [...file.grouped_segments, group('not-loaded-yet', 2)];

    render(TranscriptDisplay, { props: { ...baseProps, file } });

    expect(document.querySelectorAll('[data-segment-id]')).toHaveLength(2);
  });
});
