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
import { resetCapabilities } from '$stores/capabilities';

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

async function renderLoaded() {
  const result = render(TranscriptionAccuracySettings);
  await waitFor(() => expect(result.container.querySelector('.btn-primary')).not.toBeNull());
  return result;
}

describe('TranscriptionAccuracySettings', () => {
  it('renders the VAD and accuracy cards open, with no collapsible', async () => {
    const { container } = await renderLoaded();
    for (const id of [
      '#vad-threshold',
      '#vad-min-silence',
      '#vad-min-speech',
      '#vad-speech-pad',
      '#hallucination-toggle',
      '#repetition-penalty',
    ]) {
      expect(container.querySelector(id), id).not.toBeNull();
    }
    expect(container.querySelector('.collapsible-header')).toBeNull();
  });

  it('saves only cleanup, VAD, hallucination and repetition fields', async () => {
    const { container } = await renderLoaded();
    await fireEvent.input(container.querySelector('#vad-min-silence') as HTMLInputElement, {
      target: { value: '1500' },
    });
    await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);
    await waitFor(() => expect(api.updateTranscriptionSettings).toHaveBeenCalledOnce());
    const payload = api.updateTranscriptionSettings.mock.calls[0][0] as Record<string, unknown>;
    expect(Object.keys(payload).sort()).toEqual([
      'garbage_cleanup_enabled',
      'garbage_cleanup_threshold',
      'hallucination_silence_threshold',
      'repetition_penalty',
      'vad_min_silence_ms',
      'vad_min_speech_ms',
      'vad_speech_pad_ms',
      'vad_threshold',
    ]);
    expect(payload.vad_min_silence_ms).toBe(1500);
    expect(payload.hallucination_silence_threshold).toBeNull();
  });

  it('resets only the accuracy group', async () => {
    api.resetTranscriptionSettings.mockResolvedValue({ message: 'ok', default_settings: SETTINGS });
    const { container } = await renderLoaded();
    await fireEvent.click(container.querySelector('.btn-secondary') as HTMLButtonElement);
    await waitFor(() => expect(api.resetTranscriptionSettings).toHaveBeenCalledWith('accuracy'));
  });

  it('blocks saving an out-of-range cleanup threshold', async () => {
    const { container } = await renderLoaded();
    await fireEvent.input(container.querySelector('input[type="number"]') as HTMLInputElement, {
      target: { value: '5' },
    });
    expect(container.querySelector('.validation-error')?.textContent).toContain(
      'settings.transcription.validationThresholdRange'
    );
    expect((container.querySelector('.btn-primary') as HTMLButtonElement).disabled).toBe(true);
  });
});
