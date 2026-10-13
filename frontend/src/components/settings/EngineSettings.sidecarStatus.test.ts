import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}),
  },
}));

import axiosInstance from '$lib/axios';
import EngineSettings from './EngineSettings.svelte';

const get = vi.mocked(axiosInstance.get);

function makeSettings(extra: Record<string, unknown> = {}) {
  return {
    diarizer_backend: { value: 'native', source: 'default' },
    diarizer_require_sidecar: { value: false, source: 'default' },
    boundary_smoothing_enabled: { value: true, source: 'default' },
    boundary_acoustic_recheck_enabled: { value: false, source: 'default' },
    boundary_acoustic_cosine_margin: { value: 0.05, source: 'default' },
    boundary_acoustic_max_word_dur: { value: 1.0, source: 'default' },
    ...extra,
  };
}

async function chip() {
  return waitFor(() => {
    const el = screen.getByTestId('sidecar-status');
    expect(el).toBeInTheDocument();
    return el;
  });
}

describe('EngineSettings sidecar status chip', () => {
  beforeEach(() => vi.clearAllMocks());

  it.each([
    ['reachable', 'settings.engineSettings.sidecarStatus.reachable'],
    ['unreachable', 'settings.engineSettings.sidecarStatus.unreachable'],
    ['unknown', 'settings.engineSettings.sidecarStatus.unknown'],
  ])('shows %s from the engine-settings response', async (status, label) => {
    get.mockResolvedValue({ data: makeSettings({ diarizer_sidecar_status: status }) });
    render(EngineSettings);

    const el = await chip();
    expect(el.dataset.status).toBe(status);
    expect(el.textContent).toContain(label);
  });

  it('reads a response without the field (older backend) as unknown, not as down', async () => {
    get.mockResolvedValue({ data: makeSettings() });
    render(EngineSettings);

    const el = await chip();
    expect(el.dataset.status).toBe('unknown');
  });

  it('explains what the switch is and is not before the controls', async () => {
    get.mockResolvedValue({ data: makeSettings({ diarizer_sidecar_status: 'reachable' }) });
    const { container } = render(EngineSettings);

    const intro = await waitFor(() => {
      const el = container.querySelector('[data-testid="engine-intro"]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    // One visible line; the longer explanation and the admin detail wait behind "How it works".
    expect(intro.textContent).toContain('settings.engineSettings.intro.summary');
    expect(intro.textContent).not.toContain('settings.engineSettings.intro.details');
    expect(intro.textContent).not.toContain('settings.engineSettings.intro.overrideAdmin');
    await fireEvent.click(
      screen.getByRole('button', { name: 'settings.engineSettings.intro.howItWorks' })
    );
    expect(intro.textContent).toContain('settings.engineSettings.intro.details');
    expect(intro.textContent).toContain('settings.engineSettings.intro.overrideAdmin');
    const backendSelect = container.querySelector('#diarizer-backend') as HTMLElement;
    expect(
      intro.compareDocumentPosition(backendSelect) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });
});
