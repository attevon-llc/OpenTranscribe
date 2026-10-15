import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));
vi.mock('$stores/locale', () => {
  const t = (key: string) => key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});

import axiosInstance from '$lib/axios';
import EngineSettings from './EngineSettings.svelte';
import { fullLocalView, liteCloudView } from '$lib/settings/pipelineViewFixtures';

const get = vi.mocked(axiosInstance.get);
const SETTINGS = {
  diarizer_backend: { value: 'native', source: 'db' },
  diarizer_require_sidecar: { value: false, source: 'default' },
  boundary_smoothing_enabled: { value: true, source: 'default' },
  boundary_acoustic_recheck_enabled: { value: false, source: 'default' },
  boundary_acoustic_cosine_margin: { value: 0.05, source: 'default' },
  boundary_acoustic_max_word_dur: { value: 1.0, source: 'default' },
};

function serve(view: unknown) {
  get.mockImplementation(async (url: string) => ({
    data: url === '/user-settings/pipeline' ? view : SETTINGS,
  }));
}

beforeEach(() => vi.clearAllMocks());

describe('EngineSettings on lite', () => {
  it('replaces the local speaker engine controls with the read-only reason', async () => {
    serve(liteCloudView());
    const { container } = render(EngineSettings);
    await waitFor(() =>
      expect(container.querySelector('[data-testid="engine-lite-note"]')?.textContent).toContain(
        'settings.pipeline.issue.local_diarization_unavailable'
      )
    );
    expect(container.querySelector('#diarizer-backend')).toBeNull();
    // Boundary smoothing still applies to any diarized transcript, so it stays editable.
    expect(container.querySelector('#boundary-smoothing-input')).not.toBeNull();
    expect(container.querySelector('#diarizer-require-sidecar-input')).toBeNull();
  });

  it('keeps the engine select on a full deployment', async () => {
    serve(fullLocalView());
    const { container } = render(EngineSettings);
    await waitFor(() => expect(container.querySelector('#diarizer-backend')).not.toBeNull());
    expect(container.querySelector('[data-testid="engine-lite-note"]')).toBeNull();
  });
});
