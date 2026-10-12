import { describe, it, expect } from 'vitest';
import type { ASRProviderInfo, UserASRSettingsResponse } from '$lib/api/asrSettings';
import { diarizerChoices, modelCapability } from './asrCapabilities';

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
