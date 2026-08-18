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

import { ASRSettingsApi, CustomVocabularyApi } from './asrSettings';

beforeEach(() => {
  vi.clearAllMocks();
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
