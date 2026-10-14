/**
 * Display rules for the speaker detection card and the ASR provider picker (issue #1227):
 * which sources are offered, and the notices a CHOICE earns before it is saved.
 *
 * These are previews of an unsaved selection and only that. The server is the authority:
 * `PUT /user-settings/transcription` re-resolves the pipeline plan and answers 409 with a
 * stable `detail.code` for a choice that cannot run, and `GET /user-settings/pipeline` reports
 * the saved state. The rules below mirror `services/pipeline_plan.py` for the notice shown
 * while the user is still choosing.
 */
import type {
  PipelineSeverity,
  PipelineTiming,
  PipelineView,
  PipelineWhere,
} from '$lib/api/pipeline';
import type { DiarizationSource } from '$lib/api/transcriptionSettings';
import type { DiarizerChoice } from './asrCapabilities';

type Mode = 'full' | 'lite';

export interface SourceOption {
  value: DiarizationSource;
  labelKey: string;
  descKey: string;
  disabled: boolean;
}

const PREFIX = 'settings.speakerIdentification.source';

function option(value: DiarizationSource, suffix: string, disabled = false): SourceOption {
  return { value, labelKey: `${PREFIX}.${suffix}`, descKey: `${PREFIX}.${suffix}Desc`, disabled };
}

/**
 * The sources the select offers. Lite has no local speaker detection, so the option is not
 * rendered, except as a disabled entry when it is the stored value (a select must be able to
 * show what is stored, and the plan reports why it cannot run).
 */
export function speakerSourceOptions(args: {
  mode: Mode;
  current: DiarizationSource;
  asrConfigEnabled: boolean;
}): SourceOption[] {
  const options = [option('provider', 'provider')];
  if (args.mode !== 'lite') options.push(option('local', 'local'));
  else if (args.current === 'local') {
    options.push({
      ...option('local', 'local', true),
      labelKey: `${PREFIX}.localUnavailableOption`,
    });
  }
  options.push(option('pyannote', 'pyannote'));
  if (args.asrConfigEnabled || args.current === 'asr_config') {
    options.push(option('asr_config', 'asrConfig'));
  }
  options.push(option('off', 'off'));
  return options;
}

export interface TranscriptionFacts {
  returnsSpeakers: boolean;
  timing: PipelineTiming;
  /** Catalog id and model of what transcribes, to tell "the same provider" from another. */
  provider?: string | null;
  model?: string | null;
}

/** What the transcription model returns, from the saved pipeline; null until it has loaded. */
export function transcriptionFacts(view: PipelineView | null): TranscriptionFacts | null {
  const step = view?.steps.find((s) => s.step === 'transcription');
  if (!step || step.returns_speakers === null || step.timing === null) return null;
  return {
    returnsSpeakers: step.returns_speakers,
    timing: step.timing,
    provider: step.provider,
    model: step.model,
  };
}

export interface SelectionNotice {
  code: string;
  severity: PipelineSeverity;
}

/**
 * Notices for choosing `source` with a transcription model that returns `facts`, in the order
 * to show them (blocking first). Empty when the combination is fine or speaker detection is off.
 */
export function selectionNotices(
  source: DiarizationSource,
  facts: TranscriptionFacts | null,
  mode: Mode,
  chosen?: Pick<DiarizerChoice, 'provider' | 'modelName'>
): SelectionNotice[] {
  if (source === 'off') return [];
  const notices: SelectionNotice[] = [];
  if (facts?.timing === 'none') {
    return [{ code: 'asr_no_timing_for_speakers', severity: 'error' }];
  }
  if (source === 'local' && mode === 'lite') {
    notices.push({ code: 'local_diarization_unavailable', severity: 'error' });
  }
  if (source === 'provider' && facts && !facts.returnsSpeakers) {
    notices.push(
      mode === 'lite'
        ? { code: 'automatic_needs_speaker_provider', severity: 'error' }
        : { code: 'automatic_uses_server', severity: 'info' }
    );
  }
  if (source === 'asr_config' && chosen) {
    // Another saved provider: a full transcription is billed for its speakers alone, unless it
    // is the very provider and model that transcribes, whose own speakers are then used.
    const same = chosen.provider === facts?.provider && chosen.modelName === facts?.model;
    notices.push(
      same
        ? { code: 'diarizer_same_as_transcription', severity: 'info' }
        : { code: 'diarizer_double_billing', severity: 'warning' }
    );
  }
  // The model's own speaker labels need no merge, so its coarse timing costs nothing there.
  const labelsFromModel = source === 'provider' && facts?.returnsSpeakers === true;
  if (facts?.timing === 'segment' && !labelsFromModel) {
    notices.push({ code: 'segment_timing_only', severity: 'warning' });
  }
  return notices;
}

export interface SelectionWhere {
  where: PipelineWhere;
  /** The vendor that receives the audio; null for this server. */
  vendor: string | null;
}

/** Where a chosen source would detect speakers; null when it would not run. */
export function selectionWhere(
  source: DiarizationSource,
  view: PipelineView | null,
  chosen: DiarizerChoice | undefined
): SelectionWhere | null {
  switch (source) {
    case 'off':
      return null;
    case 'local':
      return view?.deployment_mode === 'lite' ? null : { where: 'server', vendor: null };
    case 'pyannote':
      return { where: 'cloud', vendor: 'pyannote.ai' };
    case 'asr_config':
      return chosen ? { where: 'cloud', vendor: chosen.providerLabel } : null;
    default: {
      const transcription = view?.steps.find((s) => s.step === 'transcription');
      if (!transcription) return null;
      if (transcription.returns_speakers) {
        return transcription.where === 'cloud'
          ? { where: 'cloud', vendor: transcription.provider_display ?? transcription.provider }
          : { where: 'server', vendor: null };
      }
      return view?.deployment_mode === 'lite' ? null : { where: 'server', vendor: null };
    }
  }
}

/**
 * Copy key for the cost line under the picker, or null when there is nothing to add. Only the
 * one case that needs saying here: Automatic with a model that returns its own speakers costs
 * nothing extra. The extra-billing cases are issues (see `selectionNotices`).
 */
export function billingNoteKey(
  source: DiarizationSource,
  view: PipelineView | null
): string | null {
  if (source !== 'provider') return null;
  const transcription = view?.steps.find((s) => s.step === 'transcription');
  if (transcription?.where !== 'cloud' || transcription.returns_speakers !== true) return null;
  return 'settings.speakerIdentification.billing.included';
}
