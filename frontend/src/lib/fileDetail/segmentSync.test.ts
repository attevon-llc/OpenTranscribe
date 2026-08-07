import { describe, expect, it } from 'vitest';

import {
  appendSegmentPage,
  patchSegmentInFile,
  renameSpeakersInFile,
  type SegmentBearingFile,
} from './segmentSync';

/**
 * Build a file payload shaped like the real one: `grouped_segments` holds only uuid
 * references, never segment objects. A fixture that embedded copies (or aliased the same
 * objects into both places) would pass regardless of what the code does — that aliasing
 * is precisely what #352 was about.
 */
function makeFile(overrides: Partial<SegmentBearingFile> = {}): SegmentBearingFile {
  return {
    uuid: 'file-1',
    transcript_segments: [
      {
        uuid: 'seg-1',
        text: 'hello',
        speaker_id: 'spk-1',
        speaker_label: 'SPEAKER_01',
        resolved_speaker_name: 'SPEAKER_01',
        speaker: { uuid: 'spk-1', name: 'SPEAKER_01', display_name: '' },
      },
      {
        uuid: 'seg-2',
        text: 'there',
        speaker_id: 'spk-2',
        speaker_label: 'SPEAKER_02',
        resolved_speaker_name: 'SPEAKER_02',
        speaker: { uuid: 'spk-2', name: 'SPEAKER_02', display_name: '' },
      },
    ],
    grouped_segments: [
      {
        is_overlap_group: false,
        overlap_group_id: null,
        start_time: 0,
        end_time: 1,
        start_segment_index: 0,
        segment_uuids: ['seg-1'],
      },
      {
        is_overlap_group: false,
        overlap_group_id: null,
        start_time: 1,
        end_time: 2,
        start_segment_index: 1,
        segment_uuids: ['seg-2'],
      },
    ],
    ...overrides,
  };
}

/** Read a segment positionally. The helper keeps the assertions readable now that
 * `transcript_segments` is optional on the payload type. */
function seg(file: SegmentBearingFile, index: number): any {
  const segments = file.transcript_segments;
  if (!segments) throw new Error('expected transcript_segments');
  return segments[index];
}

