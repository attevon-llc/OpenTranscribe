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

import { settingsModalStore } from '$stores/settingsModalStore';
import { get } from 'svelte/store';
import { resetCapabilities } from '$stores/capabilities';
import TranscriptionSettings from './TranscriptionSettings.svelte';

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

describe('TranscriptionSettings stand-in shell', () => {
  it('mounts the three forms, each with its own save and reset buttons', async () => {
    const { container } = render(TranscriptionSettings);
    await waitFor(() => expect(container.querySelector('#speaker-behavior')).not.toBeNull());
    for (const root of [
      '.transcription-language-settings',
      '.transcription-accuracy-settings',
      '.speaker-detection-settings',
    ]) {
      const form = container.querySelector(root);
      expect(form, root).not.toBeNull();
      expect(form?.querySelectorAll('.btn-primary')).toHaveLength(1);
      expect(form?.querySelectorAll('.btn-secondary')).toHaveLength(1);
    }
  });

  it('marks the modal section dirty while any one form has edits, and only then', async () => {
    const { container } = render(TranscriptionSettings);
    await waitFor(() => expect(container.querySelector('#speaker-behavior')).not.toBeNull());
    expect(get(settingsModalStore).dirtyState.transcription).toBe(false);

    await fireEvent.change(container.querySelector('#speaker-behavior') as HTMLSelectElement, {
      target: { value: 'use_defaults' },
    });
    await waitFor(() => expect(get(settingsModalStore).dirtyState.transcription).toBe(true));

    const speakers = container.querySelector('.speaker-detection-settings') as HTMLElement;
    await fireEvent.click(speakers.querySelector('.btn-primary') as HTMLButtonElement);
    await waitFor(() => expect(get(settingsModalStore).dirtyState.transcription).toBe(false));
  });
});
