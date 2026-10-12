/**
 * The read-only "Processing pipeline" view (issue #1227).
 *
 * Mirrors `backend/app/schemas/pipeline.py`. The server resolves how the caller's next
 * recording would be processed (`services/pipeline_plan`); nothing here re-derives it. Every
 * id is stable and doubles as an i18n key suffix; `message` is the fixed English sentence for
 * an issue code and is only a fallback for a code this build has no translation for.
 */

import axiosInstance from '../axios';

export type PipelineStepName = 'transcription' | 'diarization' | 'speaker_id' | 'llm';
export type PipelineSeverity = 'error' | 'warning' | 'info';
export type PipelineWhere = 'server' | 'cloud';
export type PipelineTiming = 'word' | 'segment' | 'none';
export type PipelineSpeakerHint = 'min' | 'max' | 'num';

export interface PipelineIssue {
  /** Stable code; i18n key `settings.pipeline.issue.<code>`. */
  code: string;
  severity: PipelineSeverity;
  step: PipelineStepName;
  message: string;
}

export interface PipelineStep {
  step: PipelineStepName;
  enabled: boolean;
  /** Null when the step does not run. */
  where: PipelineWhere | null;
  /** Stable id: local_gpu|local_cpu|cloud, asr|local|pyannote_ai|asr_config|off, ... */
  engine: string;
  execution: string | null;
  provider: string | null;
  provider_display: string | null;
  model: string | null;
  /** Transcription step only. */
  returns_speakers: boolean | null;
  /** Transcription step only. */
  timing: PipelineTiming | null;
  /** Speaker detection by another transcription provider, billed as a transcription. */
  adapter_billing: boolean;
  speaker_hints: PipelineSpeakerHint[];
  audio_leaves_server: boolean;
  /** Settings section id that changes this step. */
  settings_section: string;
}

export interface PipelineView {
  /** Always four, in processing order. */
  steps: PipelineStep[];
  issues: PipelineIssue[];
  deployment_mode: 'full' | 'lite';
  diarization_source_locked: boolean;
}

/** How the caller's next recording would be processed, and what stands in its way. */
export async function getPipelineView(): Promise<PipelineView> {
  const response = await axiosInstance.get('/user-settings/pipeline');
  // A reply without the steps cannot be drawn; fail here once instead of in every consumer.
  if (!Array.isArray(response.data?.steps) || !Array.isArray(response.data?.issues)) {
    throw new Error('Malformed processing pipeline response');
  }
  return response.data;
}
