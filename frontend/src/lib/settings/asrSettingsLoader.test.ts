import { beforeEach, describe, expect, it, vi } from 'vitest';

const { api, getTranscriptionSettings } = vi.hoisted(() => ({
  api: {
    getProviders: vi.fn(),
    getSettings: vi.fn(),
    getActiveLocalModel: vi.fn(),
    getStatus: vi.fn(),
  },
  getTranscriptionSettings: vi.fn(),
}));

vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: api }));
vi.mock('$lib/api/transcriptionSettings', () => ({ getTranscriptionSettings }));

import { loadAsrSettings } from './asrSettingsLoader';

const both = { userProviders: true, modelSelection: true };

beforeEach(() => {
  vi.clearAllMocks();
  api.getProviders.mockResolvedValue({ providers: [{ id: 'local' }] });
  api.getSettings.mockResolvedValue({
    configurations: [{ uuid: 'c1' }],
    shared_configurations: undefined,
    active_configuration_id: 'c1',
  });
  api.getActiveLocalModel.mockResolvedValue({ active_model: 'large-v3', model_info: undefined });
  api.getStatus.mockResolvedValue({ deployment_mode: 'full' });
  getTranscriptionSettings.mockResolvedValue({
    diarization_source: 'local',
    diarization_asr_config_uuid: null,
  });
});

describe('loadAsrSettings', () => {
  it('shapes one round of reads for the panel', async () => {
    const loaded = await loadAsrSettings(both);
    expect(loaded).toMatchObject({
      providers: [{ id: 'local' }],
      configurations: [{ uuid: 'c1' }],
      sharedConfigurations: [],
      activeConfigurationId: 'c1',
      usingLocalDefault: false,
      isLite: false,
      diarizationSource: 'local',
      diarizerConfigUuid: null,
    });
    expect(loaded.localModel?.active_model).toBe('large-v3');
  });

  it('uses the local default when nothing is active or nothing is saved', async () => {
    api.getSettings.mockResolvedValue({
      configurations: [{ uuid: 'c1' }],
      active_configuration_id: null,
    });
    expect((await loadAsrSettings(both)).usingLocalDefault).toBe(true);
    api.getSettings.mockResolvedValue({ configurations: [], active_configuration_id: 'c1' });
    expect((await loadAsrSettings(both)).usingLocalDefault).toBe(true);
  });

  it('does not request what the deployment turned off', async () => {
    const loaded = await loadAsrSettings({ userProviders: false, modelSelection: false });
    expect(api.getProviders).not.toHaveBeenCalled();
    expect(api.getSettings).not.toHaveBeenCalled();
    expect(api.getStatus).not.toHaveBeenCalled();
    expect(api.getActiveLocalModel).not.toHaveBeenCalled();
    expect(loaded).toMatchObject({
      providers: [],
      configurations: [],
      localModel: null,
      isLite: false,
    });
  });

  it('degrades the reads that may fail instead of failing the panel', async () => {
    api.getSettings.mockRejectedValue(new Error('x'));
    api.getActiveLocalModel.mockRejectedValue(new Error('x'));
    api.getStatus.mockRejectedValue(new Error('x'));
    getTranscriptionSettings.mockRejectedValue(new Error('x'));
    const loaded = await loadAsrSettings(both);
    expect(loaded).toMatchObject({
      configurations: [],
      usingLocalDefault: true,
      localModel: null,
      isLite: false,
      diarizationSource: null,
    });
  });

  it('lets a provider catalogue failure fail the load', async () => {
    api.getProviders.mockRejectedValue(new Error('down'));
    await expect(loadAsrSettings(both)).rejects.toThrow('down');
  });
});
