import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$lib/api/asrSettings', () => ({
  ASRSettingsApi: {
    getProviderDisplayName: (p: string) => p,
    formatPricePerHour: () => '',
  },
}));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));
vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});

import ASRConfigModal from './ASRConfigModal.svelte';
import type { ASRProviderInfo } from '$lib/api/asrSettings';

// A partial catalog: only the fields the editor reads.
const PROVIDERS = [
  {
    id: 'openai',
    provider: 'openai',
    display_name: 'OpenAI',
    name: 'OpenAI',
    requires_api_key: true,
    supports_diarization: false,
    speaker_count_hints: [],
    timestamp_granularity: 'none',
    supports_vocabulary: false,
    supports_translation: false,
    sdk_available: true,
    status: 'tested',
    status_note: '',
    diarization_quality: '',
    description: '',
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
    requires_api_key: true,
    supports_diarization: true,
    speaker_count_hints: [],
    timestamp_granularity: 'word',
    supports_vocabulary: true,
    supports_translation: false,
    sdk_available: true,
    status: 'tested',
    status_note: '',
    diarization_quality: '',
    description: '',
    models: [{ id: 'nova-3', name: 'nova-3', display_name: 'nova-3' }],
  },
] as unknown as ASRProviderInfo[];

beforeEach(() => vi.clearAllMocks());

async function open(props: Record<string, unknown> = {}) {
  const result = render(ASRConfigModal, {
    props: { show: true, providers: PROVIDERS, diarizationSource: 'provider', ...props },
  });
  return result;
}
async function pick(provider: string, model?: string) {
  const providerSelect = await waitFor(() => {
    const el = document.querySelector('#asr-provider') as HTMLSelectElement;
    expect(el).not.toBeNull();
    return el;
  });
  await fireEvent.change(providerSelect, { target: { value: provider } });
  if (model) {
    const modelSelect = await waitFor(() => {
      const el = document.querySelector('#asr-model') as HTMLSelectElement;
      expect(el).not.toBeNull();
      return el;
    });
    await fireEvent.change(modelSelect, { target: { value: model } });
  }
}
const badges = () => document.querySelector('[data-testid="model-capabilities"]');
const value = (testid: string) =>
  badges()?.querySelector(`[data-testid="${testid}"]`)?.getAttribute('data-value');
const noticeCodes = () =>
  Array.from(document.querySelectorAll<HTMLElement>('[data-testid="model-notice"]')).map(
    (n) => n.dataset.code
  );

describe('the model picker says what the chosen model returns', () => {
  it('shows Speakers included and Word timing for a model that has both', async () => {
    await open();
    await pick('deepgram', 'nova-3');
    expect(value('badge-speakers')).toBe('included');
    expect(value('badge-timing')).toBe('word');
    expect(noticeCodes()).toEqual([]);
  });

  it('warns that Automatic will detect speakers on this server for a text-only model with timing', async () => {
    await open();
    await pick('openai', 'whisper-1');
    expect(value('badge-speakers')).toBe('text-only');
    expect(value('badge-timing')).toBe('segment');
    expect(noticeCodes()).toEqual(['automatic_uses_server', 'segment_timing_only']);
  });

  it('warns that speakers cannot be added to a model with no timestamps', async () => {
    await open();
    await pick('openai', 'gpt-4o-transcribe');
    expect(value('badge-timing')).toBe('none');
    expect(document.querySelector('[data-testid="model-no-timing-info"]')).not.toBeNull();
    expect(noticeCodes()).toEqual(['asr_no_timing_for_speakers']);
  });

  it('lets the diarize model supply its own speakers without a warning', async () => {
    await open();
    await pick('openai', 'gpt-4o-transcribe-diarize');
    expect(value('badge-speakers')).toBe('included');
    expect(noticeCodes()).toEqual([]);
  });

  it('is quiet when the user turned speaker detection off, apart from the explanation', async () => {
    await open({ diarizationSource: 'off' });
    await pick('openai', 'gpt-4o-transcribe');
    expect(noticeCodes()).toEqual([]);
    expect(document.querySelector('[data-testid="model-no-timing-info"]')).not.toBeNull();
  });

  it('on lite, a text-only model is an error that points at another speaker provider', async () => {
    await open({ deploymentMode: 'lite' });
    await pick('openai', 'whisper-1');
    expect(noticeCodes()[0]).toBe('automatic_needs_speaker_provider');
  });

  it('shows the provider values before a model is picked', async () => {
    await open();
    await pick('openai');
    await waitFor(() => expect(value('badge-timing')).toBe('none'));
    expect(value('badge-speakers')).toBe('text-only');
  });
});
