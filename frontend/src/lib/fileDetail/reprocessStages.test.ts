import { describe, it, expect } from 'vitest';
import {
  DEFAULT_TRANSCRIPTION_SETTINGS,
  type TranscriptionSettings,
  type TranscriptionSystemDefaults,
} from '$lib/api/transcriptionSettings';
import {
  buildReprocessBody,
  languageChoices,
  buildStageGroups,
  countBulkResults,
  isReprocessValid,
  requiresSettingsStep,
  selectableStageIds,
  selectionHasDiarizationDisabled,
  selectionHasTranscript,
  showsSpeakerSettings,
  speakerRangeConflict,
  toggleStage,
  wizardStepLabels,
} from './reprocessStages';

const t = (key: string) => key;
const set = (...ids: string[]) => new Set(ids);

describe('selectionHasTranscript', () => {
  const done = { status: 'completed', transcript_segments: [{}] };
  it('needs a completed file with segments, single mode', () => {
    expect(selectionHasTranscript(done, false, [])).toBe(true);
    expect(selectionHasTranscript({ status: 'completed', total_segments: 3 }, false, [])).toBe(
      true
    );
    expect(selectionHasTranscript({ status: 'completed' }, false, [])).toBe(false);
    expect(selectionHasTranscript({ ...done, status: 'processing' }, false, [])).toBe(false);
    expect(selectionHasTranscript(null, false, [])).toBe(false);
  });
  it('in bulk mode any completed file will do, whatever the row schema carries', () => {
    expect(selectionHasTranscript(null, true, [{ status: 'error' }, { status: 'completed' }])).toBe(
      true
    );
    expect(selectionHasTranscript(done, true, [{ status: 'error' }])).toBe(false);
  });
});

describe('selectionHasDiarizationDisabled', () => {
  it('reads the file, or any file in bulk mode', () => {
    expect(selectionHasDiarizationDisabled({ diarization_disabled: true }, false, [])).toBe(true);
    expect(selectionHasDiarizationDisabled({}, false, [])).toBe(false);
    expect(selectionHasDiarizationDisabled(null, true, [{}, { diarization_disabled: true }])).toBe(
      true
    );
  });
});

describe('buildStageGroups', () => {
  const ids = (g: ReturnType<typeof buildStageGroups>) => g.map((x) => x.stages.map((s) => s.id));
  const disabled = (g: ReturnType<typeof buildStageGroups>) =>
    g.flatMap((x) => x.stages.map((s) => s.disabled));
  it('lists core, search and AI stages in the order the server pipeline runs them', () => {
    const groups = buildStageGroups(
      { hasTranscript: true, hasWordTimestamps: true, llmAvailable: true },
      t
    );
    expect(groups.map((g) => g.labelKey)).toEqual([
      'reprocess.coreProcessing',
      'reprocess.searchDiscovery',
      'reprocess.aiFeatures',
    ]);
    expect(ids(groups)).toEqual([
      ['transcription', 'rediarize'],
      ['search_indexing', 'analytics'],
      ['speaker_llm', 'summarization', 'topic_extraction'],
    ]);
    expect(disabled(groups)).toEqual([false, false, false, false, false, false, false]);
  });
  it('always allows full transcription, even with nothing to build on', () => {
    const groups = buildStageGroups(
      { hasTranscript: false, hasWordTimestamps: false, llmAvailable: true },
      t
    );
    expect(disabled(groups)).toEqual([false, true, true, true, true, true, true]);
  });
  it('gives the AI stages the transcript reason before the LLM reason', () => {
    const noLlm = buildStageGroups(
      { hasTranscript: true, hasWordTimestamps: true, llmAvailable: false },
      t
    );
    expect(noLlm[2].stages[0].disabledReason).toBe('reprocess.llmNotAvailable');
    const noText = buildStageGroups(
      { hasTranscript: false, hasWordTimestamps: false, llmAvailable: false },
      t
    );
    expect(noText[2].stages[0].disabledReason).toBe('reprocess.transcriptRequired');
  });
  it('keys re-diarize off word timestamps, not the transcript', () => {
    const groups = buildStageGroups(
      { hasTranscript: true, hasWordTimestamps: false, llmAvailable: false },
      t
    );
    expect(groups[0].stages[1].disabled).toBe(true);
    expect(groups[1].stages[0].disabled).toBe(false);
  });
});

describe('toggleStage', () => {
  it('adds and removes without touching the input set', () => {
    const start = set('analytics');
    const on = toggleStage(start, 'search_indexing');
    expect([...on]).toEqual(['analytics', 'search_indexing']);
    expect([...start]).toEqual(['analytics']);
    expect([...toggleStage(on, 'analytics')]).toEqual(['search_indexing']);
  });
  it('makes transcription and re-diarize exclusive in both directions', () => {
    expect([...toggleStage(set('rediarize', 'analytics'), 'transcription')]).toEqual([
      'analytics',
      'transcription',
    ]);
    expect([...toggleStage(set('transcription'), 'rediarize')]).toEqual(['rediarize']);
  });
  it('does not drop the other one when turning a stage OFF', () => {
    expect([...toggleStage(set('transcription', 'analytics'), 'analytics')]).toEqual([
      'transcription',
    ]);
  });
});

describe('selectableStageIds', () => {
  it('skips disabled stages and re-diarize', () => {
    const stages = [
      { id: 'transcription', disabled: false },
      { id: 'rediarize', disabled: false },
      { id: 'analytics', disabled: true },
      { id: 'summarization', disabled: false },
    ].map((s) => ({ ...s, labelKey: '', descKey: '', disabledReason: '' }));
    expect(selectableStageIds(stages)).toEqual(['transcription', 'summarization']);
  });
});