describe('renameSpeakersInFile', () => {
  it('renames every segment of the matched speaker', () => {
    const file = makeFile();
    const next = renameSpeakersInFile(file, [
      { uuid: 'spk-1', label: 'SPEAKER_01', displayName: 'Joe Rogan' },
    ]);

    expect(seg(next, 0).resolved_speaker_name).toBe('Joe Rogan');
    expect(seg(next, 0).speaker.display_name).toBe('Joe Rogan');
    expect(seg(next, 1).resolved_speaker_name).toBe('SPEAKER_02');
  });

  it('does not mutate the input file or its segments', () => {
    const file = makeFile();
    const snapshot = structuredClone(file);

    renameSpeakersInFile(file, [{ uuid: 'spk-1', displayName: 'Joe Rogan' }]);

    expect(file).toEqual(snapshot);
  });

  it('returns a new top-level object so Svelte invalidates', () => {
    const file = makeFile();
    expect(renameSpeakersInFile(file, [{ uuid: 'spk-1', displayName: 'X' }])).not.toBe(file);
  });

  it('never rewrites the fields speaker colours hash', () => {
    const file = makeFile();
    const next = renameSpeakersInFile(file, [
      { uuid: 'spk-1', label: 'SPEAKER_01', displayName: 'Joe Rogan' },
    ]);

    expect(seg(next, 0).speaker_label).toBe('SPEAKER_01');
    expect(seg(next, 0).speaker.name).toBe('SPEAKER_01');
  });

  it('keeps object identity for segments it does not touch', () => {
    const file = makeFile();
    const untouched = seg(file, 1);

    const next = renameSpeakersInFile(file, [{ uuid: 'spk-1', displayName: 'Joe Rogan' }]);

    expect(seg(next, 1)).toBe(untouched);
  });

  it('synthesizes a missing speaker with a uuid key, not an id key', () => {
    const file = makeFile({
      transcript_segments: [
        { uuid: 'seg-1', text: 'hi', speaker_id: 'spk-1', speaker_label: 'SPEAKER_01' },
      ],
    });

    const speaker = seg(
      renameSpeakersInFile(file, [
        { uuid: 'spk-1', label: 'SPEAKER_01', displayName: 'Joe Rogan' },
      ]),
      0
    ).speaker;

    expect(speaker).toEqual({
      uuid: 'spk-1',
      name: 'SPEAKER_01',
      display_name: 'Joe Rogan',
    });
    expect(speaker).not.toHaveProperty('id');
  });

  it('never uses a uuid as the synthesized speaker name (it drives the colour hash)', () => {
    const file = makeFile({
      transcript_segments: [{ uuid: 'seg-1', text: 'hi', speaker_id: 'spk-1' }],
    });

    const speaker = seg(
      renameSpeakersInFile(file, [
        { uuid: 'spk-1', label: 'SPEAKER_07', displayName: 'Joe Rogan' },
      ]),
      0
    ).speaker;

    expect(speaker.name).toBe('SPEAKER_07');
  });

  it('matches on speaker.uuid when speaker_id is absent', () => {
    const file = makeFile({
      transcript_segments: [
        { uuid: 'seg-1', text: 'hi', speaker: { uuid: 'spk-1', name: 'SPEAKER_01' } },
      ],
    });

    const next = renameSpeakersInFile(file, [{ uuid: 'spk-1', displayName: 'Joe Rogan' }]);

    expect(seg(next, 0).speaker.display_name).toBe('Joe Rogan');
  });

  it('falls back to the SPEAKER_XX label when no uuid matches (bulk-save path)', () => {
    const file = makeFile({
      transcript_segments: [{ uuid: 'seg-1', text: 'hi', speaker_label: 'SPEAKER_01' }],
    });

    const next = renameSpeakersInFile(file, [
      { uuid: 'unrelated', label: 'SPEAKER_01', displayName: 'Joe Rogan' },
    ]);

    expect(seg(next, 0).resolved_speaker_name).toBe('Joe Rogan');
  });

  it('applies several renames in one pass', () => {
    const next = renameSpeakersInFile(makeFile(), [
      { uuid: 'spk-1', displayName: 'Joe' },
      { uuid: 'spk-2', displayName: 'Lex' },
    ]);

    expect(next.transcript_segments?.map((s: any) => s.resolved_speaker_name)).toEqual([
      'Joe',
      'Lex',
    ]);
  });

  it('does not invent transcript_segments when the payload has none', () => {
    // The route renders the transcript column behind `file.transcript_segments`, and an
    // empty array is truthy — adding the key would mount a column with nothing in it.
    const next = renameSpeakersInFile({ uuid: 'file-1' }, [{ uuid: 'spk-1', displayName: 'X' }]);
    expect('transcript_segments' in next).toBe(false);
  });
});

describe('patchSegmentInFile', () => {
  it('merges the patch into the matching segment only', () => {
    const next = patchSegmentInFile(makeFile(), 'seg-2', { text: 'edited' });

    expect(seg(next, 1).text).toBe('edited');
    expect(seg(next, 0).text).toBe('hello');
  });

  it('leaves the segment’s other fields intact', () => {
    const next = patchSegmentInFile(makeFile(), 'seg-1', { text: 'edited' });
    const segment = seg(next, 0);

    expect(segment.speaker_label).toBe('SPEAKER_01');
    expect(segment.speaker.uuid).toBe('spk-1');
  });

  it('does not mutate the input', () => {
    const file = makeFile();
    const snapshot = structuredClone(file);

    patchSegmentInFile(file, 'seg-1', { text: 'edited' });

    expect(file).toEqual(snapshot);
  });
});

