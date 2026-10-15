import { describe, it, expect } from 'vitest';
import en from '$lib/i18n/locales/en.json';
import {
  PIPELINE_ISSUE_CODES,
  fixLinkKey,
  fixTargets,
  issueKey,
  isPipelineIssueCode,
  pipelineErrorCode,
  pipelineFailureKey,
  speakerSaveErrorKey,
} from './pipelineIssues';

const EN_KEYS = new Set(Object.keys(en));
const hasKey = (key: string) => EN_KEYS.has(key);
const conflict = (detail: unknown) => ({ response: { status: 409, data: { detail } } });

describe('PIPELINE_ISSUE_CODES', () => {
  it('lists the fifteen codes the resolver can emit, each with a sentence in en.json', () => {
    // Mirrors ISSUE_MESSAGES in backend/app/services/pipeline_plan.py.
    expect([...PIPELINE_ISSUE_CODES].sort()).toEqual(
      [
        'asr_no_timing_for_speakers',
        'asr_provider_not_allowed',
        'automatic_needs_speaker_provider',
        'automatic_uses_server',
        'diarizer_config_missing',
        'diarizer_config_no_speakers',
        'diarizer_double_billing',
        'diarizer_provider_not_allowed',
        'diarizer_same_as_transcription',
        'local_asr_unavailable',
        'local_diarization_unavailable',
        'pyannote_key_missing',
        'segment_timing_only',
        'source_locked',
        'speaker_hints_ignored',
      ].sort()
    );
    for (const code of PIPELINE_ISSUE_CODES) {
      expect(hasKey(issueKey(code)), code).toBe(true);
    }
  });

  it('recognises a code it knows and refuses anything else', () => {
    expect(isPipelineIssueCode('pyannote_key_missing')).toBe(true);
    expect(isPipelineIssueCode('Traceback (most recent call last)')).toBe(false);
  });
});

describe('fixTargets', () => {
  it('sends a model-caused problem to the provider tab and a source-caused one to Speakers', () => {
    expect(fixTargets({ code: 'segment_timing_only', step: 'diarization' })).toEqual([
      'asr-provider',
    ]);
    expect(fixTargets({ code: 'pyannote_key_missing', step: 'diarization' })).toEqual([
      'speaker-identification',
    ]);
    expect(fixTargets({ code: 'local_asr_unavailable', step: 'transcription' })).toEqual([
      'asr-provider',
    ]);
  });

  it('offers both tabs where either one fixes it', () => {
    expect(fixTargets({ code: 'asr_no_timing_for_speakers', step: 'diarization' })).toEqual([
      'asr-provider',
      'speaker-identification',
    ]);
    expect(fixTargets({ code: 'automatic_needs_speaker_provider', step: 'diarization' })).toEqual([
      'speaker-identification',
      'asr-provider',
    ]);
  });

  it('sends a provider the administrator has not enabled to the provider tab', () => {
    expect(fixTargets({ code: 'asr_provider_not_allowed', step: 'transcription' })).toEqual([
      'asr-provider',
    ]);
    expect(fixTargets({ code: 'diarizer_provider_not_allowed', step: 'diarization' })).toEqual([
      'asr-provider',
    ]);
  });

  it('offers no link for an administrator-owned setting', () => {
    expect(fixTargets({ code: 'source_locked', step: 'diarization' })).toEqual([]);
  });

  it('falls back to the step own tab for a code this build does not know', () => {
    expect(fixTargets({ code: 'brand_new_code', step: 'llm' })).toEqual(['llm-provider']);
    expect(fixTargets({ code: 'brand_new_code', step: 'transcription' })).toEqual(['asr-provider']);
  });

  it('has a link label in en.json for every target', () => {
    for (const section of ['asr-provider', 'speaker-identification', 'llm-provider'] as const) {
      expect(hasKey(fixLinkKey(section)), section).toBe(true);
    }
  });
});

describe('pipelineErrorCode', () => {
  it('reads detail.code of a 409', () => {
    expect(pipelineErrorCode(conflict({ code: 'asr_no_timing_for_speakers', message: 'x' }))).toBe(
      'asr_no_timing_for_speakers'
    );
  });

  it('is null for a bare-string detail, another status or no response', () => {
    expect(pipelineErrorCode(conflict('Some legacy sentence'))).toBeNull();
    expect(
      pipelineErrorCode({ response: { status: 422, data: { detail: { code: 'x' } } } })
    ).toBeNull();
    expect(pipelineErrorCode(new Error('network'))).toBeNull();
  });
});

describe('speakerSaveErrorKey', () => {
  it('asks for a key only when the server says the key is missing', () => {
    const key = speakerSaveErrorKey(conflict({ code: 'pyannote_key_missing', message: 'm' }));
    expect(key).toBe('settings.speakerIdentification.pyannoteKey.requiredToSelect');
    expect(hasKey(key as string)).toBe(true);
  });

  it('does not tell a user to save a key for any other refusal (the #1227 bug)', () => {
    const key = speakerSaveErrorKey(conflict({ code: 'asr_no_timing_for_speakers', message: 'm' }));
    expect(key).toBe('settings.pipeline.issue.asr_no_timing_for_speakers');
    expect(key).not.toContain('pyannoteKey');
    expect(speakerSaveErrorKey(conflict({ code: 'diarizer_config_missing', message: 'm' }))).toBe(
      'settings.pipeline.issue.diarizer_config_missing'
    );
  });

  it('gives no specific copy for a 409 without a known code, never the raw text', () => {
    expect(
      speakerSaveErrorKey(conflict({ code: 'unheard_of', message: 'raw vendor text' }))
    ).toBeNull();
    expect(speakerSaveErrorKey(conflict('pyannote.ai needs a key'))).toBeNull();
    expect(speakerSaveErrorKey({ response: { status: 409 } })).toBeNull();
  });
});

describe('policy refusals', () => {
  it('renders a refused own key as a fixed sentence, never the server text', () => {
    const err = conflict({ code: 'own_asr_keys_disabled', message: 'raw server sentence' });
    const key = pipelineFailureKey(err, 'g.key');
    expect(key).toBe('settings.asrProvider.error.own_asr_keys_disabled');
    expect(hasKey(key as string)).toBe(true);
  });

  it('renders a refused provider with the pipeline sentence', () => {
    expect(
      pipelineFailureKey(conflict({ code: 'asr_provider_not_allowed', message: 'm' }), 'g.key')
    ).toBe('settings.pipeline.issue.asr_provider_not_allowed');
  });
});

describe('pipelineFailureKey', () => {
  it('gives a known code its sentence, an unknown code the generic key, and no code nothing', () => {
    expect(
      pipelineFailureKey(conflict({ code: 'local_asr_unavailable', message: 'm' }), 'g.key')
    ).toBe('settings.pipeline.issue.local_asr_unavailable');
    expect(pipelineFailureKey(conflict({ code: 'unheard_of', message: 'raw' }), 'g.key')).toBe(
      'g.key'
    );
    expect(pipelineFailureKey(new Error('network'), 'g.key')).toBeNull();
  });
});