describe('speaker settings and steps', () => {
  it('shows speaker settings for re-diarize always, for transcription only on the local engine', () => {
    expect(showsSpeakerSettings(set('rediarize'), true)).toBe(true);
    expect(showsSpeakerSettings(set('transcription'), false)).toBe(true);
    expect(showsSpeakerSettings(set('transcription'), true)).toBe(false);
    expect(showsSpeakerSettings(set('analytics'), false)).toBe(false);
  });
  it('adds a settings step for transcription even on a cloud engine (the model choice)', () => {
    expect(requiresSettingsStep(set('transcription'), false)).toBe(true);
    expect(requiresSettingsStep(set('rediarize'), true)).toBe(true);
    expect(requiresSettingsStep(set('analytics'), false)).toBe(false);
    expect(wizardStepLabels(true)).toHaveLength(3);
    expect(wizardStepLabels(false)).toEqual(['reprocess.stepStages', 'reprocess.stepReview']);
  });
});

describe('validity', () => {
  it('flags only a minimum above the maximum', () => {
    expect(speakerRangeConflict(5, 2)).toBe(true);
    expect(speakerRangeConflict(2, 2)).toBe(false);
    expect(speakerRangeConflict(5, null)).toBe(false);
  });
  it('needs a stage, and a sane range only where the range applies', () => {
    expect(isReprocessValid(set(), false, null, null)).toBe(false);
    expect(isReprocessValid(set('analytics'), false, 9, 1)).toBe(true);
    expect(isReprocessValid(set('rediarize'), true, 9, 1)).toBe(false);
    expect(isReprocessValid(set('rediarize'), true, 1, 9)).toBe(true);
  });
});

describe('buildReprocessBody', () => {
  const base = {
    stages: ['rediarize'],
    showSpeakerSettings: true,
    settings: null,
    systemDefaults: null,
    minSpeakers: null,
    maxSpeakers: null,
    numSpeakers: null,
    model: null,
    modelChoiceEnabled: true,
  };
  it('sends only the stages when there are no speaker settings or model', () => {
    expect(buildReprocessBody({ ...base, showSpeakerSettings: false, minSpeakers: 2 })).toEqual({
      stages: ['rediarize'],
    });
  });
  it('sends a typed range and a fixed count', () => {
    expect(buildReprocessBody({ ...base, minSpeakers: 2, maxSpeakers: 4 })).toEqual({
      stages: ['rediarize'],
      min_speakers: 2,
      max_speakers: 4,
    });
    expect(buildReprocessBody({ ...base, numSpeakers: 3 })).toEqual({
      stages: ['rediarize'],
      num_speakers: 3,
    });
  });
  it('fills blanks with the system range under "use system defaults", except for a fixed count', () => {
    const settings = {
      ...DEFAULT_TRANSCRIPTION_SETTINGS,
      speaker_prompt_behavior: 'use_defaults',
    } as TranscriptionSettings;
    const systemDefaults = { min_speakers: 1, max_speakers: 20 } as TranscriptionSystemDefaults;
    expect(buildReprocessBody({ ...base, settings, systemDefaults })).toMatchObject({
      min_speakers: 1,
      max_speakers: 20,
    });
    expect(
      buildReprocessBody({ ...base, settings, systemDefaults, numSpeakers: 2 })
    ).not.toHaveProperty('min_speakers');
  });
  it('sends the model only when one was chosen AND the deployment lets users choose', () => {
    const body = (o: object) => buildReprocessBody({ ...base, showSpeakerSettings: false, ...o });
    expect(body({ model: 'base' })).toHaveProperty('whisper_model', 'base');
    expect(body({ model: 'base', modelChoiceEnabled: false })).not.toHaveProperty('whisper_model');
    expect(body({ model: null })).not.toHaveProperty('whisper_model');
  });
});

describe('countBulkResults', () => {
  it('counts started and not started', () => {
    expect(countBulkResults([{ success: true }, { success: false }, { success: true }])).toEqual({
      successful: 2,
      failed: 1,
    });
    expect(countBulkResults([])).toEqual({ successful: 0, failed: 0 });
  });
});

describe('forced language (#1263)', () => {
  const base = {
    showSpeakerSettings: false,
    settings: null,
    systemDefaults: null,
    minSpeakers: null,
    maxSpeakers: null,
    numSpeakers: null,
    model: null,
    modelChoiceEnabled: true,
  };
  it('is sent with the transcription stage only', () => {
    expect(buildReprocessBody({ ...base, stages: ['transcription'], language: 'fr' })).toEqual({
      stages: ['transcription'],
      language: 'fr',
    });
    // The server answers 422 for a language on a run that does not transcribe.
    expect(buildReprocessBody({ ...base, stages: ['rediarize'], language: 'fr' })).toEqual({
      stages: ['rediarize'],
    });
  });
  it('is omitted when unset', () => {
    expect(buildReprocessBody({ ...base, stages: ['transcription'], language: null })).toEqual({
      stages: ['transcription'],
    });
  });
  it('lists auto-detect first and the rest by name', () => {
    expect(languageChoices({ fr: 'French', auto: 'Auto-detect', de: 'German' })).toEqual([
      ['auto', 'Auto-detect'],
      ['fr', 'French'],
      ['de', 'German'],
    ]);
    expect(languageChoices(null)).toEqual([]);
  });
});
