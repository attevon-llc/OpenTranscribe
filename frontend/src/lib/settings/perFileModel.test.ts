import { describe, it, expect, vi } from 'vitest';
import type { ASRProviderInfo } from '$lib/api/asrSettings';

vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: {} }));

import { FAST_MODEL, optionName, perFileOptions, type PerFileFacts } from './perFileModel';

function provider(id: string, overrides: Partial<ASRProviderInfo> = {}): ASRProviderInfo {
  return {
    provider: id as ASRProviderInfo['provider'],
    id,
    display_name: id === 'local' ? 'Local (GPU)' : id[0].toUpperCase() + id.slice(1),
    name: id,
    description: '',
    requires_api_key: false,
    supports_region: false,
    supports_base_url: false,
    supports_diarization: true,
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

const PROVIDERS = [
  provider('local', {
    models: [
      { id: 'large-v3-turbo', name: 'large-v3-turbo', display_name: 'Large V3 Turbo' },
      { id: 'base', name: 'base', display_name: 'Base' },
    ],
  }),
  provider('openai', {
    supports_diarization: false,
    timestamp_granularity: 'none',
    models: [
      {
        id: 'whisper-1',
        name: 'whisper-1',
        display_name: 'whisper-1',
        timestamp_granularity: 'segment',
      },
    ],
  }),
];

function facts(status: Partial<PerFileFacts['status']>, providers = PROVIDERS): PerFileFacts {
  return { providers, status: status as PerFileFacts['status'] };
}

describe('the per-file model options and what each returns (#1243)', () => {
  it('names this server model when nothing else transcribes, with the catalog local flags', () => {
    const [def, fast] = perFileOptions(facts({ deployment_mode: 'full' }), 'large-v3-turbo');
    expect(def).toMatchObject({ value: null, kind: 'default', local: true, vendor: null });
    expect(def.capability).toMatchObject({ returnsSpeakers: true, timing: 'word' });
    expect(fast).toMatchObject({ value: FAST_MODEL, kind: 'fast', local: true });
    expect(fast.capability).toMatchObject({ returnsSpeakers: true, timing: 'word' });
  });

  it('names the saved cloud provider as what "my setting" runs, with its own flags', () => {
    const [def] = perFileOptions(
      facts({
        deployment_mode: 'full',
        is_cloud_provider: true,
        active_provider: 'openai',
        active_model: 'whisper-1',
      }),
      'large-v3-turbo'
    );
    expect(def).toMatchObject({ local: false, vendor: 'Openai', model: 'whisper-1' });
    expect(def.capability).toMatchObject({ returnsSpeakers: false, timing: 'segment' });
  });

  it('offers no Fast option on lite, which cannot transcribe on this server', () => {
    const options = perFileOptions(
      facts({
        deployment_mode: 'lite',
        is_cloud_provider: true,
        active_provider: 'openai',
        active_model: 'whisper-1',
      }),
      'large-v3-turbo'
    );
    expect(options.map((o) => o.kind)).toEqual(['default']);
  });

  it('claims no flags when the catalog could not be read', () => {
    const options = perFileOptions(null, 'large-v3-turbo');
    expect(options.map((o) => o.capability)).toEqual([null, null]);
  });

  it('writes each option name as one translated string', () => {
    const t = (key: string, vars?: Record<string, string>) =>
      vars ? `${key}|${Object.values(vars).join('|')}` : key;
    const [def, fast] = perFileOptions(facts({ deployment_mode: 'full' }), 'large-v3-turbo');
    expect(optionName(def, t)).toBe('uploader.highQualityOption|large-v3-turbo');
    expect(optionName(fast, t)).toBe('uploader.fastProcessingOption');
    const [cloud] = perFileOptions(
      facts({ is_cloud_provider: true, active_provider: 'openai', active_model: 'whisper-1' }),
      'large-v3-turbo'
    );
    expect(optionName(cloud, t)).toBe('uploader.cloudDefaultOption|Openai|whisper-1');
  });
});
