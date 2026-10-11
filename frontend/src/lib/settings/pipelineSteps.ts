/**
 * View models for the Processing pipeline page (issue #1227): `PipelineView` in, display
 * fields out. Nothing is decided here; the server already said where each step runs, what
 * it returns and what is wrong. This only picks the copy key for each stable id.
 */
import type {
  PipelineIssue,
  PipelineSeverity,
  PipelineStep,
  PipelineStepName,
  PipelineTiming,
  PipelineView,
  PipelineWhere,
} from '$lib/api/pipeline';
import { PIPELINE_ENGINE_KEYS } from '$lib/i18n/keyMaps';
import type { SettingsSection } from '$stores/settingsModalStore';
import { fixTargets, isPipelineIssueCode, issueKey } from './pipelineIssues';

export interface IssueViewModel {
  code: string;
  severity: PipelineSeverity;
  /** Copy for a code this build knows; null otherwise. */
  messageKey: string | null;
  /** The server's fixed sentence, only for a code with no copy. */
  fallbackText: string | null;
  fixSections: SettingsSection[];
}

export interface StepViewModel {
  step: PipelineStepName;
  titleKey: string;
  purposeKey: string;
  enabled: boolean;
  where: PipelineWhere | null;
  /** Vendor shown on the cloud chip; null for this server. */
  vendor: string | null;
  /** Translated engine description, or null when the provider name is the description. */
  engineLabelKey: string | null;
  engineText: string | null;
  model: string | null;
  /** Transcription step: does the model return speaker labels? Null on other steps. */
  returnsSpeakers: boolean | null;
  timing: PipelineTiming | null;
  adapterBilling: boolean;
  hintsKey: string | null;
  issues: IssueViewModel[];
  /** Where the "Change" link goes. */
  settingsSection: SettingsSection;
}

export interface PipelineViewModel {
  steps: StepViewModel[];
  liteBanner: boolean;
}

const STEP_KEYS: Record<PipelineStepName, { title: string; purpose: string }> = {
  transcription: {
    title: 'settings.pipeline.step.transcription',
    purpose: 'settings.pipeline.step.transcriptionPurpose',
  },
  diarization: {
    title: 'settings.pipeline.step.diarization',
    purpose: 'settings.pipeline.step.diarizationPurpose',
  },
  speaker_id: {
    title: 'settings.pipeline.step.speakerId',
    purpose: 'settings.pipeline.step.speakerIdPurpose',
  },
  llm: { title: 'settings.pipeline.step.llm', purpose: 'settings.pipeline.step.llmPurpose' },
};

const SEVERITY_ORDER: Record<PipelineSeverity, number> = { error: 0, warning: 1, info: 2 };

const SELF_DESCRIBING_ENGINES: ReadonlySet<string> = new Set([
  'local_gpu',
  'local_cpu',
  'asr',
  'inline_native',
  'embedding_stage',
]);

/** A sentence for the engine ids that have one; the rest print the provider name. */
function engineLabelKey(step: PipelineStep): string | null {
  if (!step.enabled) return null;
  if (SELF_DESCRIBING_ENGINES.has(step.engine)) {
    return PIPELINE_ENGINE_KEYS[step.engine as keyof typeof PIPELINE_ENGINE_KEYS];
  }
  if (step.engine === 'local') {
    // The local engine is the administrator's choice, shown by name.
    return step.provider === 'native' || step.provider === 'pyannote'
      ? PIPELINE_ENGINE_KEYS[step.provider]
      : PIPELINE_ENGINE_KEYS.local;
  }
  return null;
}

function engineText(step: PipelineStep): string | null {
  if (!step.enabled || engineLabelKey(step) !== null) return null;
  return step.provider_display ?? step.provider;
}

function hintsKey(step: PipelineStep): string | null {
  if (step.step !== 'diarization' || !step.enabled) return null;
  return step.speaker_hints.length > 0
    ? 'settings.pipeline.hintsUsed'
    : 'settings.pipeline.hintsIgnored';
}

function issueViewModel(issue: PipelineIssue): IssueViewModel {
  const messageKey = isPipelineIssueCode(issue.code) ? issueKey(issue.code) : null;
  return {
    code: issue.code,
    severity: issue.severity,
    messageKey,
    fallbackText: messageKey === null ? issue.message : null,
    fixSections: fixTargets(issue),
  };
}

function stepViewModel(step: PipelineStep, issues: PipelineIssue[]): StepViewModel {
  const keys = STEP_KEYS[step.step];
  return {
    step: step.step,
    titleKey: keys.title,
    purposeKey: keys.purpose,
    enabled: step.enabled,
    where: step.enabled ? step.where : null,
    vendor: step.enabled && step.where === 'cloud' ? step.provider_display ?? step.provider : null,
    engineLabelKey: engineLabelKey(step),
    engineText: engineText(step),
    model: step.enabled ? step.model : null,
    returnsSpeakers: step.returns_speakers,
    timing: step.timing,
    adapterBilling: step.adapter_billing,
    hintsKey: hintsKey(step),
    issues: issues
      .filter((issue) => issue.step === step.step)
      .map(issueViewModel)
      .sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]),
    settingsSection: step.settings_section as SettingsSection,
  };
}

export function buildPipelineSteps(view: PipelineView): PipelineViewModel {
  return {
    steps: view.steps.map((step) => stepViewModel(step, view.issues)),
    liteBanner: view.deployment_mode === 'lite',
  };
}

/** Distinct vendors that receive the recording's audio, in processing order. */
export function audioVendors(view: PipelineView): string[] {
  const vendors: string[] = [];
  for (const step of view.steps) {
    if (!step.enabled || !step.audio_leaves_server) continue;
    const name = step.provider_display ?? step.provider;
    if (name && !vendors.includes(name)) vendors.push(name);
  }
  return vendors;
}
