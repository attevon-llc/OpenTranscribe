/**
 * Pure rules behind the reprocess wizard (`SelectiveReprocessModal.svelte`): which pipeline
 * stages a selection of files can re-run, how picking one stage affects another, which steps
 * the wizard shows, whether the form may be submitted, and the request body it sends.
 *
 * The stage ids are the server's `ReprocessRequest.stages` literals; the server stays the
 * authority on what it accepts.
 */
import type { Translate } from '$lib/i18n';
import {
  speakerSubmitRange,
  type TranscriptionSettings,
  type TranscriptionSystemDefaults,
} from '$lib/api/transcriptionSettings';

export interface StageDefinition {
  id: string;
  labelKey: string;
  descKey: string;
  disabled: boolean;
  disabledReason: string;
}

export interface StageGroup {
  /** i18n key of the group heading. */
  labelKey: string;
  stages: StageDefinition[];
}

/** The slice of a file the wizard looks at; gallery rows use the list schema, the detail page the full one. */
export interface ReprocessFile {
  status?: string;
  transcript_segments?: unknown[];
  total_segments?: number;
  diarization_disabled?: boolean;
}

/**
 * Whether a re-run that needs a transcript can be offered. In bulk mode the gallery rows use
 * the list schema (no `transcript_segments` / `total_segments`), and a completed file always has
 * a transcript, so status is the indicator; a mixed selection (completed + error) enables the
 * stages if ANY file qualifies.
 */
export function selectionHasTranscript(
  file: ReprocessFile | null,
  bulkMode: boolean,
  bulkFiles: ReprocessFile[]
): boolean {
  return bulkMode
    ? bulkFiles.some((f) => f.status === 'completed')
    : file?.status === 'completed' &&
        ((file?.transcript_segments?.length ?? 0) > 0 || (file?.total_segments ?? 0) > 0);
}

/** Whether any selected file was uploaded with speaker detection off (the wizard says so). */
export function selectionHasDiarizationDisabled(
  file: ReprocessFile | null,
  bulkMode: boolean,
  bulkFiles: ReprocessFile[]
): boolean {
  return bulkMode
    ? bulkFiles.some((f) => f.diarization_disabled)
    : file?.diarization_disabled === true;
}

export interface StageAvailability {
  hasTranscript: boolean;
  /** Re-diarize needs word timestamps; a completed file has them whenever it has a transcript. */
  hasWordTimestamps: boolean;
  llmAvailable: boolean;
}

/**
 * The three groups of stages, each stage enabled or disabled (with the reason shown) for this
 * selection. Every i18n key is a literal on purpose: a key built from a template string is
 * invisible to the key-usage checks (#970).
 */
export function buildStageGroups(avail: StageAvailability, t: Translate): StageGroup[] {
  const needsTranscript = (id: string, labelKey: string, descKey: string): StageDefinition => ({
    id,
    labelKey,
    descKey,
    disabled: !avail.hasTranscript,
    disabledReason: t('reprocess.transcriptRequired'),
  });
  const needsLlm = (id: string, labelKey: string, descKey: string): StageDefinition => ({
    id,
    labelKey,
    descKey,
    disabled: !avail.hasTranscript || !avail.llmAvailable,
    disabledReason: !avail.hasTranscript
      ? t('reprocess.transcriptRequired')
      : t('reprocess.llmNotAvailable'),
  });
  return [
    {
      labelKey: 'reprocess.coreProcessing',
      stages: [
        {
          id: 'transcription',
          labelKey: 'reprocess.stageTranscription',
          descKey: 'reprocess.stageTranscriptionDesc',
          disabled: false,
          disabledReason: '',
        },
        {
          id: 'rediarize',
          labelKey: 'reprocess.stageRediarize',
          descKey: 'reprocess.stageRediarizeDesc',
          disabled: !avail.hasWordTimestamps,
          disabledReason: t('reprocess.stageRediarizeDisabled'),
        },
      ],
    },
    {
      labelKey: 'reprocess.searchDiscovery',
      stages: [
        needsTranscript(
          'search_indexing',
          'reprocess.stageSearchIndexing',
          'reprocess.stageSearchIndexingDesc'
        ),
        needsTranscript('analytics', 'reprocess.stageAnalytics', 'reprocess.stageAnalyticsDesc'),
      ],
    },
    {
      labelKey: 'reprocess.aiFeatures',
      stages: [
        needsLlm('speaker_llm', 'reprocess.stageSpeakerLLM', 'reprocess.stageSpeakerLLMDesc'),
        needsLlm(
          'summarization',
          'reprocess.stageSummarization',
          'reprocess.stageSummarizationDesc'
        ),
        needsLlm(
          'topic_extraction',
          'reprocess.stageTopicExtraction',
          'reprocess.stageTopicExtractionDesc'
        ),
      ],
    },
  ];
}

