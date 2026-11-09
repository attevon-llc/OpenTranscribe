import type { AudioExtractionSettings } from '$lib/api/audioExtractionSettings';
import type { UploadTranscriptionParams } from '$lib/services/uploadService';
import { speakerSubmitRange } from '$lib/api/transcriptionSettings';
import type {
  TranscriptionSettings,
  TranscriptionSystemDefaults,
} from '$lib/api/transcriptionSettings';

export type StepId =
  | 'media'
  | 'extraction'
  | 'tags'
  | 'collections'
  | 'speakers'
  | 'model'
  | 'review';

export interface StepConfig {
  id: StepId;
  labelKey: string;
  optional: boolean;
  skipped: boolean;
  /**
   * Optional per-step content width (any valid CSS `max-width` value), applied
   * to `.step-content` for the active step. All steps share one `.step-body`
   * (see the modal's fixed width), so a step whose natural content is narrower
   * than the others — e.g. a short list of chips — would otherwise stretch to
   * fill the same width as a step with a wide filter/create row (#751 item 1).
   */
  contentWidth?: string;
}

export function defaultSteps(): StepConfig[] {
  return [
    { id: 'media', labelKey: 'uploader.stepMedia', optional: false, skipped: false },
    {
      id: 'tags',
      labelKey: 'uploader.stepTags',
      optional: true,
      skipped: false,
      contentWidth: '420px',
    },
    { id: 'collections', labelKey: 'uploader.stepCollections', optional: true, skipped: false },
    // Model comes first: choosing the Fast model (or having speaker detection off) removes the
    // Speakers step, so the user must not type a range before learning it would be discarded.
    { id: 'model', labelKey: 'uploader.stepModel', optional: false, skipped: false },
    { id: 'speakers', labelKey: 'uploader.stepSpeakers', optional: false, skipped: false },
    { id: 'review', labelKey: 'uploader.stepReview', optional: false, skipped: false },
  ];
}

/** Insert the extraction step right after `media`. */
export function withExtractionStep(steps: StepConfig[]): StepConfig[] {
  return [
    steps[0], // media
    { id: 'extraction', labelKey: 'uploader.stepExtraction', optional: false, skipped: false },
    ...steps.slice(1),
  ];
}

export function withoutExtractionStep(steps: StepConfig[]): StepConfig[] {
  return steps.filter((s) => s.id !== 'extraction');
}

/** Mark the optional steps the user skipped last time as skipped again. */
export function applySkippedSteps(steps: StepConfig[], skippedIds: string[]): StepConfig[] {
  return steps.map((s) => (s.optional && skippedIds.includes(s.id) ? { ...s, skipped: true } : s));
}

/** Whether a selected file is a large video that should get the audio-extraction step. */
export function shouldOfferExtraction(
  file: File,
  settings: AudioExtractionSettings | null
): boolean {
  const isVideo = file.type.startsWith('video/');
  const thresholdMb = settings?.extraction_threshold_mb || 100;
  const isLargeFile = file.size > thresholdMb * 1024 * 1024;
  return settings?.auto_extract_enabled !== false && isVideo && isLargeFile;
}

export interface TranscriptionChoices {
  speakerCountApplies: boolean;
  transcriptionSettings: TranscriptionSettings | null;
  transcriptionSystemDefaults: TranscriptionSystemDefaults | null;
  minSpeakers: number | null;
  maxSpeakers: number | null;
  numSpeakers: number | null;
  selectedWhisperModel: string | null;
  skipSummary: boolean;
  voiceprintConsent: boolean;
  voiceprintsOn: boolean;
}

/**
 * Everything the wizard collected about HOW to transcribe, as one object. Read it when the
 * user confirms, not later: audio extraction finishes in the background after the wizard
 * has reset its own state, so a lazy read there would see the defaults.
 */
export function buildTranscriptionParams(c: TranscriptionChoices): UploadTranscriptionParams {
  // Nothing to send for a range the server would discard (see speakerCountApplies).
  const range = c.speakerCountApplies
    ? speakerSubmitRange(
        c.transcriptionSettings,
        c.transcriptionSystemDefaults,
        { minSpeakers: c.minSpeakers, maxSpeakers: c.maxSpeakers },
        c.numSpeakers
      )
    : { minSpeakers: null, maxSpeakers: null };
  return {
    minSpeakers: range.minSpeakers,
    maxSpeakers: range.maxSpeakers,
    numSpeakers: c.speakerCountApplies ? c.numSpeakers : null,
    whisperModel: c.selectedWhisperModel,
    skipSummary: c.skipSummary,
    voiceprintConsent:
      c.voiceprintConsent && c.voiceprintsOn && c.speakerCountApplies ? 'granted' : null,
  };
}
