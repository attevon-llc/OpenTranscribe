/**
 * `PipelineView` builders for tests: the states the Processing pipeline page and the speaker
 * detection card must render. Shapes follow `backend/app/schemas/pipeline.py`; the values are
 * what `GET /user-settings/pipeline` returns for the matching setup, not invented ones.
 */
import type { PipelineIssue, PipelineStep, PipelineView } from '$lib/api/pipeline';

const BASE_STEP = {
  enabled: true,
  execution: null,
  provider: null,
  provider_display: null,
  model: null,
  returns_speakers: null,
  timing: null,
  adapter_billing: false,
  audio_leaves_server: false,
};

export function step(overrides: Partial<PipelineStep> & Pick<PipelineStep, 'step'>): PipelineStep {
  const sections = {
    transcription: 'asr-provider',
    diarization: 'speaker-identification',
    speaker_id: 'speaker-identification',
    llm: 'llm-provider',
  } as const;
  return {
    ...BASE_STEP,
    speaker_hints: [],
    where: 'server',
    engine: 'off',
    settings_section: sections[overrides.step],
    ...overrides,
  };
}

export function issue(
  code: string,
  severity: PipelineIssue['severity'],
  stepName: PipelineIssue['step'] = 'diarization'
): PipelineIssue {
  return { code, severity, step: stepName, message: `fixed sentence for ${code}` };
}

/** Full image, local GPU Whisper, speakers detected on this server, an LLM on this network. */
export function fullLocalView(overrides: Partial<PipelineView> = {}): PipelineView {
  return {
    steps: [
      step({
        step: 'transcription',
        engine: 'local_gpu',
        provider: 'local',
        provider_display: 'Local (GPU)',
        model: 'large-v3-turbo',
        returns_speakers: true,
        timing: 'word',
      }),
      step({
        step: 'diarization',
        engine: 'local',
        execution: 'inline_local',
        provider: 'native',
        provider_display: 'diar-native engine',
        speaker_hints: [],
      }),
      step({
        step: 'speaker_id',
        engine: 'inline_native',
        provider: 'native',
        provider_display: 'diar-native engine',
      }),
      step({
        step: 'llm',
        engine: 'llm',
        provider: 'vllm',
        provider_display: 'vllm',
        model: 'qwen',
      }),
    ],
    issues: [issue('speaker_hints_ignored', 'warning')],
    deployment_mode: 'full',
    diarization_source_locked: false,
    ...overrides,
  };
}

/** Lite: cloud transcription that returns speakers, pyannote.ai off, voiceprints on this server. */
export function liteCloudView(overrides: Partial<PipelineView> = {}): PipelineView {
  return {
    steps: [
      step({
        step: 'transcription',
        where: 'cloud',
        engine: 'cloud',
        provider: 'deepgram',
        provider_display: 'Deepgram',
        model: 'nova-3',
        returns_speakers: true,
        timing: 'word',
        audio_leaves_server: true,
      }),
      step({
        step: 'diarization',
        where: 'cloud',
        engine: 'asr',
        execution: 'inline_asr',
        provider: 'deepgram',
        provider_display: 'Deepgram',
        audio_leaves_server: true,
      }),
      step({
        step: 'speaker_id',
        engine: 'embedding_stage',
        provider: 'native',
        provider_display: 'diar-native engine',
      }),
      step({ step: 'llm', where: null, enabled: false, engine: 'off' }),
    ],
    issues: [],
    deployment_mode: 'lite',
    diarization_source_locked: false,
    ...overrides,
  };
}

/** Local GPU Whisper with the speaker labels bought from a second cloud vendor (two vendors). */
export function twoVendorView(): PipelineView {
  const view = fullLocalView({ issues: [issue('diarizer_double_billing', 'warning')] });
  view.steps[0] = step({
    step: 'transcription',
    where: 'cloud',
    engine: 'cloud',
    provider: 'openai',
    provider_display: 'OpenAI',
    model: 'whisper-1',
    returns_speakers: false,
    timing: 'segment',
    audio_leaves_server: true,
  });
  view.steps[1] = step({
    step: 'diarization',
    where: 'cloud',
    engine: 'asr_config',
    execution: 'parallel_cloud',
    provider: 'deepgram',
    provider_display: 'Deepgram',
    adapter_billing: true,
    audio_leaves_server: true,
  });
  return view;
}
