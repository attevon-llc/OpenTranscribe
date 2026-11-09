import { describe, it, expect } from 'vitest';
import {
  applySkippedSteps,
  buildTranscriptionParams,
  defaultSteps,
  shouldOfferExtraction,
  withExtractionStep,
  withoutExtractionStep,
  type TranscriptionChoices,
} from './wizardSteps';

const video = (mb: number) => {
  const f = new File(['x'], 'v.mp4', { type: 'video/mp4' });
  Object.defineProperty(f, 'size', { value: mb * 1024 * 1024 });
  return f;
};

describe('wizard steps', () => {
  it('inserts and removes the extraction step right after media', () => {
    const withIt = withExtractionStep(defaultSteps());
    expect(withIt.map((s) => s.id).slice(0, 3)).toEqual(['media', 'extraction', 'tags']);
    expect(withoutExtractionStep(withIt)).toEqual(defaultSteps());
  });

  it('re-skips only optional steps', () => {
    const out = applySkippedSteps(defaultSteps(), ['tags', 'model']);
    expect(out.find((s) => s.id === 'tags')?.skipped).toBe(true);
    expect(out.find((s) => s.id === 'model')?.skipped).toBe(false);
  });

  it('offers extraction only for large videos with auto-extract on', () => {
    const on = { auto_extract_enabled: true, extraction_threshold_mb: 100 } as never;
    expect(shouldOfferExtraction(video(150), on)).toBe(true);
    expect(shouldOfferExtraction(video(50), on)).toBe(false);
    expect(
      shouldOfferExtraction(video(150), { ...(on as object), auto_extract_enabled: false } as never)
    ).toBe(false);
    expect(shouldOfferExtraction(new File(['x'], 'a.mp3', { type: 'audio/mpeg' }), on)).toBe(false);
    expect(shouldOfferExtraction(video(150), null)).toBe(true);
  });

  it('drops speaker values the server would discard and gates consent', () => {
    const base: TranscriptionChoices = {
      speakerCountApplies: false,
      transcriptionSettings: null,
      transcriptionSystemDefaults: null,
      minSpeakers: 2,
      maxSpeakers: 4,
      numSpeakers: 3,
      selectedWhisperModel: 'tiny',
      skipSummary: true,
      voiceprintConsent: true,
      voiceprintsOn: true,
    };
    expect(buildTranscriptionParams(base)).toEqual({
      minSpeakers: null,
      maxSpeakers: null,
      numSpeakers: null,
      whisperModel: 'tiny',
      skipSummary: true,
      voiceprintConsent: null,
    });
    const applies = buildTranscriptionParams({ ...base, speakerCountApplies: true });
    expect(applies.numSpeakers).toBe(3);
    expect(applies.voiceprintConsent).toBe('granted');
    expect(
      buildTranscriptionParams({ ...base, speakerCountApplies: true, voiceprintsOn: false })
        .voiceprintConsent
    ).toBeNull();
  });
});
