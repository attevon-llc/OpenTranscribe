/**
 * `transcriptStore` holds the transcript being viewed/edited (segments + speakers). A desync
 * here is silent — a dropped edit shows no error, just the wrong transcript.
 *
 * The `processedTranscriptSegments` derived store (speaker-grouped, overlap-merged display
 * blocks) and its dedicated coverage were removed with `TranscriptModal.svelte` (issue #755,
 * J10) — that component was its only production consumer.
 */
import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import { transcriptStore, type TranscriptSegment, type SpeakerInfo } from './transcriptStore';

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
