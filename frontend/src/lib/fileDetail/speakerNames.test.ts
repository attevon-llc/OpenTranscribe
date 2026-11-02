import { describe, it, expect } from 'vitest';
import {
  baselineFrom,
  hasUnsavedSpeakerNames,
  renameProfileSuggestions,
  savedSpeakerRenames,
  speakerHasUnsavedChange,
  speakersFromSegments,
  speakersNeedingProfileDecision,
  unsavedNamesToKeep,
  validateSpeakerName,
  type SpeakerItem,
} from './speakerNames';

const t = (key: string) => key;

function speaker(overrides: Partial<SpeakerItem> = {}): SpeakerItem {
  return { uuid: 'u1', name: 'SPEAKER_01', display_name: 'Alice', verified: false, ...overrides };
}

describe('validateSpeakerName', () => {
  it('rejects missing, blank and over-long names, accepts 100 characters', () => {
    expect(validateSpeakerName('', t)).toEqual({
      isValid: false,
      error: 'speakerValidation.nameRequired',
    });
    expect(validateSpeakerName('   ', t).error).toBe('speakerValidation.nameEmpty');
    expect(validateSpeakerName('x'.repeat(101), t).error).toBe('speakerValidation.nameTooLong');
    expect(validateSpeakerName('x'.repeat(100), t)).toEqual({ isValid: true });
  });
});

describe('change detection', () => {
  const baseline = new Map([['u1', 'Alice']]);

  it('a clear to empty is a change; trailing whitespace is not', () => {
    expect(speakerHasUnsavedChange(speaker({ display_name: '' }), baseline)).toBe(true);
    expect(speakerHasUnsavedChange(speaker({ display_name: 'Alice  ' }), baseline)).toBe(false);
    expect(speakerHasUnsavedChange(speaker({ uuid: '' }), baseline)).toBe(false);
  });

  it('hasUnsavedSpeakerNames is false for an empty list and true when any name moved', () => {
    expect(hasUnsavedSpeakerNames([], baseline)).toBe(false);
    expect(hasUnsavedSpeakerNames([speaker()], baseline)).toBe(false);
    expect(hasUnsavedSpeakerNames([speaker({ display_name: 'Bob' })], baseline)).toBe(true);
  });

  it('baselineFrom trims each display name', () => {
    expect(baselineFrom([{ uuid: 'a', display_name: ' Ann ' }, { uuid: 'b' }])).toEqual(
      new Map([
        ['a', 'Ann'],
        ['b', ''],
      ])
    );
  });

  it('unsavedNamesToKeep keeps only edited names of speakers that have a baseline', () => {
    const kept = unsavedNamesToKeep(
      [speaker({ display_name: 'Alicia ' }), speaker({ uuid: 'new', display_name: 'X' })],
      baseline
    );
    expect(kept).toEqual(new Map([['u1', 'Alicia ']]));
  });
});

describe('speakersNeedingProfileDecision', () => {
  const linked = speaker({ display_name: 'Alicia', profile: { uuid: 'p1', name: 'Alice' } });

  it('needs a decision for a linked profile whose name differs', () => {
    expect(speakersNeedingProfileDecision([linked], false)).toEqual([linked]);
  });

  it('never for a clear, a matching name, an unlinked speaker, or diarization off', () => {
    const cleared = { ...linked, display_name: '' };
    const same = { ...linked, display_name: 'Alice' };
    const unlinked = speaker({ display_name: 'Alicia' });
    expect(speakersNeedingProfileDecision([cleared, same, unlinked], false)).toEqual([]);
    expect(speakersNeedingProfileDecision([linked], true)).toEqual([]);
  });
});

describe('renameProfileSuggestions', () => {
  it('renames matching profile suggestions only', () => {
    const s = speaker({
      profile_suggestions: [
        { name: 'Alice', suggestion_type: 'profile' },
        { name: 'Alice', suggestion_type: 'voice' },
        { name: 'Bob', suggestion_type: 'profile' },
      ],
    });
    const [out] = renameProfileSuggestions(
      [s],
      new Map([['p1', { oldName: 'Alice', newName: 'Alicia' }]])
    );
    expect(out.profile_suggestions?.map((x) => x.name)).toEqual(['Alicia', 'Alice', 'Bob']);
  });

  it('returns the same list when nothing was renamed globally', () => {
    const list = [speaker()];
    expect(renameProfileSuggestions(list, new Map())).toBe(list);
  });
});

describe('savedSpeakerRenames', () => {
  it('skips failures and paints a cleared speaker with its raw label', () => {
    const renames = savedSpeakerRenames(
      [
        speaker({ uuid: 'a', name: 'SPEAKER_00', display_name: ' Ann ' }),
        speaker({ uuid: 'b', name: 'SPEAKER_01', display_name: '' }),
        speaker({ uuid: 'c', name: 'SPEAKER_02', display_name: 'Cy' }),
      ],
      new Set(['c'])
    );
    expect(renames).toEqual([
      { uuid: 'a', label: 'SPEAKER_00', displayName: 'Ann' },
      { uuid: 'b', label: 'SPEAKER_01', displayName: 'SPEAKER_01' },
    ]);
  });
});

describe('speakersFromSegments', () => {
  it('one entry per label in first-seen order, unknown label as last resort', () => {
    const speakers = speakersFromSegments(
      [
        { speaker_label: 'SPEAKER_01', speaker: { display_name: 'Ann' } },
        { speaker: { name: 'SPEAKER_02' } },
        { speaker_label: 'SPEAKER_01' },
        {},
      ],
      'unknown'
    );
    expect(speakers).toEqual([
      { name: 'SPEAKER_01', display_name: 'Ann' },
      { name: 'SPEAKER_02', display_name: 'SPEAKER_02' },
      { name: 'unknown', display_name: 'unknown' },
    ]);
  });
});
