import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
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
import { toastStore } from '$stores/toast';
import EngineSettings from './EngineSettings.svelte';

const get = vi.mocked(axiosInstance.get);
const del = vi.mocked(axiosInstance.delete);

function makeSettings() {
  return {
    diarizer_backend: { value: 'native', source: 'db' },
    diarizer_require_sidecar: { value: false, source: 'default' },
    boundary_smoothing_enabled: { value: true, source: 'default' },
    boundary_acoustic_recheck_enabled: { value: false, source: 'default' },
    boundary_acoustic_cosine_margin: { value: 0.05, source: 'default' },
    boundary_acoustic_max_word_dur: { value: 1.0, source: 'default' },
  };
}

describe('EngineSettings - dirty state, translated options, headings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue({ data: makeSettings() });
  });

  it('dispatches change with hasChanges true after an edit, false when reverted', async () => {
    const changes: boolean[] = [];
    render(EngineSettings, {
      events: {
        change: (e: CustomEvent<{ hasChanges: boolean }>) => changes.push(e.detail.hasChanges),
      },
    } as never);

    const select = await screen.findByLabelText<HTMLSelectElement>(
      'settings.engineSettings.diarizerBackend'
    );
    await fireEvent.change(select, { target: { value: 'pyannote' } });
    await waitFor(() => expect(changes.at(-1)).toBe(true));

    await fireEvent.change(select, { target: { value: 'native' } });
    await waitFor(() => expect(changes.at(-1)).toBe(false));
  });

  it('renders translation keys for the backend options, not hardcoded English', async () => {
    render(EngineSettings);
    await screen.findByLabelText('settings.engineSettings.diarizerBackend');
    const labels = screen.getAllByRole('option').map((o) => o.textContent?.trim());
    expect(labels).toEqual([
      'settings.engineSettings.backendNative',
      'settings.engineSettings.backendPyannote',
    ]);
  });

  it('renders the two group headings', async () => {
    const { container } = render(EngineSettings);
    await screen.findByLabelText('settings.engineSettings.diarizerBackend');
    const headings = Array.from(container.querySelectorAll('.section-title')).map(
      (h) => h.textContent?.trim()
    );
    expect(headings).toEqual([
      'settings.engineSettings.diarizerHeading',
      'settings.engineSettings.boundaryHeading',
    ]);
  });

  it('disables the re-check thresholds while the acoustic re-check is off', async () => {
    render(EngineSettings);
    const margin = await screen.findByLabelText<HTMLInputElement>(
      'settings.engineSettings.boundaryAcousticCosineMargin'
    );
    expect(margin).toBeDisabled();
    expect(
      screen.getByLabelText<HTMLInputElement>('settings.engineSettings.boundaryAcousticMaxWordDur')
    ).toBeDisabled();
  });

  it('translates the reset failure fallback', async () => {
    del.mockRejectedValue({});
    render(EngineSettings);
    await screen.findByLabelText('settings.engineSettings.diarizerBackend');
    await fireEvent.click(
      screen.getAllByRole('button', { name: 'settings.engineSettings.resetKey' })[0]
    );
    await waitFor(() => expect(toastStore.error).toHaveBeenCalled());
    expect(vi.mocked(toastStore.error).mock.calls[0][0]).toBe(
      'settings.engineSettings.resetFailed:{"key":"diarizer_backend"}'
    );
  });
});
