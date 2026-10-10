import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';

const mockAxios = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: unknown) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

const api = vi.hoisted(() => ({
  getTranscriptionSettings: vi.fn(),
  getTranscriptionSystemDefaults: vi.fn(),
  updateTranscriptionSettings: vi.fn(),
  resetTranscriptionSettings: vi.fn(),
}));
vi.mock('$lib/api/transcriptionSettings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/transcriptionSettings')>();
  return { ...actual, ...api };
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

import SpeakerIdentificationSection from './SpeakerIdentificationSection.svelte';
import { settingsModalStore } from '$stores/settingsModalStore';
import { resetCapabilities } from '$stores/capabilities';

const SETTINGS = {
  min_speakers: 1,
  max_speakers: 20,
  speaker_prompt_behavior: 'always_prompt',
  diarization_source: 'provider',
};
const engineValue = (value: unknown) => ({ value, source: 'default' });

beforeEach(() => {
  vi.clearAllMocks();
  resetCapabilities();
  settingsModalStore.reset();
  api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS });
  api.getTranscriptionSystemDefaults.mockResolvedValue({
    min_speakers: 1,
    max_speakers: 20,
    diarization_source_default: 'provider',
  });
  mockAxios.get.mockImplementation(async (url: string) => ({
    data:
      url === '/admin/engine-settings'
        ? {
            diarizer_backend: engineValue('native'),
            diarizer_require_sidecar: engineValue(false),
            boundary_smoothing_enabled: engineValue(true),
            boundary_acoustic_recheck_enabled: engineValue(false),
            boundary_acoustic_cosine_margin: engineValue(0.05),
            boundary_acoustic_max_word_dur: engineValue(1.0),
          }
        : { pending_files: 0, total_files: 0, progress: null },
  }));
});

const tab = (c: HTMLElement, id: string) =>
  c.querySelector(`#tab-${id}`) as HTMLButtonElement | null;
const panel = (c: HTMLElement, id: string) => c.querySelector(`#tabpanel-${id}`) as HTMLElement;
const ids = (c: HTMLElement) => Array.from(c.querySelectorAll('[role="tab"]')).map((el) => el.id);

describe('SpeakerIdentificationSection tabs by role', () => {
  it('gives a plain user detection and attributes only, and no tab strip contents beyond them', () => {
    const { container } = render(SpeakerIdentificationSection);
    expect(ids(container)).toEqual(['tab-spk-detection', 'tab-spk-attributes']);
  });

  it('shows an admin the engine and maintenance tabs locked with the super-admin reason', () => {
    const { container } = render(SpeakerIdentificationSection, { props: { isAdmin: true } });
    expect(ids(container)).toEqual([
      'tab-spk-detection',
      'tab-spk-attributes',
      'tab-spk-engine',
      'tab-spk-maintenance',
    ]);
    for (const id of ['spk-engine', 'spk-maintenance']) {
      expect(tab(container, id)?.disabled).toBe(true);
      expect(tab(container, id)?.getAttribute('title')).toBe('settings.nav.requiresSuperAdmin');
    }
  });

  it('never renders a locked tab panel for an admin, and falls back from a requested one', () => {
    const { container } = render(SpeakerIdentificationSection, {
      props: { isAdmin: true, initialTab: 'spk-engine' },
    });
    expect(panel(container, 'spk-engine')).toBeNull();
    expect(tab(container, 'spk-detection')?.getAttribute('aria-selected')).toBe('true');
    expect(mockAxios.get).not.toHaveBeenCalledWith('/admin/engine-settings');
  });

  it('opens and loads the engine for a super admin', async () => {
    const { container } = render(SpeakerIdentificationSection, {
      props: { isAdmin: true, isSuperAdmin: true, initialTab: 'spk-engine' },
    });
    expect(tab(container, 'spk-engine')?.getAttribute('aria-selected')).toBe('true');
    await waitFor(() => expect(container.querySelector('#diarizer-backend')).not.toBeNull());
  });

  it('drops tabs whose capability the deployment lacks', () => {
    const { container } = render(SpeakerIdentificationSection, {
      props: { isAdmin: true, isSuperAdmin: true, engineCap: false, migrationCap: false },
    });
    expect(ids(container)).toEqual(['tab-spk-detection', 'tab-spk-attributes']);
  });
});

describe('SpeakerIdentificationSection mounting and dirty state', () => {
  it('keeps the first panel mounted but hidden after switching tabs', async () => {
    const { container } = render(SpeakerIdentificationSection);
    await waitFor(() => expect(container.querySelector('#speaker-behavior')).not.toBeNull());
    await fireEvent.click(tab(container, 'spk-attributes') as HTMLElement);

    expect(panel(container, 'spk-detection').hidden).toBe(true);
    expect(container.querySelector('#speaker-behavior')).not.toBeNull();
    expect(panel(container, 'spk-attributes').hidden).toBe(false);
  });

  it('sets the speaker-identification dirty flag from a child change event, and clears it', async () => {
    const { container } = render(SpeakerIdentificationSection);
    await waitFor(() => expect(container.querySelector('#speaker-behavior')).not.toBeNull());
    expect(get(settingsModalStore).dirtyState['speaker-identification']).toBeFalsy();

    const select = container.querySelector('#speaker-behavior') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: 'use_custom' } });
    await waitFor(() =>
      expect(get(settingsModalStore).dirtyState['speaker-identification']).toBe(true)
    );
    expect(tab(container, 'spk-detection')?.querySelector('.tab-badge')?.textContent).toBe('●');

    await fireEvent.change(select, { target: { value: 'always_prompt' } });
    await waitFor(() =>
      expect(get(settingsModalStore).dirtyState['speaker-identification']).toBe(false)
    );
  });

  it('reports the engine panel dirty state through the same flag', async () => {
    const { container } = render(SpeakerIdentificationSection, {
      props: { isAdmin: true, isSuperAdmin: true, initialTab: 'spk-engine' },
    });
    await waitFor(() => expect(container.querySelector('#diarizer-backend')).not.toBeNull());
    await fireEvent.change(container.querySelector('#diarizer-backend') as HTMLSelectElement, {
      target: { value: 'pyannote' },
    });
    await waitFor(() =>
      expect(get(settingsModalStore).dirtyState['speaker-identification']).toBe(true)
    );
  });
});

describe('Maintenance tab', () => {
  it('holds the bulk panel and the embedding-system link card', async () => {
    const { container } = render(SpeakerIdentificationSection, {
      props: { isAdmin: true, isSuperAdmin: true, initialTab: 'spk-maintenance' },
    });
    expect(container.querySelector('[data-testid="embedding-link-card"] button')).not.toBeNull();
    await waitFor(() =>
      expect(mockAxios.get).toHaveBeenCalledWith('/speaker-attributes/migration/status')
    );
  });

  it('omits the link card when the embedding section does not exist', () => {
    const { container } = render(SpeakerIdentificationSection, {
      props: {
        isAdmin: true,
        isSuperAdmin: true,
        initialTab: 'spk-maintenance',
        embeddingCap: false,
      },
    });
    expect(container.querySelector('[data-testid="embedding-link-card"]')).toBeNull();
  });
});
