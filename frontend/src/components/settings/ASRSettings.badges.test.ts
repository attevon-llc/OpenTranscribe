import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

const asrApi = vi.hoisted(() => ({
  getProviders: vi.fn(),
  getSettings: vi.fn(),
  getActiveLocalModel: vi.fn(),
  getStatus: vi.fn(),
  getProviderDisplayName: (p: string) => p,
}));
vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: asrApi }));
const speakerApi = vi.hoisted(() => ({ getTranscriptionSettings: vi.fn() }));
vi.mock('$lib/api/transcriptionSettings', () => speakerApi);
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));
vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});

import ASRSettings from './ASRSettings.svelte';

// The catalog as the server sends it: OpenAI is text-only at provider level, and three of
// its models differ (whisper-1 times by segment, gpt-4o-transcribe has no timestamps at all,
// the -diarize model returns speakers at segment level).
const PROVIDERS = [
  {
    id: 'openai',
    provider: 'openai',
    display_name: 'OpenAI',
    name: 'OpenAI',
    supports_diarization: false,
    speaker_count_hints: [],
    timestamp_granularity: 'none',
    status: 'tested',
    models: [
      {
        id: 'whisper-1',
        name: 'whisper-1',
        display_name: 'whisper-1',
        timestamp_granularity: 'segment',
      },
      { id: 'gpt-4o-transcribe', name: 'gpt-4o-transcribe', display_name: 'gpt-4o-transcribe' },
      {
        id: 'gpt-4o-transcribe-diarize',
        name: 'gpt-4o-transcribe-diarize',
        display_name: 'gpt-4o-transcribe-diarize',
        supports_diarization: true,
        timestamp_granularity: 'segment',
      },
    ],
  },
  {
    id: 'deepgram',
    provider: 'deepgram',
    display_name: 'Deepgram',
    name: 'Deepgram',
    supports_diarization: true,
    speaker_count_hints: [],
    timestamp_granularity: 'word',
    status: 'tested',
    models: [{ id: 'nova-3', name: 'nova-3', display_name: 'nova-3' }],
  },
];
const config = (uuid: string, provider: string, model: string) => ({
  id: 1,
  uuid,
  user_id: 1,
  name: `cfg-${uuid}`,
  provider,
  model_name: model,
  is_active: true,
  has_api_key: true,
  is_shared: false,
  created_at: '',
  updated_at: '',
});
const CONFIGS = [
  config('w1', 'openai', 'whisper-1'),
  config('g4', 'openai', 'gpt-4o-transcribe'),
  config('g4d', 'openai', 'gpt-4o-transcribe-diarize'),
  config('dg', 'deepgram', 'nova-3'),
];

beforeEach(() => {
  vi.clearAllMocks();
  asrApi.getProviders.mockResolvedValue({ providers: PROVIDERS });
  asrApi.getSettings.mockResolvedValue({
    configurations: CONFIGS,
    shared_configurations: [],
    active_configuration_uuid: 'dg',
    active_configuration_id: 'dg',
    total: CONFIGS.length,
  });
  asrApi.getActiveLocalModel.mockResolvedValue({
    active_model: 'large-v3-turbo',
    source: 'environment',
    available_models: [{ short_name: 'large-v3-turbo' }],
    model_info: {
      display_name: 'Large v3 Turbo',
      description: 'd',
      supports_diarization: true,
      supports_translation: false,
      language_support: 'english_optimized',
    },
  });
  asrApi.getStatus.mockResolvedValue({ deployment_mode: 'full' });
  speakerApi.getTranscriptionSettings.mockResolvedValue({
    diarization_source: 'provider',
    diarization_asr_config_uuid: null,
  });
});

