import { describe, it, expect } from 'vitest';
import type { ASRProviderInfo, UserASRSettingsResponse } from '$lib/api/asrSettings';
import {
  capabilityLabel,
  capabilityNotes,
  diarizerChoices,
  modelCapability,
  optionWithFlags,
  providerCapability,
  savedProviderRows,
} from './asrCapabilities';

function provider(overrides: Partial<ASRProviderInfo> & { id: string }): ASRProviderInfo {
  return {
    provider: overrides.id as ASRProviderInfo['provider'],
    display_name: overrides.id,
    name: overrides.id,
    description: '',
    requires_api_key: true,
    supports_region: false,
    supports_base_url: false,
    supports_diarization: false,
    supports_vocabulary: false,
    supports_translation: false,
    sdk_available: true,
    status: 'tested',
    status_note: '',
    diarization_quality: '',
    speaker_count_hints: [],
    timestamp_granularity: 'word',
    models: [],
    ...overrides,
  };
}

// OpenAI as the catalog sends it: text-only at provider level, one model that differs each way.
const OPENAI = provider({
  id: 'openai',
  display_name: 'OpenAI',
  supports_diarization: false,
  timestamp_granularity: 'none',
  models: [
    {
      id: 'whisper-1',
      display_name: 'whisper-1',
      name: 'whisper-1',
      timestamp_granularity: 'segment',
    },
    { id: 'gpt-4o-transcribe', display_name: 'gpt-4o-transcribe', name: 'gpt-4o-transcribe' },
    {
      id: 'gpt-4o-transcribe-diarize',
      display_name: 'gpt-4o-transcribe-diarize',
      name: 'gpt-4o-transcribe-diarize',
      supports_diarization: true,
      timestamp_granularity: 'segment',
    },
  ],
});
const DEEPGRAM = provider({
  id: 'deepgram',
  display_name: 'Deepgram',
  supports_diarization: true,
  speaker_count_hints: [],
  timestamp_granularity: 'word',
  models: [{ id: 'nova-3', display_name: 'Nova 3', name: 'Nova 3' }],
});
const PROVIDERS = [OPENAI, DEEPGRAM];

describe('modelCapability', () => {
  it.each([
    ['whisper-1', false, 'segment'],
    ['gpt-4o-transcribe', false, 'none'],
    ['gpt-4o-transcribe-diarize', true, 'segment'],
  ] as const)('OpenAI %s: speakers=%s, timing=%s', (model, speakers, timing) => {
    expect(modelCapability(PROVIDERS, 'openai', model)).toMatchObject({
      returnsSpeakers: speakers,
      timing,
    });
  });

  it('lets a model without its own value inherit the provider', () => {
    expect(modelCapability(PROVIDERS, 'deepgram', 'nova-3')).toEqual({
      returnsSpeakers: true,
      timing: 'word',
      speakerHints: [],
    });
  });

  it('answers with the provider values for a model the catalog does not list', () => {
    expect(modelCapability(PROVIDERS, 'deepgram', 'unlisted')).toMatchObject({
      returnsSpeakers: true,
      timing: 'word',
    });
  });

  it('is null for a provider the catalog does not know', () => {
    expect(modelCapability(PROVIDERS, 'mystery', 'x')).toBeNull();
  });
});

function config(
  uuid: string,
  providerId: string,
  model: string,
  extra: Partial<UserASRSettingsResponse> = {}
): UserASRSettingsResponse {
  return {
    id: 1,
    uuid,
    user_id: 1,
    name: `${providerId} ${model}`,
    provider: providerId as UserASRSettingsResponse['provider'],
    model_name: model,
    is_active: true,
    has_api_key: true,
    created_at: '',
    updated_at: '',
    ...extra,
  };
}

describe('diarizerChoices', () => {
  const own = [
    config('a', 'deepgram', 'nova-3'),
    config('b', 'openai', 'gpt-4o-transcribe'),
    config('c', 'openai', 'gpt-4o-transcribe-diarize'),
  ];

  it('lists only saved configs whose model returns speakers', () => {
    expect(diarizerChoices(own, [], PROVIDERS, null).map((c) => c.uuid)).toEqual(['a', 'c']);
  });

  it('excludes the active transcription config', () => {
    expect(diarizerChoices(own, [], PROVIDERS, 'a').map((c) => c.uuid)).toEqual(['c']);
  });

  it('includes configs shared with the user, naming their owner', () => {
    const shared = [config('s', 'deepgram', 'nova-3', { owner_name: 'Ada', is_own: false })];
    const choices = diarizerChoices([], shared, PROVIDERS, null);
    expect(choices).toHaveLength(1);
    expect(choices[0]).toMatchObject({ uuid: 's', owner: 'Ada', providerLabel: 'Deepgram' });
  });

  it('skips a config of a provider the catalog does not know', () => {
    expect(diarizerChoices([config('z', 'mystery', 'x')], [], PROVIDERS, null)).toEqual([]);
  });
});

