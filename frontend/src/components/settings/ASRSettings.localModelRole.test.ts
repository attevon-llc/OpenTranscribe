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
    setLocalModel: vi.fn(),
    restartGpuWorker: vi.fn(),
  },
}));

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => (run((k, params) => (params ? `${k}:${JSON.stringify(params)}` : k)), () => {}),
  },
}));

import ASRSettings from './ASRSettings.svelte';

describe('ASRSettings - local model controls by role', () => {
  beforeEach(() => vi.clearAllMocks());

  it('locks the model select and restart button for a plain admin, with a tooltip', async () => {
    render(ASRSettings, { props: { isAdmin: true, isSuperAdmin: false } });
    const select = await screen.findByLabelText<HTMLSelectElement>(
      'settings.asrProvider.activeModel'
    );
    expect(select).toBeDisabled();
    expect(select).toHaveAttribute('title', 'settings.nav.requiresSuperAdmin');
    const restart = screen.getByRole('button', { name: /settings.asrProvider.restartGpuWorker/ });
    expect(restart).toBeDisabled();
    expect(restart).toHaveAttribute('title', 'settings.nav.requiresSuperAdmin');
    expect(screen.getByText('settings.asrProvider.localModelLockedHint')).toBeInTheDocument();
  });

  it('enables the controls for a super admin', async () => {
    render(ASRSettings, { props: { isAdmin: true, isSuperAdmin: true } });
    const select = await screen.findByLabelText<HTMLSelectElement>(
      'settings.asrProvider.activeModel'
    );
    expect(select).toBeEnabled();
    expect(
      screen.getByRole('button', { name: /settings.asrProvider.restartGpuWorker/ })
    ).toBeEnabled();
    expect(screen.queryByText('settings.asrProvider.localModelLockedHint')).toBeNull();
  });

  it('translates the language-support badge', async () => {
    render(ASRSettings, { props: { isAdmin: false } });
    expect(await screen.findByText('settings.asrProvider.capEnglishOptimized')).toBeInTheDocument();
    expect(screen.queryByText('English optimized')).toBeNull();
  });
});
