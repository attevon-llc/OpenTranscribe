import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { LLMStatusState } from '$stores/llmStatus';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});

vi.mock('$lib/api/speakerAttributeSettings', () => ({
  getSpeakerAttributeSettings: vi.fn().mockResolvedValue({
    detection_enabled: true,
    gender_detection_enabled: true,
    show_attributes_on_cards: true,
    auto_llm_speaker_identification: true,
  }),
  updateSpeakerAttributeSettings: vi.fn(),
  resetSpeakerAttributeSettings: vi.fn(),
}));

// The gender-lock read goes through the privacy client; the migration-status guard below is about
// the bulk panel's route, so keep this off the shared axios mock.
vi.mock('$lib/api/privacy', () => ({
  PrivacyApi: { getEffective: vi.fn() },
}));

const llmState = writable<LLMStatusState>({
  status: null,
  available: true,
  checking: false,
  lastChecked: new Date(),
});
vi.mock('$stores/llmStatus', () => ({
  llmStatusStore: {
    subscribe: (run: (value: LLMStatusState) => void) => llmState.subscribe(run),
    initialize: vi.fn().mockResolvedValue(undefined),
  },
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
import { PrivacyApi } from '$lib/api/privacy';
import SpeakerAttributeSettings from './SpeakerAttributeSettings.svelte';

describe('SpeakerAttributeSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: {} });
    vi.mocked(getSpeakerAttributeSettings).mockResolvedValue({
      detection_enabled: true,
      gender_detection_enabled: true,
      show_attributes_on_cards: true,
      auto_llm_speaker_identification: true,
    });
    llmState.set({ status: null, available: true, checking: false, lastChecked: new Date() });
  });

  it('renders no bulk section and never requests migration status', async () => {
    const { container } = render(SpeakerAttributeSettings);
    await screen.findByLabelText('settings.speakerAttributes.enableDetection');
    expect(container.querySelector('.bulk-section')).toBeNull();
    expect(axiosInstance.get).not.toHaveBeenCalled();
  });

  it('renders a .section-title heading and says name suggestions are independent', async () => {
    const { container } = render(SpeakerAttributeSettings);
    await screen.findByLabelText('settings.speakerAttributes.enableDetection');
    expect(container.querySelector('h3.section-title')?.textContent).toBe(
      'settings.speakerAttributes.title'
    );
    expect(screen.getByText('settings.speakerAttributes.llmIndependentNote')).toBeInTheDocument();
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
      auto_llm_speaker_identification: true,
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

  describe('automatic AI speaker names switch (#1148)', () => {
    const NAMES = 'settings.speakerAttributes.autoLlmNames';

    it('renders on by default with the privacy and manual-button wording', async () => {
      render(SpeakerAttributeSettings);
      const toggle = (await screen.findByLabelText(NAMES)) as HTMLInputElement;
      expect(toggle.checked).toBe(true);
      expect(toggle.disabled).toBe(false);
      expect(screen.getByText(`${NAMES}Desc`)).toBeInTheDocument();
    });

    it('saves only its own field', async () => {
      vi.mocked(updateSpeakerAttributeSettings).mockResolvedValue({
        detection_enabled: true,
        gender_detection_enabled: true,
        show_attributes_on_cards: true,
        auto_llm_speaker_identification: false,
      });
      render(SpeakerAttributeSettings);
      await fireEvent.click(await screen.findByLabelText(NAMES));
      await fireEvent.click(
        screen.getByRole('button', { name: 'settings.speakerAttributes.save' })
      );
      await waitFor(() => expect(updateSpeakerAttributeSettings).toHaveBeenCalledTimes(1));
      expect(updateSpeakerAttributeSettings).toHaveBeenCalledWith({
        auto_llm_speaker_identification: false,
      });
    });

    it('reports dirty state when only this switch changes', async () => {
      const changes: boolean[] = [];
      render(SpeakerAttributeSettings, {
        events: {
          change: (e: CustomEvent<{ hasChanges: boolean }>) => changes.push(e.detail.hasChanges),
        },
      } as never);
      await fireEvent.click(await screen.findByLabelText(NAMES));
      await waitFor(() => expect(changes.at(-1)).toBe(true));
    });

    it('is disabled with an explanation when no AI provider is configured', async () => {
      llmState.set({ status: null, available: false, checking: false, lastChecked: new Date() });
      render(SpeakerAttributeSettings);
      const toggle = (await screen.findByLabelText(NAMES)) as HTMLInputElement;
      expect(toggle.disabled).toBe(true);
      expect(screen.getByTestId('auto-llm-no-provider')).toBeInTheDocument();
    });
  });
});

describe('gender inference notice and lock (issue #1280)', () => {
  const GENDER = 'settings.speakerAttributes.genderDetection';
  const policy = (locked_by: string | null) => ({
    organization_uuid: null,
    voiceprint_matching: { enabled: true, locked_by: null },
    gender_inference: { enabled: locked_by === null, locked_by },
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getSpeakerAttributeSettings).mockResolvedValue({
      detection_enabled: true,
      gender_detection_enabled: true,
      show_attributes_on_cards: true,
      auto_llm_speaker_identification: true,
    });
    vi.mocked(PrivacyApi.getEffective).mockResolvedValue(policy(null) as never);
    llmState.set({ status: null, available: true, checking: false, lastChecked: new Date() });
  });

  it('says beside the switch that gender is inferred automatically and can be turned off', async () => {
    render(SpeakerAttributeSettings);
    const toggle = (await screen.findByLabelText(GENDER)) as HTMLInputElement;
    expect(screen.getByTestId('gender-inference-notice')).toHaveTextContent(
      'privacy.gender.settingsNotice'
    );
    expect(toggle.disabled).toBe(false);
    expect(toggle.checked).toBe(true);
    expect(toggle.getAttribute('aria-describedby')).toContain('gender-notice');
  });

  it('can still be turned off and saved by the person', async () => {
    vi.mocked(updateSpeakerAttributeSettings).mockResolvedValue({
      detection_enabled: true,
      gender_detection_enabled: false,
      show_attributes_on_cards: true,
      auto_llm_speaker_identification: true,
    });
    render(SpeakerAttributeSettings);
    await fireEvent.click(await screen.findByLabelText(GENDER));
    await fireEvent.click(screen.getByRole('button', { name: 'settings.speakerAttributes.save' }));
    await waitFor(() =>
      expect(updateSpeakerAttributeSettings).toHaveBeenCalledWith({
        gender_detection_enabled: false,
      })
    );
  });

  it.each(['organization', 'instance', 'platform'])(
    'shows the switch off and disabled with the reason when the %s has it off',
    async (layer) => {
      vi.mocked(PrivacyApi.getEffective).mockResolvedValue(policy(layer) as never);
      render(SpeakerAttributeSettings);
      await screen.findByLabelText(GENDER);
      await waitFor(() => expect(screen.getByTestId('gender-locked')).toBeInTheDocument());
      const toggle = screen.getByLabelText(GENDER) as HTMLInputElement;
      expect(toggle.disabled).toBe(true);
      expect(toggle.checked).toBe(false);
      expect(screen.getByTestId('gender-locked')).toHaveTextContent(`privacy.lockedBy.${layer}`);
    }
  );

  it('does not lock the switch for the person own off state', async () => {
    vi.mocked(PrivacyApi.getEffective).mockResolvedValue(policy('user') as never);
    render(SpeakerAttributeSettings);
    const toggle = (await screen.findByLabelText(GENDER)) as HTMLInputElement;
    await waitFor(() => expect(PrivacyApi.getEffective).toHaveBeenCalled());
    expect(screen.queryByTestId('gender-locked')).toBeNull();
    expect(toggle.disabled).toBe(false);
  });

  it('leaves the switch usable when the policy cannot be read (the server still enforces it)', async () => {
    vi.mocked(PrivacyApi.getEffective).mockRejectedValue(new Error('501'));
    render(SpeakerAttributeSettings);
    const toggle = (await screen.findByLabelText(GENDER)) as HTMLInputElement;
    await waitFor(() => expect(PrivacyApi.getEffective).toHaveBeenCalled());
    expect(toggle.disabled).toBe(false);
  });
});
