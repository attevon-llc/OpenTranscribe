import { describe, it, expect, beforeEach } from 'vitest';
import {
  loadRecordingSettings,
  restorePreviousValues,
  savePreviousValues,
  type UploadPreviousValues,
} from './uploadPreferences';
import { defaultSteps } from './wizardSteps';

const prev = (over: Partial<UploadPreviousValues> = {}): UploadPreviousValues => ({
  collectionIds: ['c1', 'gone'],
  collectionNames: ['One', 'Gone'],
  tagNames: ['t'],
  minSpeakers: 1,
  maxSpeakers: null,
  numSpeakers: null,
  skipSummary: true,
  selectedWhisperModel: 'tiny',
  skippedSteps: ['tags'],
  timestamp: 0,
  ...over,
});

describe('restorePreviousValues', () => {
  beforeEach(() => localStorage.clear());

  it('returns null with nothing stored or unparsable storage', () => {
    expect(restorePreviousValues([], defaultSteps())).toBeNull();
    localStorage.setItem('opentr:uploadPreviousValues', '{bad');
    expect(restorePreviousValues([], defaultSteps())).toBeNull();
  });

  it('round-trips, keeping only collections that still exist', () => {
    savePreviousValues(prev());
    const out = restorePreviousValues([{ uuid: 'c1', name: 'One' }], defaultSteps())!;
    expect(out.collections).toEqual([{ uuid: 'c1', name: 'One' }]);
    expect(out.tags).toEqual(['t']);
    expect(out.maxSpeakers).toBeNull();
    expect(out.skipSummary).toBe(true);
    expect(out.steps?.find((s) => s.id === 'tags')?.skipped).toBe(true);
  });
});

describe('loadRecordingSettings', () => {
  beforeEach(() => localStorage.clear());
  it('defaults, converts minutes to seconds, and survives bad JSON', () => {
    expect(loadRecordingSettings().maxRecordingDuration).toBe(7200);
    localStorage.setItem(
      'recordingSettings',
      JSON.stringify({ maxRecordingDuration: 30, autoStopEnabled: false })
    );
    expect(loadRecordingSettings()).toEqual({
      maxRecordingDuration: 1800,
      recordingQuality: 'high',
      autoStopEnabled: false,
    });
    localStorage.setItem('recordingSettings', 'nope');
    expect(loadRecordingSettings().maxRecordingDuration).toBe(7200);
  });
});
