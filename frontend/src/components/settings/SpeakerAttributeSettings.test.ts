import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});

vi.mock('$lib/api/speakerAttributeSettings', () => ({
  getSpeakerAttributeSettings: vi.fn().mockResolvedValue({
    detection_enabled: true,
    gender_detection_enabled: true,
    show_attributes_on_cards: true,
  }),
  updateSpeakerAttributeSettings: vi.fn(),
  resetSpeakerAttributeSettings: vi.fn(),
}));

vi.mock('$stores/speakerAttributePrefs', () => ({ refreshSpeakerAttributePrefs: vi.fn() }));

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => (run((k, params) => (params ? `${k}:${JSON.stringify(params)}` : k)), () => {}),
  },
}));

import axiosInstance from '$lib/axios';
import {
  getSpeakerAttributeSettings,
  updateSpeakerAttributeSettings,
} from '$lib/api/speakerAttributeSettings';
import { refreshSpeakerAttributePrefs } from '$stores/speakerAttributePrefs';
import SpeakerAttributeSettings from './SpeakerAttributeSettings.svelte';

describe('SpeakerAttributeSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: {} });
    vi.mocked(getSpeakerAttributeSettings).mockResolvedValue({
      detection_enabled: true,
      gender_detection_enabled: true,
      show_attributes_on_cards: true,
    });
  });

  it('renders no bulk section and never requests migration status', async () => {
    const { container } = render(SpeakerAttributeSettings);
    await screen.findByLabelText('settings.speakerAttributes.enableDetection');
    expect(container.querySelector('.bulk-section')).toBeNull();
    expect(axiosInstance.get).not.toHaveBeenCalled();
  });

  it('renders a .section-title heading and the LLM coupling note', async () => {
    const { container } = render(SpeakerAttributeSettings);
    await screen.findByLabelText('settings.speakerAttributes.enableDetection');
    expect(container.querySelector('h3.section-title')?.textContent).toBe(
      'settings.speakerAttributes.title'
    );
    expect(screen.getByText('settings.speakerAttributes.llmCouplingNote')).toBeInTheDocument();
  });

  it('reports dirty state by dispatching change', async () => {
    const changes: boolean[] = [];
    render(SpeakerAttributeSettings, {
      events: {
        change: (e: CustomEvent<{ hasChanges: boolean }>) => changes.push(e.detail.hasChanges),
      },
    } as never);
    const toggle = await screen.findByLabelText('settings.speakerAttributes.enableDetection');
    await fireEvent.click(toggle);
    await waitFor(() => expect(changes.at(-1)).toBe(true));
  });

  it('refreshes the speaker-card preference right after a successful save', async () => {
    vi.mocked(updateSpeakerAttributeSettings).mockResolvedValue({
      detection_enabled: true,
      gender_detection_enabled: true,
      show_attributes_on_cards: false,
    });
    render(SpeakerAttributeSettings);
    await fireEvent.click(await screen.findByLabelText('settings.speakerAttributes.showOnCards'));
    await fireEvent.click(screen.getByRole('button', { name: 'settings.speakerAttributes.save' }));

    await waitFor(() => expect(refreshSpeakerAttributePrefs).toHaveBeenCalledWith(true));
  });

  it('does not refresh the preference when the save fails', async () => {
    vi.mocked(updateSpeakerAttributeSettings).mockRejectedValue(new Error('boom'));
    render(SpeakerAttributeSettings);
    await fireEvent.click(await screen.findByLabelText('settings.speakerAttributes.showOnCards'));
    await fireEvent.click(screen.getByRole('button', { name: 'settings.speakerAttributes.save' }));

    await screen.findByText('settings.speakerAttributes.saveFailed');
    expect(refreshSpeakerAttributePrefs).not.toHaveBeenCalled();
  });
});
