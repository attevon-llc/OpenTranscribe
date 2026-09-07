import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return {
    default: axiosInstance,
    isRequestCancelled: () => false,
  };
});

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

import axiosInstance from '$lib/axios';
import EngineSettings from './EngineSettings.svelte';

const get = vi.mocked(axiosInstance.get);
const post = vi.mocked(axiosInstance.post);

function makeSettings(
  overrides: Partial<{ diarizer_require_sidecar: boolean; source: string }> = {}
) {
  return {
    diarizer_backend: { value: 'native', source: 'default' },
    diarizer_require_sidecar: {
      value: overrides.diarizer_require_sidecar ?? false,
      source: overrides.source ?? 'default',
    },
    boundary_smoothing_enabled: { value: true, source: 'default' },
    boundary_acoustic_recheck_enabled: { value: false, source: 'default' },
    boundary_acoustic_cosine_margin: { value: 0.05, source: 'default' },
    boundary_acoustic_max_word_dur: { value: 1.0, source: 'default' },
  };
}

describe('EngineSettings — diarizer_require_sidecar toggle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the toggle and reflects the current server value (off)', async () => {
    get.mockResolvedValue({ data: makeSettings({ diarizer_require_sidecar: false }) });

    render(EngineSettings);

    const input = await screen.findByLabelText<HTMLInputElement>(
      'settings.engineSettings.diarizerRequireSidecarHelp'
    );
    expect(input.checked).toBe(false);
  });

  it('reflects the current server value (on)', async () => {
    get.mockResolvedValue({
      data: makeSettings({ diarizer_require_sidecar: true, source: 'db' }),
    });

    render(EngineSettings);

    const input = await screen.findByLabelText<HTMLInputElement>(
      'settings.engineSettings.diarizerRequireSidecarHelp'
    );
    expect(input.checked).toBe(true);
  });

  it('toggling the checkbox and saving sends only the changed field', async () => {
    get.mockResolvedValue({ data: makeSettings({ diarizer_require_sidecar: false }) });
    post.mockResolvedValue({ data: makeSettings({ diarizer_require_sidecar: true }) } as never);

    render(EngineSettings);

    const input = await screen.findByLabelText<HTMLInputElement>(
      'settings.engineSettings.diarizerRequireSidecarHelp'
    );
    await fireEvent.click(input);

    const saveButton = screen.getByRole('button', { name: 'settings.engineSettings.save' });
    await waitFor(() => expect(saveButton).toBeEnabled());
    await fireEvent.click(saveButton);

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/admin/engine-settings/update', {
        diarizer_require_sidecar: true,
      })
    );
  });
});