describe('appendSegmentPage', () => {
  const nextPage = {
    transcript_segments: [{ uuid: 'seg-3', text: 'more' }],
    grouped_segments: [
      {
        is_overlap_group: false,
        overlap_group_id: null,
        start_time: 2,
        end_time: 3,
        start_segment_index: 2,
        segment_uuids: ['seg-3'],
      },
    ],
  };

  it('advances both representations together', () => {
    const next = appendSegmentPage(makeFile(), nextPage);

    expect(next.transcript_segments?.map((x: any) => x.uuid)).toEqual(['seg-1', 'seg-2', 'seg-3']);
    expect(next.grouped_segments?.map((g) => g.segment_uuids)).toEqual([
      ['seg-1'],
      ['seg-2'],
      ['seg-3'],
    ]);
  });

  it('preserves global group indices from the server', () => {
    const next = appendSegmentPage(makeFile(), nextPage);
    expect(next.grouped_segments?.map((g) => g.start_segment_index)).toEqual([0, 1, 2]);
  });

  it('does not duplicate segments when the same page arrives twice', () => {
    // `transcript_segments` feeds the transcript store, exports and search indexing, so a
    // repeated append must not double up. (Repeated *groups* are harmless: TranscriptDisplay
    // claims each segment for exactly one group at render time.)
    const once = appendSegmentPage(makeFile(), nextPage);
    const twice = appendSegmentPage(once, nextPage);

    expect(twice.transcript_segments?.map((s: any) => s.uuid)).toEqual(['seg-1', 'seg-2', 'seg-3']);
  });

  it('stitches an overlap run split across the page boundary', () => {
    const file = makeFile({
      transcript_segments: [{ uuid: 'seg-1', text: 'a' }],
      grouped_segments: [
        {
          is_overlap_group: false,
          overlap_group_id: 'ov-1',
          start_time: 0,
          end_time: 1,
          start_segment_index: 0,
          segment_uuids: ['seg-1'],
        },
      ],
    });

    const next = appendSegmentPage(file, {
      transcript_segments: [{ uuid: 'seg-2', text: 'b' }],
      grouped_segments: [
        {
          is_overlap_group: false,
          overlap_group_id: 'ov-1',
          start_time: 0.5,
          end_time: 2,
          start_segment_index: 1,
          segment_uuids: ['seg-2'],
        },
      ],
    });

    expect(next.grouped_segments).toHaveLength(1);
    expect(next.grouped_segments?.[0]).toMatchObject({
      is_overlap_group: true,
      overlap_group_id: 'ov-1',
      start_time: 0,
      end_time: 2,
      start_segment_index: 0,
      segment_uuids: ['seg-1', 'seg-2'],
    });
  });

  it('stitches a multi-member run split mid-way (the shape real data produces)', () => {
    // Verified against the dev stack: a 4-member overlap run cut at the page boundary
    // arrives as 2 + 2, both halves flagged as overlap groups sharing one id.
    const file = makeFile({
      transcript_segments: [{ uuid: 's1' }, { uuid: 's2' }],
      grouped_segments: [
        {
          is_overlap_group: true,
          overlap_group_id: 'ov-1',
          start_time: 0,
          end_time: 2,
          start_segment_index: 0,
          segment_uuids: ['s1', 's2'],
        },
      ],
    });

    const next = appendSegmentPage(file, {
      transcript_segments: [{ uuid: 's3' }, { uuid: 's4' }],
      grouped_segments: [
        {
          is_overlap_group: true,
          overlap_group_id: 'ov-1',
          start_time: 1.5,
          end_time: 4,
          start_segment_index: 2,
          segment_uuids: ['s3', 's4'],
        },
      ],
    });

    expect(next.grouped_segments).toHaveLength(1);
    expect(next.grouped_segments?.[0].segment_uuids).toEqual(['s1', 's2', 's3', 's4']);
    expect(next.grouped_segments?.[0].end_time).toBe(4);
  });

  it('does not merge adjacent groups from different overlap runs', () => {
    const file = makeFile({
      transcript_segments: [{ uuid: 'seg-1', text: 'a' }],
      grouped_segments: [
        {
          is_overlap_group: false,
          overlap_group_id: 'ov-1',
          start_time: 0,
          end_time: 1,
          start_segment_index: 0,
          segment_uuids: ['seg-1'],
        },
      ],
    });

    const next = appendSegmentPage(file, {
      transcript_segments: [{ uuid: 'seg-2', text: 'b' }],
      grouped_segments: [
        {
          is_overlap_group: false,
          overlap_group_id: 'ov-2',
          start_time: 1,
          end_time: 2,
          start_segment_index: 1,
          segment_uuids: ['seg-2'],
        },
      ],
    });

    expect(next.grouped_segments).toHaveLength(2);
  });

  it('handles a page with no grouping (redaction-pending shape)', () => {
    const next = appendSegmentPage(makeFile(), { transcript_segments: [], grouped_segments: [] });

    expect(next.transcript_segments).toHaveLength(2);
    expect(next.grouped_segments).toHaveLength(2);
  });
});
