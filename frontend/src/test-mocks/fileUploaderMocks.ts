/**
 * Shared module-mock factories and DOM helpers for the `FileUploader` wizard tests. `vi.mock` is
 * hoisted and must stay in each test file; the factory bodies live here, e.g.
 * `vi.mock('$lib/api/tags', async () => (await import('$testMocks/fileUploaderMocks')).tagsApiMock())`.
 * This module imports no app store, so a factory may load it (see `settingsModalMocks.ts`).
 */
import { expect, vi } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/svelte';

/** `configService` with no protected-media auth. */
export const configServiceMock = () => ({
  loadProtectedMediaAuthConfig: vi.fn().mockResolvedValue(undefined),
  getAuthConfigForUrl: vi.fn().mockReturnValue(null),
});

export const AUDIO_EXTRACTION_SETTINGS = {
  auto_extract_enabled: true,
  extraction_threshold_mb: 100,
  remember_choice: false,
  show_modal: true,
};

/** The audio-extraction settings API, answering the defaults above. */
export const audioExtractionSettingsMock = () => ({
  getAudioExtractionSettings: vi.fn().mockResolvedValue({ ...AUDIO_EXTRACTION_SETTINGS }),
});

/** The ASR settings API with one active local model. */
export const asrActiveModelMock = (activeModel = 'large-v3-turbo') => ({
  ASRSettingsApi: {
    getActiveLocalModel: vi.fn().mockResolvedValue({ active_model: activeModel }),
  },
});

/** An account with no tags yet. */
export const tagsApiMock = () => ({ listTags: vi.fn().mockResolvedValue([]) });

/** What `getTranscriptionSystemDefaults` returns: a plain two-language deployment. */
export const TRANSCRIPTION_SYSTEM_DEFAULTS = {
  min_speakers: 1,
  max_speakers: 20,
  garbage_cleanup_enabled: true,
  garbage_cleanup_threshold: 50,
  valid_speaker_prompt_behaviors: ['always_prompt', 'use_defaults', 'use_custom'],
  available_source_languages: { auto: 'Auto-detect', en: 'English' },
  available_llm_output_languages: { en: 'English' },
  common_languages: ['auto', 'en'],
  vad_threshold: 0.5,
  vad_min_silence_ms: 2000,
  vad_min_speech_ms: 250,
  vad_speech_pad_ms: 400,
  hallucination_silence_threshold: null,
  repetition_penalty: 1.0,
  diarization_source_default: 'provider',
  valid_diarization_sources: ['provider', 'local', 'pyannote', 'off'],
};

/** Hands `files` to the wizard's file input as if the user had picked them. */
export async function selectFiles(container: HTMLElement, files: File[]) {
  await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, 'files', { configurable: true, value: files });
  await fireEvent.change(input);
}

/** Single-file form of `selectFiles`. */
export const selectFile = (container: HTMLElement, selected: File) =>
  selectFiles(container, [selected]);

/** A `File` whose reported size is overridden (no need to allocate the bytes). */
export function sizedFile(name: string, type: string, size?: number): File {
  const f = new File(['x'], name, { type });
  if (size !== undefined) Object.defineProperty(f, 'size', { configurable: true, value: size });
  return f;
}
