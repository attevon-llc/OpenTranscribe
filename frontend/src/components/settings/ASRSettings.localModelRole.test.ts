import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$lib/api/asrSettings', () => ({
  ASRSettingsApi: {
    getProviders: vi.fn().mockResolvedValue({ providers: [] }),
    getSettings: vi.fn().mockResolvedValue({
      configurations: [],
      shared_configurations: [],
      active_configuration_id: undefined,
      total: 0,
    }),
    getActiveLocalModel: vi.fn().mockResolvedValue({
      active_model: 'large-v3-turbo',
      source: 'environment',
      available_models: [{ short_name: 'large-v3-turbo' }, { short_name: 'small' }],
      model_info: {
        display_name: 'Large v3 Turbo',
        description: 'd',
        supports_diarization: true,
        supports_translation: false,
        language_support: 'english_optimized',
      },
    }),
    getStatus: vi.fn().mockResolvedValue({ deployment_mode: 'full' }),
    setLocalModel: vi.fn(),
    restartGpuWorker: vi.fn(),
  },
}));

vi.mock('$lib/api/localModels', () => ({
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
}));

vi.mock('$lib/api/transcriptionSettings', () => ({
  getTranscriptionSettings: vi.fn().mockResolvedValue({ diarization_source: 'provider' }),
}));

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => (run((k, params) => (params ? `${k}:${JSON.stringify(params)}` : k)), () => {}),
  },
}));

import ASRSettings from './ASRSettings.svelte';
import { getLocalModelCatalog } from '$lib/api/localModels';

describe('ASRSettings - local model library by role', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows a plain admin the library locked and requests nothing from it', async () => {
    render(ASRSettings, { props: { isAdmin: true, isSuperAdmin: false } });
    expect(await screen.findByTestId('local-model-locked-hint')).toHaveTextContent(
      'settings.localModels.locked'
    );
    expect(getLocalModelCatalog).not.toHaveBeenCalled();
  });

  it('loads the catalog for a super admin', async () => {
    render(ASRSettings, { props: { isAdmin: true, isSuperAdmin: true } });
    expect(await screen.findByTestId('local-model-tiny')).toBeInTheDocument();
    expect(getLocalModelCatalog).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('local-model-locked-hint')).toBeNull();
  });

  it('never shows the library to a plain user', async () => {
    render(ASRSettings, { props: { isAdmin: false } });
    expect(await screen.findByText('settings.asrProvider.capEnglishOptimized')).toBeInTheDocument();
    expect(screen.queryByTestId('local-model-library')).toBeNull();
    expect(getLocalModelCatalog).not.toHaveBeenCalled();
  });

  it('translates the language-support badge', async () => {
    render(ASRSettings, { props: { isAdmin: false } });
    expect(await screen.findByText('settings.asrProvider.capEnglishOptimized')).toBeInTheDocument();
    expect(screen.queryByText('English optimized')).toBeNull();
  });
});