async function renderLoaded() {
  const result = render(ASRSettings, { props: { isAdmin: false } });
  await waitFor(() =>
    expect(result.container.querySelectorAll('.config-card').length).toBe(CONFIGS.length)
  );
  return result;
}
const cardFor = (c: HTMLElement, uuid: string) =>
  Array.from(c.querySelectorAll<HTMLElement>('.config-card')).find(
    (el) => el.querySelector('.config-name')?.textContent?.trim() === `cfg-${uuid}`
  )!;
const speakers = (el: HTMLElement) =>
  el.querySelector('[data-testid="badge-speakers"]')?.getAttribute('data-value');
const timing = (el: HTMLElement) =>
  el.querySelector('[data-testid="badge-timing"]')?.getAttribute('data-value');

describe('provider cards say what each model returns (#1219)', () => {
  it('whisper-1 is text only with segment timing', async () => {
    const { container } = await renderLoaded();
    const card = cardFor(container, 'w1');
    expect(speakers(card)).toBe('text-only');
    expect(timing(card)).toBe('segment');
  });

  it('gpt-4o-transcribe is text only with no timing', async () => {
    const { container } = await renderLoaded();
    const card = cardFor(container, 'g4');
    expect(speakers(card)).toBe('text-only');
    expect(timing(card)).toBe('none');
  });

  it('gpt-4o-transcribe-diarize includes speakers at segment timing, overriding its provider', async () => {
    const { container } = await renderLoaded();
    const card = cardFor(container, 'g4d');
    expect(speakers(card)).toBe('included');
    expect(timing(card)).toBe('segment');
  });

  it('a speaker-returning provider with word timing shows both', async () => {
    const { container } = await renderLoaded();
    const card = cardFor(container, 'dg');
    expect(speakers(card)).toBe('included');
    expect(timing(card)).toBe('word');
  });

  it('shows the extra-billing badge only on the config chosen as the speaker detector', async () => {
    speakerApi.getTranscriptionSettings.mockResolvedValue({
      diarization_source: 'asr_config',
      diarization_asr_config_uuid: 'g4d',
    });
    const { container } = await renderLoaded();
    expect(cardFor(container, 'g4d').querySelector('[data-testid="badge-adapter"]')).not.toBeNull();
    expect(cardFor(container, 'dg').querySelector('[data-testid="badge-adapter"]')).toBeNull();
    expect(cardFor(container, 'w1').querySelector('[data-testid="badge-adapter"]')).toBeNull();
  });

  it('shows no extra-billing badge when the source is not asr_config', async () => {
    const { container } = await renderLoaded();
    expect(container.querySelector('[data-testid="badge-adapter"]')).toBeNull();
  });

  it('still lists the configs when the speaker settings cannot be read', async () => {
    speakerApi.getTranscriptionSettings.mockRejectedValue(new Error('down'));
    const { container } = await renderLoaded();
    expect(container.querySelector('[data-testid="badge-adapter"]')).toBeNull();
  });
});

describe('lite deployment', () => {
  it('hides the local model card and its controls and says why', async () => {
    asrApi.getStatus.mockResolvedValue({ deployment_mode: 'lite' });
    const { container } = await renderLoaded();
    expect(container.querySelector('.local-model-section')).toBeNull();
    expect(container.textContent).not.toContain('settings.asrProvider.localGpuModel');
    expect(container.querySelector('[data-testid="asr-lite-note"]')?.textContent).toContain(
      'settings.pipeline.issue.local_asr_unavailable'
    );
  });

  it('offers no way back to the local model while a cloud config is active', async () => {
    asrApi.getStatus.mockResolvedValue({ deployment_mode: 'lite' });
    const { container } = await renderLoaded();
    expect(container.querySelector('.btn-use-local')).toBeNull();
  });

  it('keeps the local model card and the switch back on a full deployment', async () => {
    const { container } = await renderLoaded();
    expect(container.querySelector('.local-model-section')).not.toBeNull();
    expect(container.querySelector('.btn-use-local')).not.toBeNull();
    expect(container.querySelector('[data-testid="asr-lite-note"]')).toBeNull();
  });
});
