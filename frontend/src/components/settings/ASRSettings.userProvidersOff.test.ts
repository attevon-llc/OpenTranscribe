/**
 * With `asr.user_providers` off the Provider tab still pins the local model (#1207).
 *
 * The provider list, status and policy endpoints are 404 then, so the panel must not call them,
 * and must not render the bring-your-own provider controls; the local model and (for a super
 * admin) the model library stay.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
  getProviders: vi.fn(),
  getSettings: vi.fn(),
  getActiveLocalModel: vi.fn(),
  getStatus: vi.fn(),
}));

vi.mock('$lib/api/asrSettings', () => ({
  ASRSettingsApi: {
    ...api,
    getProviderDisplayName: (p: string) => p,
    setLocalModel: vi.fn(),
    restartGpuWorker: vi.fn(),
  },
}));

vi.mock('$lib/api/localModels', async () =>
  (await import('$testMocks/asrMocks')).localModelsApiMock()
);
vi.mock('$lib/api/transcriptionSettings', async () =>
  (await import('$testMocks/asrMocks')).transcriptionSettingsApiMock()
);
vi.mock('$components/settings/asr/CloudProviderPolicy.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyColonVarsLocale()
);

import ASRSettings from './ASRSettings.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  api.getProviders.mockResolvedValue({ providers: [] });
  api.getSettings.mockResolvedValue({
    configurations: [],
    shared_configurations: [],
    active_configuration_id: undefined,
    total: 0,
  });
  api.getStatus.mockResolvedValue({ deployment_mode: 'full' });
  api.getActiveLocalModel.mockResolvedValue({
    active_model: 'large-v3-turbo',
    source: 'environment',
    available_models: [],
    model_info: {
      display_name: 'Large v3 Turbo',
      description: 'd',
      supports_diarization: true,
      supports_translation: false,
      language_support: 'english_optimized',
    },
  });
});

describe('ASRSettings without bring-your-own providers', () => {
  it('keeps the local model and its library, and never calls the provider endpoints', async () => {
    render(ASRSettings, {
      props: { isAdmin: true, isSuperAdmin: true, userProvidersCap: false },
    });

    expect(await screen.findByTestId('local-model-tiny')).toBeInTheDocument();
    expect(api.getActiveLocalModel).toHaveBeenCalledTimes(1);
    expect(api.getProviders).not.toHaveBeenCalled();
    expect(api.getSettings).not.toHaveBeenCalled();
    expect(api.getStatus).not.toHaveBeenCalled();
    expect(screen.queryByTestId('in-use-flags')).toBeNull();
    expect(screen.queryByText('settings.asrProvider.addConfig')).toBeNull();
  });

  it('shows the provider controls and calls their endpoints when the capability is on', async () => {
    render(ASRSettings, { props: { isAdmin: false, userProvidersCap: true } });

    expect(await screen.findByTestId('in-use-flags')).toBeInTheDocument();
    expect(api.getProviders).toHaveBeenCalledTimes(1);
    expect(api.getSettings).toHaveBeenCalledTimes(1);
    expect(screen.getAllByText('settings.asrProvider.addConfig').length).toBeGreaterThan(0);
  });

  it('does not ask for the local model when model selection is off', async () => {
    render(ASRSettings, { props: { isAdmin: false, modelSelectionCap: false } });

    expect(await screen.findByTestId('in-use-flags')).toBeInTheDocument();
    expect(api.getActiveLocalModel).not.toHaveBeenCalled();
    expect(screen.queryByTestId('local-model-library')).toBeNull();
  });
});
