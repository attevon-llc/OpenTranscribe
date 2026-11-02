/**
 * The reprocess wizard: which stages it offers for a file, how the step list changes with the
 * selection, when the form is valid, and the exact request each mode sends. The speaker-range
 * prefill and the cloud-ASR banner are covered in `SelectiveReprocessModal.test.ts`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('../lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
vi.mock('../stores/toast', () => ({ toastStore: toast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => {
      run((key: string, o?: Record<string, unknown>) =>
        o?.count !== undefined ? `${key}:${o.count}` : key
      );
      return () => {};
    },
  },
}));

const llm = vi.hoisted(() => ({ available: { current: false } }));
vi.mock('../stores/llmStatus', async () => {
  const { writable } = await import('svelte/store');
  const isLLMAvailable = writable(false);
  llm.available = {
    set current(v: boolean) {
      isLLMAvailable.set(v);
    },
  } as { current: boolean };
  return { isLLMAvailable, llmStatusStore: { initialize: vi.fn() } };
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

const DONE = { uuid: 'f1', status: 'completed', total_segments: 5, transcript_segments: [{}] };
const FAILED = { uuid: 'f2', status: 'error' };

const boxes = () => Array.from(document.querySelectorAll<HTMLInputElement>('.stage-checkbox'));
// Order: transcription, rediarize, search_indexing, analytics, speaker_llm, summarization, topic_extraction.
const BOX = {
  transcription: 0,
  rediarize: 1,
  search: 2,
  analytics: 3,
  speakerLlm: 4,
  summary: 5,
  topics: 6,
};
const primary = () => document.querySelector('.primary-button') as HTMLButtonElement;
const pills = () =>
  Array.from(document.querySelectorAll('.stage-pill')).map((e) => e.textContent?.trim());

async function open(props: Record<string, unknown>, onReprocess?: (detail: unknown) => void) {
  const view = render(SelectiveReprocessModal, {
    props: { showModal: true, ...props },
    events: onReprocess ? { reprocess: (e: CustomEvent) => onReprocess(e.detail) } : {},
  });
  await waitFor(() => expect(boxes()).toHaveLength(7));
  return view;
}

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = '';
  llm.available.current = false;
  mockAxios.post.mockResolvedValue({ data: {} });
  mockGetDefaults.mockResolvedValue({ min_speakers: 1, max_speakers: 20 });
  mockGetSettings.mockResolvedValue({
    ...DEFAULT_TRANSCRIPTION_SETTINGS,
    speaker_prompt_behavior: 'use_custom',
  });
});

describe('which stages are offered', () => {
  it('enables everything but the AI stages for a finished file when no LLM is configured', async () => {
    await open({ file: DONE });
    expect(boxes().map((b) => b.disabled)).toEqual([false, false, false, false, true, true, true]);
    expect(document.querySelector('.stage-disabled-hint')?.textContent).toBe(
      'reprocess.llmNotAvailable'
    );
  });

  it('enables the AI stages once an LLM is available', async () => {
    llm.available.current = true;
    await open({ file: DONE });
    await waitFor(() => expect(boxes()[BOX.summary].disabled).toBe(false));
  });

  it('leaves only full transcription for a file with no transcript', async () => {
    llm.available.current = true;
    await open({ file: FAILED });
    expect(boxes().map((b) => b.disabled)).toEqual([false, true, true, true, true, true, true]);
    expect(document.querySelector('.stage-disabled-hint')?.textContent).toBe(
      'reprocess.stageRediarizeDisabled'
    );
  });

  it('in bulk mode offers the stages when ANY selected file is completed', async () => {
    await open({ bulkMode: true, bulkFiles: [FAILED, { uuid: 'f3', status: 'completed' }] });
    expect(boxes()[BOX.rediarize].disabled).toBe(false);
    expect(boxes()[BOX.search].disabled).toBe(false);
  });
});

describe('selecting', () => {
  it('treats full transcription and re-diarize as mutually exclusive', async () => {
    await open({ file: DONE });
    await fireEvent.click(boxes()[BOX.transcription]);
    await fireEvent.click(boxes()[BOX.rediarize]);
    expect(boxes()[BOX.rediarize].checked).toBe(true);
    expect(boxes()[BOX.transcription].checked).toBe(false);
    await fireEvent.click(boxes()[BOX.transcription]);
    expect(boxes()[BOX.rediarize].checked).toBe(false);
  });

  it('Select all picks every enabled stage except re-diarize, and toggles back', async () => {
    llm.available.current = true;
    await open({ file: DONE });
    await waitFor(() => expect(boxes()[BOX.summary].disabled).toBe(false));
    const toggleAll = document.querySelector('.select-all-btn') as HTMLElement;
    expect(toggleAll.textContent?.trim()).toBe('reprocess.selectAll');
    await fireEvent.click(toggleAll);
    expect(boxes().map((b) => b.checked)).toEqual([true, false, true, true, true, true, true]);
    expect(toggleAll.textContent?.trim()).toBe('reprocess.deselectAll');
    await fireEvent.click(toggleAll);
    expect(boxes().some((b) => b.checked)).toBe(false);
  });

  it('cannot go on without a stage', async () => {
    await open({ file: DONE });
    expect(primary().disabled).toBe(true);
    await fireEvent.click(boxes()[BOX.search]);
    expect(primary().disabled).toBe(false);
  });
});

describe('steps', () => {
  it('goes straight to review for stages that need no settings', async () => {
    await open({ file: DONE });
    await fireEvent.click(boxes()[BOX.analytics]);
    expect(document.querySelectorAll('.step-dot')).toHaveLength(2);
    await fireEvent.click(primary());
    await waitFor(() => expect(pills()).toEqual(['reprocess.stageAnalytics']));
  });

  it('inserts a settings step for transcription, and the review warns it replaces the transcript', async () => {
    await open({ file: DONE });
    await fireEvent.click(boxes()[BOX.transcription]);
    expect(document.querySelectorAll('.step-dot')).toHaveLength(3);
    await fireEvent.click(primary());
    await waitFor(() => expect(document.querySelector('#modal-min-speakers')).not.toBeNull());
    await fireEvent.click(primary());
    await waitFor(() => expect(document.querySelector('.warning-destructive')).not.toBeNull());
    expect(document.querySelector('.warning-caution')).toBeNull();
  });

  it('warns re-diarize rewrites the speakers instead', async () => {
    await open({ file: DONE });
    await fireEvent.click(boxes()[BOX.rediarize]);
    await fireEvent.click(primary());
    await waitFor(() => expect(document.querySelector('#modal-min-speakers')).not.toBeNull());
    await fireEvent.click(primary());
    await waitFor(() => expect(document.querySelector('.warning-caution')).not.toBeNull());
    expect(document.querySelector('.warning-destructive')).toBeNull();
  });

  it('refuses a minimum above the maximum', async () => {
    await open({ file: DONE });
    await fireEvent.click(boxes()[BOX.rediarize]);
    await fireEvent.click(primary());
    const min = await waitFor(
      () => document.querySelector('#modal-min-speakers') as HTMLInputElement
    );
    const max = document.querySelector('#modal-max-speakers') as HTMLInputElement;
    await fireEvent.input(min, { target: { value: '5' } });
    await fireEvent.input(max, { target: { value: '2' } });
    await waitFor(() => expect(document.querySelector('.validation-error')).not.toBeNull());
    await fireEvent.click(primary()); // review
    await waitFor(() => expect(pills()).toHaveLength(1));
    expect(primary().disabled).toBe(true);
  });
});

describe('submitting', () => {
  async function run() {
    await fireEvent.click(primary()); // review
    await waitFor(() => expect(document.querySelector('.review-section')).not.toBeNull());
    await fireEvent.click(primary()); // submit
  }

  it('posts the chosen stages for one file, tells the caller and closes', async () => {
    const onReprocess = vi.fn();
    await open({ file: DONE }, onReprocess);
    await fireEvent.click(boxes()[BOX.search]);
    await fireEvent.click(boxes()[BOX.analytics]);
    await run();
    await waitFor(() =>
      expect(mockAxios.post).toHaveBeenCalledWith('/files/f1/reprocess', {
        stages: ['search_indexing', 'analytics'],
      })
    );
    expect(toast.success).toHaveBeenCalledWith('reprocess.startedSuccess');
    expect(onReprocess).toHaveBeenCalledWith({
      fileId: 'f1',
      stages: ['search_indexing', 'analytics'],
    });
  });

  it('posts one bulk action for the selected files and reports how many started or failed', async () => {
    const onReprocess = vi.fn();
    mockAxios.post.mockResolvedValue({
      data: [{ success: true }, { success: true }, { success: false }],
    });
    await open({ bulkMode: true, bulkFiles: [DONE, { ...DONE, uuid: 'f3' }, FAILED] }, onReprocess);
    await fireEvent.click(boxes()[BOX.search]);
    await run();
    await waitFor(() =>
      expect(mockAxios.post).toHaveBeenCalledWith('/files/management/bulk-action', {
        stages: ['search_indexing'],
        file_uuids: ['f1', 'f3', 'f2'],
        action: 'reprocess',
      })
    );
    expect(toast.success).toHaveBeenCalledWith('reprocess.bulkStartedSuccess:2');
    expect(toast.error).toHaveBeenCalledWith('reprocess.bulkStartFailed:1');
    expect(onReprocess).toHaveBeenCalledWith({ stages: ['search_indexing'], count: 2 });
  });

  it('sends no Whisper model unless the user chose one', async () => {
    await open({ file: DONE });
    await fireEvent.click(boxes()[BOX.transcription]);
    await fireEvent.click(primary());
    await waitFor(() => expect(document.querySelector('#modal-min-speakers')).not.toBeNull());
    await run();
    await waitFor(() => expect(mockAxios.post).toHaveBeenCalled());
    expect(mockAxios.post.mock.calls[0][1]).not.toHaveProperty('whisper_model');
  });

  it("surfaces the server's reason and stays open when the request is refused", async () => {
    mockAxios.post.mockRejectedValue({
      response: { status: 409, data: { detail: 'media missing' } },
    });
    await open({ file: DONE });
    await fireEvent.click(boxes()[BOX.search]);
    await run();
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('media missing'));
    expect(document.querySelector('.stage-checkbox, .review-section')).not.toBeNull();
    expect(primary().disabled).toBe(false);
  });
});