describe('the flags as text, for places that cannot draw pills (#1243)', () => {
  const t = (key: string, vars?: Record<string, string>) =>
    vars ? `${key}|${Object.values(vars).join('|')}` : key;

  it('names speakers and timing in one translated sentence with placeholders', () => {
    expect(capabilityLabel({ returnsSpeakers: true, timing: 'word' }, t)).toBe(
      'settings.asrProvider.flagsLine|settings.asrProvider.speakersIncluded|settings.asrProvider.timing.word'
    );
    expect(capabilityLabel({ returnsSpeakers: false, timing: 'none' }, t)).toContain(
      'settings.asrProvider.textOnly|settings.asrProvider.timing.none'
    );
  });

  it('wraps an option name with its flags, claims nothing for an unknown model, says varies', () => {
    expect(optionWithFlags('Nova', { returnsSpeakers: true, timing: 'word' }, t)).toContain(
      'settings.asrProvider.optionWithFlags|Nova|'
    );
    expect(optionWithFlags('Mystery', null, t)).toBe('Mystery');
    expect(optionWithFlags('OpenAI', 'varies', t)).toBe('settings.asrProvider.optionVaries|OpenAI');
  });
});

describe('what the flags mean for mixing and matching (#1243)', () => {
  const keys = (cap: { returnsSpeakers: boolean; timing: 'word' | 'segment' | 'none' }, ctx = {}) =>
    capabilityNotes(cap, ctx).map((n) => n.key);

  it('blocks a model with no timing from speaker detection, whatever else it returns', () => {
    expect(capabilityNotes({ returnsSpeakers: true, timing: 'none' })).toEqual([
      { key: 'settings.asrProvider.timing.noneInfo', blocking: true },
    ]);
    expect(keys({ returnsSpeakers: false, timing: 'none' })).toEqual([
      'settings.asrProvider.timing.noneInfo',
    ]);
  });

  it('says a speakers-included model needs no separate detector, and where its speakers come from', () => {
    expect(keys({ returnsSpeakers: true, timing: 'word' })).toEqual([
      'settings.asrProvider.implication.speakersIncluded',
    ]);
    expect(keys({ returnsSpeakers: true, timing: 'word' }, { local: true })).toEqual([
      'settings.asrProvider.implication.speakersLocal',
    ]);
  });

  it('says a text-only model takes speakers from this server or another provider, never this server on lite', () => {
    expect(keys({ returnsSpeakers: false, timing: 'word' })).toEqual([
      'settings.asrProvider.implication.textOnly',
    ]);
    expect(keys({ returnsSpeakers: false, timing: 'word' }, { lite: true })).toEqual([
      'settings.asrProvider.implication.textOnlyLite',
    ]);
  });

  it('adds the segment-timing caveat only where another detector adds the speakers', () => {
    expect(keys({ returnsSpeakers: false, timing: 'segment' })).toEqual([
      'settings.asrProvider.implication.textOnly',
      'settings.pipeline.issue.segment_timing_only',
    ]);
    expect(keys({ returnsSpeakers: true, timing: 'segment' })).toEqual([
      'settings.asrProvider.implication.speakersIncluded',
    ]);
  });
});

describe('provider-level flags and the saved provider list (#1243)', () => {
  it('reports one set when every model of a provider agrees', () => {
    expect(providerCapability(PROVIDERS, 'deepgram')).toEqual({
      returnsSpeakers: true,
      timing: 'word',
      speakerHints: [],
    });
  });

  it('reports varies for a provider whose models differ (OpenAI)', () => {
    expect(providerCapability(PROVIDERS, 'openai')).toBe('varies');
  });

  it('is null for a provider the catalog does not list', () => {
    expect(providerCapability(PROVIDERS, 'nope')).toBeNull();
  });

  const config = (uuid: string, providerId: string, model: string) =>
    ({
      uuid,
      name: uuid,
      provider: providerId,
      model_name: model,
      is_own: true,
    }) as UserASRSettingsResponse;

  it('lists every saved config with what it returns and keeps the transcribing one out of the choices', () => {
    const own = [
      config('a', 'deepgram', 'nova-3'),
      config('b', 'openai', 'gpt-4o-transcribe'),
      config('c', 'mystery', 'x'),
    ];
    const rows = savedProviderRows(own, [], PROVIDERS, 'a');
    expect(rows.map((r) => [r.uuid, r.returnsSpeakers, r.timing, r.transcribing])).toEqual([
      ['a', true, 'word', true],
      ['b', false, 'none', false],
      ['c', false, null, false],
    ]);
    expect(diarizerChoices(own, [], PROVIDERS, 'a')).toEqual([]);
    expect(diarizerChoices(own, [], PROVIDERS, null).map((c) => c.uuid)).toEqual(['a']);
  });
});
