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

import TranscriptionAccuracySettings from './TranscriptionAccuracySettings.svelte';
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
 * `transcription.advanced` is deployment-owned: the VAD and accuracy cards are hidden and
 * their fields are left out of the save payload. Noise cleanup stays editable.
 */
async function renderLoaded() {
  const result = render(TranscriptionAccuracySettings);
  await waitFor(() => expect(result.container.querySelector('.btn-primary')).not.toBeNull());
  return result;
}

async function saveAfterCleanupChange(container: HTMLElement) {
  await fireEvent.input(container.querySelector('input[type="number"]') as HTMLInputElement, {
    target: { value: '80' },
  });
  await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);
  await waitFor(() => expect(api.updateTranscriptionSettings).toHaveBeenCalledOnce());
  return api.updateTranscriptionSettings.mock.calls[0][0] as Record<string, unknown>;
}

describe('TranscriptionAccuracySettings deployment lock', () => {
  it('shows the advanced cards by default and saves them', async () => {
    const { container } = await renderLoaded();
    expect(container.querySelector('#vad-threshold')).not.toBeNull();
    expect(await saveAfterCleanupChange(container)).toHaveProperty('vad_threshold');
  });

  it('hides and does not send locked settings', async () => {
    lock({ 'transcription.advanced': false });
    const { container } = await renderLoaded();
    expect(container.querySelector('#vad-threshold')).toBeNull();
    expect(container.querySelector('#repetition-penalty')).toBeNull();

    const payload = await saveAfterCleanupChange(container);
    expect(payload.garbage_cleanup_threshold).toBe(80);
    for (const key of [
      'vad_threshold',
      'vad_min_silence_ms',
      'vad_min_speech_ms',
      'vad_speech_pad_ms',
      'hallucination_silence_threshold',
      'repetition_penalty',
    ]) {
      expect(payload).not.toHaveProperty(key);
    }
  });
});
