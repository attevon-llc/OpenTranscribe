/**
 * `asrSettings.ts`'s real logic is the field-normalization in `getProviders()`/
 * `getSettings()` — the backend catalog uses different field names than the
 * frontend interfaces (documented in the module itself), and a silent mismatch
 * there means a provider's capabilities render wrong or a price shows as
 * undefined. `CustomVocabularyApi.getVocabulary` similarly merges two backend
 * arrays into one list the UI expects to already be flat.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('../axios', () => ({ default: mockInstance }));

import { ASRSettingsApi, CustomVocabularyApi, type ASRConfigDeleted } from './asrSettings';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('credentials are write-only (#1246)', () => {
  it('has no way to read a stored key back', () => {
    expect('getApiKey' in ASRSettingsApi).toBe(false);
  });
});

describe('ASRSettingsApi.getProviders policy flag', () => {
  it('keeps allowed_for_users for administrators and leaves it undefined for users', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        providers: [
          { id: 'deepgram', display_name: 'Deepgram', allowed_for_users: false, models: [] },
          { id: 'openai', display_name: 'OpenAI', models: [] },
        ],
      },
    });
    const { providers } = await ASRSettingsApi.getProviders();
    expect(providers[0].allowed_for_users).toBe(false);
    expect(providers[1].allowed_for_users).toBeUndefined();
  });
});

describe('ASRSettingsApi.getProviders', () => {
  it('normalizes backend field names to the frontend interface, including nested models', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        providers: [
          {
            id: 'deepgram',
            display_name: 'Deepgram',
            requires_api_key: true,
            requires_region: false,
            supports_custom_url: true,
            status: 'tested',
            models: [
              {
                id: 'nova-2',
                display_name: 'Nova 2',
                price_per_min_stream: 0.0043,
              },
            ],
          },
        ],
      },
    });

    const { providers } = await ASRSettingsApi.getProviders();

    expect(providers).toHaveLength(1);
    const [p] = providers;
    expect(p.id).toBe('deepgram');
    expect(p.name).toBe('Deepgram'); // display_name aliased to name
    expect(p.supports_region).toBe(false); // requires_region -> supports_region
    expect(p.supports_base_url).toBe(true); // supports_custom_url -> supports_base_url
    expect(p.sdk_available).toBe(true); // absent in backend -> defaults true
    expect(p.models[0].price_per_min_realtime).toBe(0.0043); // price_per_min_stream -> _realtime
  });

  it('keeps what each provider and model returns: speakers, hints and timestamp granularity (#1227)', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        providers: [
          {
            id: 'openai',
            display_name: 'OpenAI',
            supports_diarization: false,
            speaker_count_hints: ['num'],
            timestamp_granularity: 'none',
            models: [
              { id: 'gpt-4o-transcribe' },
              {
                id: 'gpt-4o-transcribe-diarize',
                supports_diarization: true,
                timestamp_granularity: 'segment',
                speaker_count_hints: [],
              },
            ],
          },
          { id: 'legacy', display_name: 'Legacy', models: [] },
        ],
      },
    });

    const { providers } = await ASRSettingsApi.getProviders();

    expect(providers[0].timestamp_granularity).toBe('none');
    expect(providers[0].speaker_count_hints).toEqual(['num']);
    // A model that does not differ carries nothing of its own, so the provider's value applies.
    expect(providers[0].models[0].timestamp_granularity).toBeUndefined();
    expect(providers[0].models[1]).toMatchObject({
      supports_diarization: true,
      timestamp_granularity: 'segment',
      speaker_count_hints: [],
    });
    // A catalog that predates the fields cannot be assumed to time its text.
    expect(providers[1].timestamp_granularity).toBe('none');
    expect(providers[1].speaker_count_hints).toEqual([]);
  });

  it('falls back sensibly when the backend omits optional fields entirely', async () => {
    mockInstance.get.mockResolvedValue({ data: { providers: [{}] } });

    const { providers } = await ASRSettingsApi.getProviders();

    expect(providers[0].id).toBe('');
    expect(providers[0].requires_api_key).toBe(false);
    expect(providers[0].status).toBe('experimental');
    expect(providers[0].models).toEqual([]);
  });
});

describe('ASRSettingsApi.getLocalWhisperModels', () => {
  it("extracts and re-shapes the 'local' provider's model catalog", async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        providers: [
          {
            id: 'local',
            models: [{ id: 'base', display_name: 'Base', downloaded: true }],
          },
          { id: 'openai', models: [{ id: 'whisper-1' }] },
        ],
      },
    });

    const models = await ASRSettingsApi.getLocalWhisperModels();

    expect(models).toEqual([
      {
        id: 'base',
        display_name: 'Base',
        description: '',
        downloaded: true,
        supports_translation: false,
        language_support: 'multilingual',
      },
    ]);
  });

  it('returns an empty array when no local provider is in the catalog', async () => {
    mockInstance.get.mockResolvedValue({ data: { providers: [{ id: 'openai', models: [] }] } });
    expect(await ASRSettingsApi.getLocalWhisperModels()).toEqual([]);
  });
});

describe('ASRSettingsApi.getSettings', () => {
  it('normalizes configs/configurations and active_config_uuid/active_configuration_uuid aliases', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        configs: [{ uuid: 'c1' }, { uuid: 'c2' }],
        active_config_uuid: 'c1',
      },
    });

    const settings = await ASRSettingsApi.getSettings();

    expect(settings.configurations).toHaveLength(2);
    expect(settings.total).toBe(2);
    expect(settings.active_configuration_uuid).toBe('c1');
    expect(settings.active_configuration_id).toBe('c1'); // backwards-compat alias
  });
});

describe('ASRSettingsApi presentational helpers', () => {
  it('formats price per minute as an hourly estimate', () => {
    expect(ASRSettingsApi.formatPricePerHour(0.0043)).toBe('$0.26/hr');
  });

  it('maps known connection statuses to their CSS class, defaulting unknowns to neutral', () => {
    expect(ASRSettingsApi.getStatusColor('success')).toBe('status-success');
    expect(ASRSettingsApi.getStatusColor('failed')).toBe('status-error');
    expect(ASRSettingsApi.getStatusColor('something-else')).toBe('status-neutral');
  });

  it('maps known providers to a friendly display name, falling back to the raw id', () => {
    expect(ASRSettingsApi.getProviderDisplayName('deepgram')).toBe('Deepgram');
    expect(ASRSettingsApi.getProviderDisplayName('unknown-provider')).toBe('unknown-provider');
  });
});

describe('CustomVocabularyApi.getVocabulary', () => {
  it('merges user terms and system terms into one flat list', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        terms: [
          { id: 1, term: 'OpenTranscribe', domain: 'general', is_active: true, is_system: false },
        ],
        system_terms: [
          { id: 2, term: 'PyAnnote', domain: 'general', is_active: true, is_system: true },
        ],
      },
    });

    const vocab = await CustomVocabularyApi.getVocabulary();

    expect(vocab.map((v) => v.term)).toEqual(['OpenTranscribe', 'PyAnnote']);
  });

  it('passes domain and active_only through as query params, but omits domain for "all"', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        terms: [{ id: 1, term: 'x', domain: 'medical', is_active: true, is_system: false }],
        system_terms: [],
      },
    });

    const medical = await CustomVocabularyApi.getVocabulary('medical');
    expect(mockInstance.get).toHaveBeenCalledWith('/custom-vocabulary', {
      params: { domain: 'medical', active_only: false },
    });
    expect(medical).toHaveLength(1);

    mockInstance.get.mockResolvedValue({ data: { terms: [], system_terms: [] } });
    await CustomVocabularyApi.getVocabulary('all');
    expect(mockInstance.get).toHaveBeenCalledWith('/custom-vocabulary', {
      params: { active_only: false },
    });
  });
});

// Issue #1236: the delete answers with the pipeline it leaves (it used to be a bare 204,
// discarded here), so the settings UI can say when speaker detection is no longer runnable.
describe('ASRSettingsApi.deleteConfig', () => {
  it('returns the active config and pipeline issues the delete leaves', async () => {
    const body: ASRConfigDeleted = {
      message: 'ASR configuration deleted',
      config_uuid: null,
      deleted: true,
      diarizer_source_reverted: true,
      pipeline_issues: [
        {
          code: 'pyannote_key_missing',
          severity: 'error',
          step: 'diarization',
          message: 'pyannote.ai speaker detection needs a pyannote.ai API key.',
        },
      ],
    };
    mockInstance.delete.mockResolvedValue({ data: body });

    const result = await ASRSettingsApi.deleteConfig('cfg-uuid');

    expect(mockInstance.delete).toHaveBeenCalledWith('/asr-settings/config/cfg-uuid');
    expect(result).toEqual(body);
    expect(result.pipeline_issues[0].code).toBe('pyannote_key_missing');
  });

  it('propagates a lite refusal (409) to the caller instead of swallowing it', async () => {
    const refusal = {
      response: { status: 409, data: { detail: { code: 'local_asr_unavailable', message: 'x' } } },
    };
    mockInstance.delete.mockRejectedValue(refusal);

    await expect(ASRSettingsApi.deleteConfig('cfg-uuid')).rejects.toBe(refusal);
  });
});
