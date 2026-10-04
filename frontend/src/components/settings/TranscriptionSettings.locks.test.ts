/**
 * Deployment-owned transcription settings (`transcription.diarization_source`,
 * `transcription.advanced`): hidden in the panel, and left out of the save
 * payload so the request never tries to change a value the server ignores.
 */
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
  ASRSettingsApi: { getStatus: vi.fn().mockResolvedValue({}) },
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

import TranscriptionSettings from './TranscriptionSettings.svelte';
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

async function renderLoaded() {
  const result = render(TranscriptionSettings);
  await waitFor(() =>
    expect(result.container.querySelector('#llm-output-language')).not.toBeNull()
  );
  return result;
}

async function saveAfterLanguageChange(container: HTMLElement) {
  const select = container.querySelector('#llm-output-language') as HTMLSelectElement;
  await fireEvent.change(select, { target: { value: 'de' } });
  await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);
  await waitFor(() => expect(api.updateTranscriptionSettings).toHaveBeenCalledOnce());
  return api.updateTranscriptionSettings.mock.calls[0][0] as Record<string, unknown>;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS });
  api.getTranscriptionSystemDefaults.mockResolvedValue({
    available_source_languages: { auto: 'Auto-detect', en: 'English' },
    available_llm_output_languages: { en: 'English', de: 'German' },
    common_languages: ['auto', 'en'],
  });
  api.updateTranscriptionSettings.mockImplementation(async (u) => ({ ...SETTINGS, ...u }));
});

afterEach(() => resetCapabilities());

describe('TranscriptionSettings deployment locks', () => {
  it('shows the diarization source and Advanced block by default and saves them', async () => {
    const { container } = await renderLoaded();
    expect(container.querySelector('#diarization-source')).not.toBeNull();
    expect(container.querySelector('.collapsible-header')).not.toBeNull();

    const payload = await saveAfterLanguageChange(container);
    expect(payload).toHaveProperty('diarization_source');
    expect(payload).toHaveProperty('vad_threshold');
  });

  it('hides and does not send locked settings', async () => {
    lock({ 'transcription.diarization_source': false, 'transcription.advanced': false });
    const { container } = await renderLoaded();
    expect(container.querySelector('#diarization-source')).toBeNull();
    expect(container.querySelector('.collapsible-header')).toBeNull();

    const payload = await saveAfterLanguageChange(container);
    expect(payload.llm_output_language).toBe('de');
    expect(payload).not.toHaveProperty('diarization_source');
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
