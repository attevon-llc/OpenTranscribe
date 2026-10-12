/**
 * Copy keys for the Processing pipeline view, as explicit literals (issue #1227).
 *
 * Same contract as `$lib/i18n/keyMaps` (`keyMaps.test.ts` proves every key resolves in every
 * locale and that nothing builds a key by interpolation), kept in its own module so the
 * settings-only strings stay out of the root layout's static import closure.
 */

/**
 * One sentence per stable issue code the resolver emits
 * (`services/pipeline_plan.py` ISSUE_MESSAGES). A code outside this map has no copy here.
 */
export const PIPELINE_ISSUE_KEYS = {
  pyannote_key_missing: 'settings.pipeline.issue.pyannote_key_missing',
  diarizer_config_missing: 'settings.pipeline.issue.diarizer_config_missing',
  diarizer_config_no_speakers: 'settings.pipeline.issue.diarizer_config_no_speakers',
  local_asr_unavailable: 'settings.pipeline.issue.local_asr_unavailable',
  local_diarization_unavailable: 'settings.pipeline.issue.local_diarization_unavailable',
  automatic_needs_speaker_provider: 'settings.pipeline.issue.automatic_needs_speaker_provider',
  asr_no_timing_for_speakers: 'settings.pipeline.issue.asr_no_timing_for_speakers',
  automatic_uses_server: 'settings.pipeline.issue.automatic_uses_server',
  segment_timing_only: 'settings.pipeline.issue.segment_timing_only',
  speaker_hints_ignored: 'settings.pipeline.issue.speaker_hints_ignored',
  diarizer_double_billing: 'settings.pipeline.issue.diarizer_double_billing',
  diarizer_same_as_transcription: 'settings.pipeline.issue.diarizer_same_as_transcription',
  source_locked: 'settings.pipeline.issue.source_locked',
} as const;

/** Step engine ids that have a sentence of their own (the rest print the provider name). */
export const PIPELINE_ENGINE_KEYS = {
  local_gpu: 'settings.pipeline.engine.local_gpu',
  local_cpu: 'settings.pipeline.engine.local_cpu',
  asr: 'settings.pipeline.engine.asr',
  native: 'settings.pipeline.engine.native',
  pyannote: 'settings.pipeline.engine.pyannote',
  local: 'settings.pipeline.engine.local',
  inline_native: 'settings.pipeline.engine.inline_native',
  embedding_stage: 'settings.pipeline.engine.embedding_stage',
} as const;

/** Timestamp granularity badge of a transcription model. */
export const TIMING_KEYS = {
  word: 'settings.asrProvider.timing.word',
  segment: 'settings.asrProvider.timing.segment',
  none: 'settings.asrProvider.timing.none',
} as const;

/** Screen-reader label of an issue's severity (the colour is not the only cue). */
export const PIPELINE_SEVERITY_KEYS = {
  error: 'settings.pipeline.severity.error',
  warning: 'settings.pipeline.severity.warning',
  info: 'settings.pipeline.severity.info',
} as const;
