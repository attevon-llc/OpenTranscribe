import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: unknown) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.mock('$lib/api/asrSettings', () => ({
  ASRSettingsApi: {
    getStatus: vi.fn().mockResolvedValue({}),
    getProviderDisplayName: (p: string) => p,
  },
}));

const api = vi.hoisted(() => ({
  getTranscriptionSettings: vi.fn(),
  getTranscriptionSystemDefaults: vi.fn(),
  updateTranscriptionSettings: vi.fn(),
  resetTranscriptionSettings: vi.fn(),
}));
vi.mock('$lib/api/transcriptionSettings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/transcriptionSettings')>();
  return { ...actual, ...api };
});

import SpeakerDetectionSettings from './SpeakerDetectionSettings.svelte';
import { capabilities, resetCapabilities } from '$stores/capabilities';

const SETTINGS = {
  min_speakers: 1,
  max_speakers: 20,
  speaker_prompt_behavior: 'always_prompt',
  garbage_cleanup_enabled: true,
  garbage_cleanup_threshold: 50,
  source_language: 'auto',
  translate_to_english: false,
  llm_output_language: 'en',
  vad_threshold: 0.5,
  vad_min_silence_ms: 2000,
  vad_min_speech_ms: 250,
  vad_speech_pad_ms: 400,
  hallucination_silence_threshold: null,
  repetition_penalty: 1.0,
  diarization_source: 'provider',
};

function lock(caps: Record<string, boolean>) {
  capabilities.set({
    edition: 'community',
    loaded: true,
    capabilities: caps,
    audience: {},
    maxUploadBytes: undefined,
    apiMediatedUploadEnabled: true,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS });
  api.getTranscriptionSystemDefaults.mockResolvedValue({
    min_speakers: 1,
    max_speakers: 20,
    garbage_cleanup_threshold: 50,
    diarization_source_default: 'provider',
    available_source_languages: { auto: 'Auto-detect', en: 'English' },
    available_llm_output_languages: { en: 'English', de: 'German' },
    common_languages: ['auto', 'en'],
  });
  api.updateTranscriptionSettings.mockImplementation(async (u) => ({ ...SETTINGS, ...u }));
});

afterEach(() => resetCapabilities());
/**
 * `transcription.diarization_source` is deployment-owned: the control is hidden and the
 * value is left out of the save payload so the request never tries to change it.
 */
async function renderLoaded() {
  const result = render(SpeakerDetectionSettings);
  await waitFor(() => expect(result.container.querySelector('#speaker-behavior')).not.toBeNull());
  return result;
}

async function saveAfterBehaviorChange(container: HTMLElement) {
  const select = container.querySelector('#speaker-behavior') as HTMLSelectElement;
  await fireEvent.change(select, { target: { value: 'use_defaults' } });
  await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);
  await waitFor(() => expect(api.updateTranscriptionSettings).toHaveBeenCalledOnce());
  return api.updateTranscriptionSettings.mock.calls[0][0] as Record<string, unknown>;
}

describe('SpeakerDetectionSettings deployment lock', () => {
  it('shows the detection source by default and saves it', async () => {
    const { container } = await renderLoaded();
    expect(container.querySelector('#diarization-source')).not.toBeNull();
    expect(await saveAfterBehaviorChange(container)).toHaveProperty('diarization_source');
  });

  it('hides and does not send the detection source when locked', async () => {
    lock({ 'transcription.diarization_source': false });
    const { container } = await renderLoaded();
    expect(container.querySelector('#diarization-source')).toBeNull();
    const payload = await saveAfterBehaviorChange(container);
    expect(payload.speaker_prompt_behavior).toBe('use_defaults');
    expect(payload).not.toHaveProperty('diarization_source');
  });
});
