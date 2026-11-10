/**
 * Factories for the ASR settings suites' `$lib/api/*` module mocks. `vi.mock` is hoisted and stays
 * in each test file; the factory bodies live here: `vi.mock('$lib/api/localModels', async () =>
 * (await import('$testMocks/asrMocks')).localModelsApiMock())`.
 */
import { vi } from 'vitest';

/** `$lib/api/localModels` serving a one-model catalog (`tiny`, not downloaded). */
export const localModelsApiMock = () => ({
  getLocalModelCatalog: vi.fn().mockResolvedValue({
    models: [
      {
        id: 'tiny',
        display_name: 'tiny',
        repo_id: 'Systran/faster-whisper-tiny',
        approx_bytes: 75_000_000,
        vram_hint_gb: 1,
        language_support: 'multilingual',
        supports_translation: true,
        is_default: false,
        downloaded: false,
        partial: false,
        present_bytes: 0,
        is_active: false,
      },
    ],
    download: null,
    free_bytes: 50_000_000_000,
    cache_writable: true,
    active_model: 'large-v3-turbo',
  }),
  startLocalModelDownload: vi.fn(),
  cancelLocalModelDownload: vi.fn(),
});

/** `$lib/api/transcriptionSettings` with the provider as diarization source. */
export const transcriptionSettingsApiMock = () => ({
  getTranscriptionSettings: vi.fn().mockResolvedValue({ diarization_source: 'provider' }),
});
