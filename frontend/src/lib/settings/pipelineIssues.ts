/**
 * The pipeline plan's issue codes, the copy keyed by them, and where each one is fixed.
 *
 * The server decides what is wrong and sends a stable `code` (`services/pipeline_plan.py`,
 * `ISSUE_MESSAGES`). This module only maps a code to translated copy and to the settings tab
 * that fixes it. Text the server wrote about a failure (exception text, a vendor message) is
 * never rendered: a code this build has no copy for falls back to the server's fixed sentence,
 * or to the generic message when there is none.
 */
import type { PipelineIssue } from '$lib/api/pipeline';
import type { SettingsSection } from '$stores/settingsModalStore';
import { PIPELINE_ISSUE_KEYS, POLICY_REFUSAL_KEYS } from '$lib/settings/pipelineKeys';
import { getErrorStatus } from '$lib/utils/apiError';

export const PIPELINE_ISSUE_CODES = [
  'pyannote_key_missing',
  'diarizer_config_missing',
  'diarizer_config_no_speakers',
  'local_asr_unavailable',
  'local_diarization_unavailable',
  'automatic_needs_speaker_provider',
  'asr_no_timing_for_speakers',
  'automatic_uses_server',
  'segment_timing_only',
  'speaker_hints_ignored',
  'diarizer_double_billing',
  'diarizer_same_as_transcription',
  'source_locked',
  'asr_provider_not_allowed',
  'diarizer_provider_not_allowed',
] as const;

export type PipelineIssueCode = (typeof PIPELINE_ISSUE_CODES)[number];

const KNOWN: ReadonlySet<string> = new Set(PIPELINE_ISSUE_CODES);

export function isPipelineIssueCode(code: string): code is PipelineIssueCode {
  return KNOWN.has(code);
}

/** i18n key of the sentence for an issue code this build knows. */
export function issueKey(code: PipelineIssueCode): string {
  return PIPELINE_ISSUE_KEYS[code];
}

type FixSection = 'asr-provider' | 'speaker-identification' | 'llm-provider';

// Where each code is fixed. A code caused by the transcription MODEL is fixed under
// Transcription > Provider; one caused by the speaker detection CHOICE under Speakers; where
// either would do, both are offered. `source_locked` is the administrator's, so it has none.
const FIX_SECTIONS: Record<PipelineIssueCode, FixSection[]> = {
  pyannote_key_missing: ['speaker-identification'],
  diarizer_config_missing: ['speaker-identification'],
  diarizer_config_no_speakers: ['speaker-identification'],
  local_asr_unavailable: ['asr-provider'],
  local_diarization_unavailable: ['speaker-identification'],
  automatic_needs_speaker_provider: ['speaker-identification', 'asr-provider'],
  asr_no_timing_for_speakers: ['asr-provider', 'speaker-identification'],
  automatic_uses_server: ['speaker-identification'],
  segment_timing_only: ['asr-provider'],
  speaker_hints_ignored: ['speaker-identification'],
  diarizer_double_billing: ['speaker-identification'],
  diarizer_same_as_transcription: ['speaker-identification'],
  source_locked: [],
  asr_provider_not_allowed: ['asr-provider'],
  diarizer_provider_not_allowed: ['asr-provider'],
};

const STEP_SECTION: Record<PipelineIssue['step'], FixSection> = {
  transcription: 'asr-provider',
  diarization: 'speaker-identification',
  speaker_id: 'speaker-identification',
  llm: 'llm-provider',
};

/** The settings sections that can fix an issue, best first. */
export function fixTargets(issue: Pick<PipelineIssue, 'code' | 'step'>): SettingsSection[] {
  return isPipelineIssueCode(issue.code) ? FIX_SECTIONS[issue.code] : [STEP_SECTION[issue.step]];
}

const FIX_LINK_KEYS: Record<FixSection, string> = {
  'asr-provider': 'settings.pipeline.goTo.asrProvider',
  'speaker-identification': 'settings.pipeline.goTo.speakers',
  'llm-provider': 'settings.pipeline.goTo.llm',
};

/** i18n key of the link text that opens a settings section. */
export function fixLinkKey(section: SettingsSection): string {
  return FIX_LINK_KEYS[section as FixSection] ?? FIX_LINK_KEYS['speaker-identification'];
}

interface ConflictBody {
  response?: { data?: { detail?: unknown } };
}

/** `detail.code` of a 409 from `PUT /user-settings/transcription`; null for any other failure. */
export function pipelineErrorCode(err: unknown): string | null {
  if (getErrorStatus(err) !== 409) return null;
  const detail = (err as ConflictBody)?.response?.data?.detail;
  const code = (detail as { code?: unknown } | null | undefined)?.code;
  return typeof code === 'string' ? code : null;
}

/**
 * Copy for a request the server refused with 409 `detail: {code, message}`, keyed by the code.
 * Null when the refusal carries no code this build knows, so the caller shows its generic
 * message: raw server text is never rendered.
 */
export function pipelineRefusalKey(err: unknown): string | null {
  const code = pipelineErrorCode(err);
  if (code === null) return null;
  if (isPipelineIssueCode(code)) return issueKey(code);
  return code in POLICY_REFUSAL_KEYS
    ? POLICY_REFUSAL_KEYS[code as keyof typeof POLICY_REFUSAL_KEYS]
    : null;
}

/**
 * Copy key for a failed request that may carry a plan code: the code's own sentence, else the
 * caller's generic key when the server named a code this build does not know (its `message`
 * is not shown), else null so the caller falls back to its usual error text.
 */
export function pipelineFailureKey(err: unknown, fallbackKey: string): string | null {
  return pipelineRefusalKey(err) ?? (pipelineErrorCode(err) !== null ? fallbackKey : null);
}

/**
 * Copy for a refused speaker-detection save. A missing key keeps the existing "save the key
 * first" message; every other known refusal gets its own sentence.
 */
export function speakerSaveErrorKey(err: unknown): string | null {
  return pipelineErrorCode(err) === 'pyannote_key_missing'
    ? 'settings.speakerIdentification.pyannoteKey.requiredToSelect'
    : pipelineRefusalKey(err);
}
