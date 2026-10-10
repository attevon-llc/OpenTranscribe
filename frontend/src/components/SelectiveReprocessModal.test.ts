/**
 * The reprocess dialog starts from the user's saved speaker range and sends a blank field as
 * the user's own choice, instead of leaving it to an env default (#1198).
 *
 * Asserted at the boundary: the input the dialog shows, and the request body it posts.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('../lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.mock('../stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('../stores/llmStatus', async () => {
  const { readable } = await import('svelte/store');
  return {
    isLLMAvailable: readable(false),
    llmStatusStore: { initialize: vi.fn() },
  };
});

vi.mock('$lib/api/asrSettings', () => ({
  ASRSettingsApi: {
    getStatus: vi.fn().mockResolvedValue({ is_cloud_provider: false, active_provider: 'local' }),
    getActiveLocalModel: vi.fn().mockResolvedValue({ active_model: 'large-v3-turbo' }),
    getProviderDisplayName: (p: string) => p,
  },
}));

const mockGetSettings = vi.hoisted(() => vi.fn());
const mockGetDefaults = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/transcriptionSettings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/transcriptionSettings')>();
  return {
    ...actual,
    getTranscriptionSettings: mockGetSettings,
    getTranscriptionSystemDefaults: mockGetDefaults,
  };
});

import SelectiveReprocessModal from './SelectiveReprocessModal.svelte';
import { DEFAULT_TRANSCRIPTION_SETTINGS } from '$lib/api/transcriptionSettings';

const FILE = {
  uuid: 'file-1',
  status: 'completed',
  total_segments: 10,
  transcript_segments: [{}],
};

function saved(overrides: Record<string, unknown>) {
  mockGetSettings.mockResolvedValue({
    ...DEFAULT_TRANSCRIPTION_SETTINGS,
    min_speakers: 3,
    max_speakers: 5,
    ...overrides,
  });
}

async function openOnSettingsStep(stage: 'transcription' | 'rediarize') {
  const view = render(SelectiveReprocessModal, { props: { showModal: true, file: FILE } });
  const boxes = () => Array.from(document.querySelectorAll<HTMLInputElement>('.stage-checkbox'));
  await waitFor(() => expect(boxes().length).toBeGreaterThan(0));
  const index = stage === 'transcription' ? 0 : 1;
  await waitFor(() => expect(boxes()[index].disabled).toBe(false));
  await fireEvent.click(boxes()[index]);
  await fireEvent.click(document.querySelector('.primary-button') as HTMLElement); // -> settings
  await waitFor(() => expect(document.querySelector('#modal-min-speakers')).not.toBeNull());
  return view;
}

const field = (id: string) => document.querySelector(id) as HTMLInputElement;

async function submit() {
  await fireEvent.click(document.querySelector('.primary-button') as HTMLElement); // -> review
  await waitFor(() => expect(document.querySelector('.primary-button')).not.toBeNull());
  await fireEvent.click(document.querySelector('.primary-button') as HTMLElement); // -> submit
  await waitFor(() => expect(mockAxios.post).toHaveBeenCalled());
  return mockAxios.post.mock.calls[0][1] as Record<string, unknown>;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.post.mockResolvedValue({ data: {} });
  mockAxios.get.mockResolvedValue({ data: {} });
  mockGetDefaults.mockResolvedValue({ min_speakers: 1, max_speakers: 20 });
  saved({ speaker_prompt_behavior: 'use_custom' });
});

describe('SelectiveReprocessModal speaker range', () => {
  it('prefills the saved range for a re-diarize', async () => {
    await openOnSettingsStep('rediarize');
    await waitFor(() => expect(field('#modal-min-speakers').value).toBe('3'));
    expect(field('#modal-max-speakers').value).toBe('5');
  });

  it('prefills the saved range for a full transcription re-run too', async () => {
    saved({ speaker_prompt_behavior: 'always_prompt' });
    await openOnSettingsStep('transcription');
    await waitFor(() => expect(field('#modal-min-speakers').value).toBe('3'));
  });

  it('starts blank under "use system defaults", and then sends the system range', async () => {
    saved({ speaker_prompt_behavior: 'use_defaults' });
    await openOnSettingsStep('rediarize');
    await waitFor(() => expect(mockGetSettings).toHaveBeenCalled());
    expect(field('#modal-min-speakers').value).toBe('');

    const body = await submit();
    expect(body).toMatchObject({ stages: ['rediarize'], min_speakers: 1, max_speakers: 20 });
  });

  it('sends the prefilled saved range, and a value the user typed over it', async () => {
    await openOnSettingsStep('rediarize');
    await waitFor(() => expect(field('#modal-min-speakers').value).toBe('3'));
    await fireEvent.input(field('#modal-max-speakers'), { target: { value: '8' } });

    const body = await submit();
    expect(body).toMatchObject({ min_speakers: 3, max_speakers: 8 });
  });

  it('still opens, with blank fields, when the saved settings cannot be loaded', async () => {
    mockGetSettings.mockRejectedValue(new Error('offline'));
    await openOnSettingsStep('rediarize');
    expect(field('#modal-min-speakers').value).toBe('');

    const body = await submit();
    expect(body.min_speakers).toBeUndefined();
  });
});
