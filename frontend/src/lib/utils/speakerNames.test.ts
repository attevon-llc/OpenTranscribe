/**
 * These pin the shared placeholder-vs-human contract, which the segment speaker
 * dropdown, the speaker editor panel and (separately) the backend's SQL predicate
 * all key off. The cases that matter are the boundaries the old open-coded copies
 * disagreed on: blank names, and a real name that merely starts with "SPEAKER_".
 */
import { describe, it, expect } from 'vitest';
import {
  isPlaceholderSpeakerName,
  placeholderSpeakerNumber,
  nextPlaceholderSpeakerName,
} from './speakerNames';

describe('isPlaceholderSpeakerName', () => {
  it.each(['SPEAKER_00', 'SPEAKER_1', 'SPEAKER_123', ' SPEAKER_02 '])(
    'treats %s as a diarization placeholder',
    (name) => {
      expect(isPlaceholderSpeakerName(name)).toBe(true);
    }
  );

  it.each([null, undefined, '', '   '])('treats %s as a placeholder too', (name) => {
    expect(isPlaceholderSpeakerName(name)).toBe(true);
  });

  it.each([
    'Alice Nakamura',
    // The `.startsWith('SPEAKER_')` variant this module replaces called both of
    // these placeholders, hiding a genuinely named speaker from every "labeled
    // speakers" list.
    'SPEAKER_OF_THE_HOUSE',
    'SPEAKER_02b',
    'Speaker 2',
  ])('treats %s as a human-assigned name', (name) => {
    expect(isPlaceholderSpeakerName(name)).toBe(false);
  });
});

describe('placeholderSpeakerNumber', () => {
  it('reads the slot number out of a placeholder', () => {
    expect(placeholderSpeakerNumber('SPEAKER_07')).toBe(7);
    expect(placeholderSpeakerNumber('SPEAKER_12')).toBe(12);
  });

  it('returns null for anything that is not a numbered placeholder', () => {
    expect(placeholderSpeakerNumber('Alice')).toBeNull();
    expect(placeholderSpeakerNumber('')).toBeNull();
    expect(placeholderSpeakerNumber(null)).toBeNull();
  });
});

describe('nextPlaceholderSpeakerName', () => {
  it('takes the highest existing slot plus one, ignoring human names', () => {
    expect(nextPlaceholderSpeakerName(['SPEAKER_00', 'Alice', 'SPEAKER_02'])).toBe('SPEAKER_03');
  });

  it('starts at SPEAKER_00 when there are no placeholders at all', () => {
    expect(nextPlaceholderSpeakerName([])).toBe('SPEAKER_00');
    expect(nextPlaceholderSpeakerName(['Alice', 'Bob'])).toBe('SPEAKER_00');
  });

  it('pads to two digits but does not truncate past nine', () => {
    expect(nextPlaceholderSpeakerName(['SPEAKER_08'])).toBe('SPEAKER_09');
    expect(nextPlaceholderSpeakerName(['SPEAKER_09'])).toBe('SPEAKER_10');
    expect(nextPlaceholderSpeakerName(['SPEAKER_99'])).toBe('SPEAKER_100');
  });
});
