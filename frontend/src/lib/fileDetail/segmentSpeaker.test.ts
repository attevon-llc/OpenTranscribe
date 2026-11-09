import { describe, expect, it } from 'vitest';
import type { TranscriptSegment } from '$lib/types/media';
import type { Speaker } from '$lib/types/speaker';
import {
  assignmentPatch,
  orphanedSpeakerUuid,
  pickNewSpeaker,
  rollbackPatch,
} from './segmentSpeaker';

const sp = (uuid: string, over: Partial<Speaker> = {}) =>
  ({ uuid, name: `SPEAKER_${uuid}`, ...over }) as Speaker;
const seg = (speaker: Speaker | null) => ({ speaker }) as unknown as TranscriptSegment;

describe('pickNewSpeaker', () => {
  it('is null when the segment is being unassigned', () => {
    expect(pickNewSpeaker(null, [sp('a')], sp('a'))).toBeNull();
    expect(pickNewSpeaker(undefined, [sp('a')], null)).toBeNull();
  });

  it('prefers the page list, then the just-created speaker the list cannot hold yet (#740)', () => {
    const fromList = sp('a', { display_name: 'From list' });
    expect(pickNewSpeaker('a', [fromList], sp('a', { display_name: 'created' }))).toBe(fromList);
    const created = sp('new');
    expect(pickNewSpeaker('new', [sp('a')], created)).toBe(created);
  });

  it('is null for a uuid found nowhere', () => {
    expect(pickNewSpeaker('zzz', [sp('a')], undefined)).toBeNull();
  });
});

describe('assignmentPatch', () => {
  it('shows the display name, falling back to the label, and clears all three for no speaker', () => {
    expect(assignmentPatch(sp('a', { display_name: 'Ann' }))).toMatchObject({
      speaker_id: 'a',
      resolved_speaker_name: 'Ann',
    });
    expect(assignmentPatch(sp('a'))).toMatchObject({ resolved_speaker_name: 'SPEAKER_a' });
    expect(assignmentPatch(null)).toEqual({
      speaker: null,
      speaker_id: null,
      resolved_speaker_name: null,
    });
  });
});

describe('rollbackPatch', () => {
  it('restores the original speaker and the name that was resolved for it', () => {
    const original = sp('o');
    expect(rollbackPatch(original, 'Olive')).toEqual({
      speaker: original,
      speaker_id: 'o',
      resolved_speaker_name: 'Olive',
    });
    expect(rollbackPatch(undefined, undefined)).toEqual({
      speaker: undefined,
      speaker_id: null,
      resolved_speaker_name: null,
    });
  });
});

describe('orphanedSpeakerUuid', () => {
  it('names the speaker once no segment uses it, and nothing while one still does', () => {
    const original = sp('o');
    expect(orphanedSpeakerUuid([seg(sp('x'))], original)).toBe('o');
    expect(orphanedSpeakerUuid([seg(sp('x')), seg(original)], original)).toBeNull();
  });

  it('has nothing to report for a segment that had no speaker', () => {
    expect(orphanedSpeakerUuid([], null)).toBeNull();
    expect(orphanedSpeakerUuid([], sp(''))).toBeNull();
  });
});
