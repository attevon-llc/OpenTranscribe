import { describe, it, expect } from 'vitest';
import type { Speaker } from '$lib/types/speaker';
import {
  MAX_PROFILE_MATCHES,
  confirmedSpeakerName,
  inlineNameCheck,
  matchingProfileNames,
  newSpeakerNameState,
  partitionSpeakers,
  pendingSuggestionOf,
  speakerMenuLabel,
  speakerNumberLabelOf,
  uniqueProfileNames,
  type WireSpeaker,
} from './speakerMenu';

const wire = (o: Partial<WireSpeaker> = {}): WireSpeaker => ({
  uuid: 's1',
  name: 'SPEAKER_00',
  ...o,
});
const spk = (o: Partial<Speaker> = {}): Speaker => ({ uuid: 'u', name: 'SPEAKER_00', ...o });
const identity = (s: string) => s;

describe('confirmedSpeakerName', () => {
  it('reads display_name only, trimmed', () => {
    expect(confirmedSpeakerName(wire({ display_name: '  Alice ' }))).toBe('Alice');
    expect(confirmedSpeakerName(wire({ suggested_name: 'Bob' }))).toBe('');
    expect(confirmedSpeakerName(null)).toBe('');
  });
});

describe('speakerNumberLabelOf', () => {
  it('prefers the speaker slot, then the segment label', () => {
    expect(speakerNumberLabelOf(wire({ name: 'SPEAKER_03' }), 'SPEAKER_09')).toBe('SPEAKER_03');
    expect(speakerNumberLabelOf(null, 'SPEAKER_09')).toBe('SPEAKER_09');
    expect(speakerNumberLabelOf(null, undefined)).toBe('');
  });
});

describe('pendingSuggestionOf', () => {
  it('offers a suggestion with its confidence while nothing is confirmed', () => {
    expect(pendingSuggestionOf(wire({ suggested_name: 'Bob', confidence: 0.8 }), '')).toEqual({
      uuid: 's1',
      name: 'Bob',
      confidence: 0.8,
    });
  });
  it('defaults a missing confidence to 0', () => {
    expect(pendingSuggestionOf(wire({ suggested_name: 'Bob' }), '')?.confidence).toBe(0);
  });
  it('offers nothing once a human confirmed a name, or without a speaker uuid', () => {
    expect(pendingSuggestionOf(wire({ suggested_name: 'Bob' }), 'Alice')).toBeNull();
    expect(pendingSuggestionOf(wire({ suggested_name: 'Bob', uuid: '' }), '')).toBeNull();
    expect(pendingSuggestionOf(null, '')).toBeNull();
  });
});

describe('inlineNameCheck', () => {
  it('classifies what may be saved', () => {
    expect(inlineNameCheck('   ', '')).toBe('empty');
    expect(inlineNameCheck('SPEAKER_07', '')).toBe('placeholder');
    expect(inlineNameCheck(' Alice ', 'Alice')).toBe('unchanged');
    expect(inlineNameCheck('Laura', 'Alice')).toBe('ok');
  });
});

describe('uniqueProfileNames', () => {
  it('drops blanks and repeats, keeping first-seen order', () => {
    expect(uniqueProfileNames([{ name: 'b' }, { name: '' }, { name: 'a' }, { name: 'b' }])).toEqual(
      ['b', 'a']
    );
    expect(uniqueProfileNames(null)).toEqual([]);
  });
});

describe('matchingProfileNames', () => {
  it('is a case-insensitive substring match, sorted, and empty for an empty query', () => {
    const names = ['Zoe Laurent', 'laura', 'Bob'];
    expect(matchingProfileNames(names, ' LAUR ')).toEqual(['laura', 'Zoe Laurent']);
    expect(matchingProfileNames(names, '   ')).toEqual([]);
  });
  it('caps the list', () => {
    const names = Array.from({ length: 9 }, (_, i) => `name${i}`);
    expect(matchingProfileNames(names, 'name')).toHaveLength(MAX_PROFILE_MATCHES);
  });
});

describe('partitionSpeakers', () => {
  it('treats a renamed SPEAKER_NN as named', () => {
    const { named, auto } = partitionSpeakers([
      spk({ uuid: 'a', name: 'SPEAKER_00' }),
      spk({ uuid: 'b', name: 'SPEAKER_01', display_name: 'John' }),
    ]);
    expect(named.map((s) => s.uuid)).toEqual(['b']);
    expect(auto.map((s) => s.uuid)).toEqual(['a']);
  });
});

describe('speakerMenuLabel', () => {
  it('appends the slot number to a renamed slot speaker', () => {
    expect(speakerMenuLabel(spk({ name: 'SPEAKER_02', display_name: 'John' }), identity)).toBe(
      'John (02)'
    );
  });
  it('shows a bare slot or a free-form name as is', () => {
    expect(speakerMenuLabel(spk({ name: 'SPEAKER_02' }), identity)).toBe('SPEAKER_02');
    expect(speakerMenuLabel(spk({ name: 'Guest', display_name: 'Guest A' }), identity)).toBe(
      'Guest A'
    );
  });
  it('translates the effective name', () => {
    expect(speakerMenuLabel(spk({ name: 'SPEAKER_02' }), (n) => `<${n}>`)).toBe('<SPEAKER_02>');
  });
});

describe('newSpeakerNameState', () => {
  it('allows a real name', () => {
    expect(newSpeakerNameState('Laura', false)).toEqual({ isPlaceholder: false, canCreate: true });
  });
  it('rejects a blank name without calling it a placeholder', () => {
    expect(newSpeakerNameState('   ', false)).toEqual({ isPlaceholder: false, canCreate: false });
  });
  it('rejects SPEAKER_NN as a placeholder (#740)', () => {
    expect(newSpeakerNameState(' SPEAKER_04 ', false)).toEqual({
      isPlaceholder: true,
      canCreate: false,
    });
  });
  it('blocks a second create while one is in flight', () => {
    expect(newSpeakerNameState('Laura', true).canCreate).toBe(false);
  });
});