/**
 * Toggle one stage. Full transcription subsumes re-diarize (it re-detects speakers anyway), so
 * turning either on turns the other off.
 */
export function toggleStage(selected: Set<string>, stageId: string): Set<string> {
  const next = new Set(selected);
  if (next.has(stageId)) {
    next.delete(stageId);
  } else {
    next.add(stageId);
    if (stageId === 'transcription') next.delete('rediarize');
    else if (stageId === 'rediarize') next.delete('transcription');
  }
  return next;
}

/** Every enabled stage except re-diarize (transcription already subsumes it). */
export function selectableStageIds(stages: StageDefinition[]): string[] {
  return stages.filter((s) => !s.disabled && s.id !== 'rediarize').map((s) => s.id);
}

/**
 * The speaker range applies to a re-diarize whichever provider transcribed the file, but to a
 * transcription re-run only with the local engine: a cloud ASR provider returns its own speakers.
 */
export function showsSpeakerSettings(selected: Set<string>, isCloudASR: boolean): boolean {
  return selected.has('rediarize') || (selected.has('transcription') && !isCloudASR);
}

/** The settings step carries the speaker range and the model choice. */
export function requiresSettingsStep(selected: Set<string>, showSpeakerSettings: boolean): boolean {
  return showSpeakerSettings || selected.has('transcription');
}

export function wizardStepLabels(withSettingsStep: boolean): string[] {
  return withSettingsStep
    ? ['reprocess.stepStages', 'reprocess.stepSettings', 'reprocess.stepReview']
    : ['reprocess.stepStages', 'reprocess.stepReview'];
}

/** A minimum above the maximum can never be satisfied. */
export function speakerRangeConflict(min: number | null, max: number | null): boolean {
  return min !== null && max !== null && min > max;
}

export function isReprocessValid(
  selected: Set<string>,
  showSpeakerSettings: boolean,
  min: number | null,
  max: number | null
): boolean {
  return selected.size > 0 && (!showSpeakerSettings || !speakerRangeConflict(min, max));
}

export interface ReprocessRequestInput {
  stages: string[];
  showSpeakerSettings: boolean;
  settings: TranscriptionSettings | null;
  systemDefaults: TranscriptionSystemDefaults | null;
  minSpeakers: number | null;
  maxSpeakers: number | null;
  numSpeakers: number | null;
  /** The per-file Whisper model, honoured only where the deployment lets users choose. */
  model: string | null;
  modelChoiceEnabled: boolean;
}

/** The request body shared by the single-file and bulk endpoints. */
export function buildReprocessBody(input: ReprocessRequestInput): Record<string, unknown> {
  const body: Record<string, unknown> = { stages: input.stages };

  if (input.showSpeakerSettings) {
    const range = speakerSubmitRange(
      input.settings,
      input.systemDefaults,
      { minSpeakers: input.minSpeakers, maxSpeakers: input.maxSpeakers },
      input.numSpeakers
    );
    if (range.minSpeakers !== null) body.min_speakers = range.minSpeakers;
    if (range.maxSpeakers !== null) body.max_speakers = range.maxSpeakers;
    if (input.numSpeakers !== null) body.num_speakers = input.numSpeakers;
  }

  if (input.model && input.modelChoiceEnabled) {
    body.whisper_model = input.model;
  }
  return body;
}

/** How many files of a bulk reprocess started and how many did not. */
export function countBulkResults(results: { success: boolean }[]): {
  successful: number;
  failed: number;
} {
  const successful = results.filter((r) => r.success).length;
  return { successful, failed: results.length - successful };
}
