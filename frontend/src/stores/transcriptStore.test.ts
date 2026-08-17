/**
 * `transcriptStore` holds the transcript being viewed/edited; `processedTranscriptSegments`
 * derives the speaker-grouped, overlap-merged display blocks from it. A desync here is
 * silent — wrong grouping or a dropped edit shows no error, just the wrong transcript —
 * which is exactly the class of bug `backend/tests/CLAUDE.md`'s "split store module" E2E
 * trap describes for state stores generally.
 */
import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import {
  transcriptStore,
  processedTranscriptSegments,
  type TranscriptSegment,
  type SpeakerInfo,
} from './transcriptStore';

function segment(overrides: Partial<TranscriptSegment> = {}): TranscriptSegment {
  return {
    uuid: 'seg-1',
    start_time: 0,
    end_time: 1,
    text: 'hello',
    ...overrides,
  };
}

function speaker(overrides: Partial<SpeakerInfo> = {}): SpeakerInfo {
  return { uuid: 'spk-1', name: 'SPEAKER_00', verified: false, ...overrides };
}

describe('loadTranscriptData / clear', () => {
  it('deep-copies segments and speakers so external mutation cannot leak in', () => {
    const seg = segment();
    const spk = speaker();
    transcriptStore.loadTranscriptData('file-1', [seg], [spk]);

    seg.text = 'mutated after load';
    spk.name = 'mutated after load';

    const data = get(transcriptStore);
    expect(data.segments[0].text).toBe('hello');
    expect(data.speakers[0].name).toBe('SPEAKER_00');
  });

  it('clear() resets to the empty state', () => {
    transcriptStore.loadTranscriptData('file-1', [segment()], [speaker()]);
    transcriptStore.clear();

    expect(get(transcriptStore)).toEqual({ fileId: null, segments: [], speakers: [] });
  });
});

describe('updateSpeakerName', () => {
  it('renames the speaker AND every segment attributed to them, preserving the color-mapping name', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [
        segment({
          uuid: 'seg-1',
          speaker_id: 'spk-1',
          speaker: { uuid: 'spk-1', name: 'SPEAKER_00' },
        }),
        segment({ uuid: 'seg-2', speaker_id: 'spk-2' }), // a different speaker, must be untouched
      ],
      [speaker({ uuid: 'spk-1' }), speaker({ uuid: 'spk-2', name: 'SPEAKER_01' })]
    );

    transcriptStore.updateSpeakerName('spk-1', 'Alice');

    const data = get(transcriptStore);
    expect(data.speakers.find((s) => s.uuid === 'spk-1')?.display_name).toBe('Alice');
    const renamed = data.segments.find((s) => s.uuid === 'seg-1')!;
    expect(renamed.resolved_speaker_name).toBe('Alice');
    expect(renamed.speaker?.display_name).toBe('Alice');
    // The original speaker name is what segment colors key off — renaming must not touch it.
    expect(renamed.speaker?.name).toBe('SPEAKER_00');

    const untouched = data.segments.find((s) => s.uuid === 'seg-2')!;
    expect(untouched.resolved_speaker_name).toBeUndefined();
  });

  it('synthesizes a speaker object for a segment that never had one embedded', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [segment({ uuid: 'seg-1', speaker_id: 'spk-1', speaker_label: 'SPEAKER_00' })],
      [speaker({ uuid: 'spk-1' })]
    );

    transcriptStore.updateSpeakerName('spk-1', 'Bob');

    const data = get(transcriptStore);
    expect(data.segments[0].speaker).toMatchObject({ uuid: 'spk-1', display_name: 'Bob' });
  });
});

describe('updateSegmentText', () => {
  it('updates only the text of the matching segment, preserving every other field', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [
        segment({
          uuid: 'seg-1',
          speaker_id: 'spk-1',
          resolved_speaker_name: 'Alice',
          confidence: 0.95,
        }),
      ],
      [speaker()]
    );

    transcriptStore.updateSegmentText('seg-1', 'corrected text');

    const updated = get(transcriptStore).segments[0];
    expect(updated.text).toBe('corrected text');
    expect(updated.speaker_id).toBe('spk-1');
    expect(updated.resolved_speaker_name).toBe('Alice');
    expect(updated.confidence).toBe(0.95);
  });

  it('leaves segments with a different uuid untouched', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [segment({ uuid: 'seg-1', text: 'a' }), segment({ uuid: 'seg-2', text: 'b' })],
      []
    );

    transcriptStore.updateSegmentText('seg-1', 'edited');

    expect(get(transcriptStore).segments.find((s) => s.uuid === 'seg-2')?.text).toBe('b');
  });
});

describe('processedTranscriptSegments — grouping', () => {
  it('is empty for an empty transcript', () => {
    transcriptStore.clear();
    expect(get(processedTranscriptSegments)).toEqual([]);
  });

  it('sorts out-of-order segments by start_time before grouping', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [
        segment({
          uuid: 'b',
          start_time: 5,
          end_time: 6,
          text: 'second',
          resolved_speaker_name: 'Alice',
        }),
        segment({
          uuid: 'a',
          start_time: 0,
          end_time: 1,
          text: 'first',
          resolved_speaker_name: 'Alice',
        }),
      ],
      []
    );

    const [block] = get(processedTranscriptSegments);
    expect(block.text).toBe('first second');
  });

  it('merges consecutive same-speaker segments into one block and keeps the raw index/count', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [
        segment({
          uuid: 'a',
          start_time: 0,
          end_time: 1,
          text: 'hello',
          resolved_speaker_name: 'Alice',
        }),
        segment({
          uuid: 'b',
          start_time: 1,
          end_time: 2,
          text: 'world',
          resolved_speaker_name: 'Alice',
        }),
      ],
      []
    );

    const blocks = get(processedTranscriptSegments);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      text: 'hello world',
      startTime: 0,
      endTime: 2,
      rawStartIndex: 0,
      rawSegmentCount: 2,
    });
  });

  it('starts a new block when the speaker changes, even with adjacent timestamps', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [
        segment({
          uuid: 'a',
          start_time: 0,
          end_time: 1,
          text: 'hi',
          resolved_speaker_name: 'Alice',
        }),
        segment({
          uuid: 'b',
          start_time: 1,
          end_time: 2,
          text: 'hey',
          resolved_speaker_name: 'Bob',
        }),
      ],
      []
    );

    const blocks = get(processedTranscriptSegments);
    expect(blocks.map((b) => b.speakerName)).toEqual(['Alice', 'Bob']);
  });

  it('starts a new block on an overlap_group_id change even for the same speaker', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [
        segment({
          uuid: 'a',
          start_time: 0,
          end_time: 1,
          text: 'first',
          resolved_speaker_name: 'Alice',
          overlap_group_id: 'grp-1',
        }),
        segment({
          uuid: 'b',
          start_time: 1,
          end_time: 2,
          text: 'second',
          resolved_speaker_name: 'Alice',
          overlap_group_id: undefined,
        }),
      ],
      []
    );

    const blocks = get(processedTranscriptSegments);
    expect(blocks).toHaveLength(2);
  });

  it('falls back to "Unknown Speaker" only when resolved_speaker_name is absent', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [segment({ uuid: 'a', resolved_speaker_name: undefined })],
      []
    );

    expect(get(processedTranscriptSegments)[0].speakerName).toBe('Unknown Speaker');
  });
});

describe('processedTranscriptSegments — overlap groups', () => {
  it('collapses 2+ consecutive blocks sharing an overlap_group_id into one overlap container', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [
        segment({
          uuid: 'a',
          start_time: 0,
          end_time: 3,
          text: 'alice talking',
          resolved_speaker_name: 'Alice',
          overlap_group_id: 'grp-1',
        }),
        segment({
          uuid: 'b',
          start_time: 1,
          end_time: 2,
          text: 'bob interjects',
          resolved_speaker_name: 'Bob',
          overlap_group_id: 'grp-1',
        }),
      ],
      []
    );

    const blocks = get(processedTranscriptSegments);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].isOverlapGroup).toBe(true);
    expect(blocks[0].speakerName).toBe('2 speakers overlapping');
    expect(blocks[0].startTime).toBe(0);
    expect(blocks[0].endTime).toBe(3);
    expect(blocks[0].overlapSegments).toHaveLength(2);
    expect(blocks[0].rawSegmentCount).toBe(2);
  });

  it('does not build an overlap container for a lone segment carrying an overlap_group_id', () => {
    transcriptStore.loadTranscriptData(
      'file-1',
      [
        segment({
          uuid: 'a',
          resolved_speaker_name: 'Alice',
          overlap_group_id: 'grp-1',
        }),
        segment({
          uuid: 'b',
          start_time: 5,
          end_time: 6,
          resolved_speaker_name: 'Bob',
          overlap_group_id: undefined,
        }),
      ],
      []
    );

    const blocks = get(processedTranscriptSegments);
    expect(blocks.every((b) => !b.isOverlapGroup)).toBe(true);
  });
});
