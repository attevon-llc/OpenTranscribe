/**
 * What the upload wizard sends for each source, and which of its steps it offers (#1201).
 *
 * The speaker range and model a user enters must reach the server on the URL tab and for an
 * in-wizard recording, not only for a picked file; and a step that collects a value the server
 * will throw away (speaker detection off, or the Fast CPU model) must not be offered. Payloads
 * are asserted where they leave the component: the `/files/process-url` POST body and the
 * arguments handed to the upload queue.
 *
 * The single-file "extract audio" path hands the wizard's options to a background job and then
 * resets the wizard. The options must be read BEFORE the reset: resetAllState() clears the
 * voiceprint consent, so reading them afterwards silently dropped it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

const mockUploadsStore = vi.hoisted(() => ({
  addFile: vi.fn(() => 'upload-id-1'),
  addFiles: vi.fn(() => ['upload-id-1']),
  addRecording: vi.fn(() => 'upload-id-1'),
  addExtractedAudio: vi.fn(),
}));
const mockExtractAndQueue = vi.hoisted(() => vi.fn().mockResolvedValue(true));
vi.mock('$lib/upload/bulkExtraction', () => ({
  extractAndQueue: mockExtractAndQueue,
  startBulkExtraction: vi.fn(),
}));

vi.mock('$stores/uploads', () => ({ uploadsStore: mockUploadsStore }));

vi.mock('$lib/services/configService', async () =>
  (await import('$testMocks/fileUploaderMocks')).configServiceMock()
);
vi.mock('$lib/api/audioExtractionSettings', async () =>
  (await import('$testMocks/fileUploaderMocks')).audioExtractionSettingsMock()
);

const mockGetSettings = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/transcriptionSettings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/transcriptionSettings')>();
  const { TRANSCRIPTION_SYSTEM_DEFAULTS } = await import('$testMocks/fileUploaderMocks');
  return {
    ...actual,
    getTranscriptionSettings: mockGetSettings,
    getTranscriptionSystemDefaults: vi.fn().mockResolvedValue(TRANSCRIPTION_SYSTEM_DEFAULTS),
  };
});

vi.mock('$lib/api/asrSettings', async () =>
  (await import('$testMocks/fileUploaderMocks')).asrActiveModelMock()
);
vi.mock('$lib/api/tags', async () => (await import('$testMocks/fileUploaderMocks')).tagsApiMock());

import FileUploader from './FileUploader.svelte';
import { selectFile, sizedFile } from '$testMocks/fileUploaderMocks';
import { recordingStore } from '$stores/recording';
import { DEFAULT_TRANSCRIPTION_SETTINGS } from '$lib/api/transcriptionSettings';

const PREVIOUS_VALUES_KEY = 'opentr:uploadPreviousValues';

function remember(overrides: Record<string, unknown>) {
  localStorage.setItem(
    PREVIOUS_VALUES_KEY,
    JSON.stringify({
      collectionIds: [],
      collectionNames: [],
      tagNames: [],
      minSpeakers: null,
      maxSpeakers: null,
      numSpeakers: null,
      skipSummary: false,
      selectedWhisperModel: null,
      skippedSteps: [],
      timestamp: 1,
      ...overrides,
    })
  );
}

function settings(overrides: Record<string, unknown> = {}) {
  mockGetSettings.mockResolvedValue({ ...DEFAULT_TRANSCRIPTION_SETTINGS, ...overrides });
}

const stepLabels = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('.step-item .step-label')).map((e) => e.textContent);

async function openTab(container: HTMLElement, tab: 'url' | 'record') {
  await waitFor(() => expect(container.querySelector('.tab-navigation')).not.toBeNull());
  window.dispatchEvent(new CustomEvent('setFileUploaderTab', { detail: { activeTab: tab } }));
}

async function reviewAndSubmit(container: HTMLElement) {
  await waitFor(() => expect(container.querySelector('.nav-next:not([disabled])')).not.toBeNull());
  await fireEvent.click(container.querySelector('.nav-next') as HTMLElement);
  await waitFor(() => expect(container.querySelector('.nav-review-defaults')).not.toBeNull());
  await fireEvent.click(container.querySelector('.nav-review-defaults') as HTMLElement);
  await waitFor(() => expect(container.querySelector('.nav-submit')).not.toBeNull());
  await fireEvent.click(container.querySelector('.nav-submit') as HTMLElement);
}

async function importUrl(container: HTMLElement) {
  await openTab(container, 'url');
  await waitFor(() => expect(container.querySelector('#media-url')).not.toBeNull());
  await fireEvent.input(container.querySelector('#media-url') as HTMLInputElement, {
    target: { value: 'https://example.com/talk' },
  });
  await reviewAndSubmit(container);
}

async function submitFile(container: HTMLElement) {
  await selectFile(container, sizedFile('clip.mp3', 'audio/mpeg'));
  await reviewAndSubmit(container);
}

const processUrlBody = () =>
  mockAxios.post.mock.calls.find((c) => c[0] === '/files/process-url')?.[1];

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mockAxios.get.mockResolvedValue({ data: [] });
  mockAxios.post.mockResolvedValue({ data: { uuid: 'new-file', title: 'Talk' } });
  settings();
  recordingStore.update((s) => ({ ...s, recordedBlob: null, recordingSupported: true }));
});

describe('URL import carries the per-file choices (#1201)', () => {
  it('sends the speaker range and the model the user picked', async () => {
    remember({ minSpeakers: 3, maxSpeakers: 5, selectedWhisperModel: null });
    const { container } = render(FileUploader);
    await importUrl(container);

    await waitFor(() => expect(processUrlBody()).toBeDefined());
    expect(processUrlBody()).toMatchObject({
      url: 'https://example.com/talk',
      min_speakers: 3,
      max_speakers: 5,
    });
  });

  it('sends the Fast model, and no speaker range that model would discard', async () => {
    remember({ minSpeakers: 3, maxSpeakers: 5, selectedWhisperModel: 'base' });
    const { container } = render(FileUploader);
    await importUrl(container);

    await waitFor(() => expect(processUrlBody()).toBeDefined());
    expect(processUrlBody().whisper_model).toBe('base');
    expect(processUrlBody().min_speakers).toBeUndefined();
    expect(processUrlBody().max_speakers).toBeUndefined();
  });
});

describe('an in-wizard recording carries the per-file choices (#1201)', () => {
  it('hands the transcription params to addRecording', async () => {
    remember({ minSpeakers: 2, maxSpeakers: 4, skipSummary: true });
    recordingStore.update((s) => ({ ...s, recordedBlob: new Blob(['audio']) }));
    const { container } = render(FileUploader);
    await openTab(container, 'record');
    await reviewAndSubmit(container);

    await waitFor(() => expect(mockUploadsStore.addRecording).toHaveBeenCalledTimes(1));
    const args = mockUploadsStore.addRecording.mock.calls[0] as unknown[];
    expect(args[4]).toMatchObject({ minSpeakers: 2, maxSpeakers: 4, skipSummary: true });
  });
});

describe('the Speakers step is offered only when it can apply (#1201)', () => {
  it('is present, after the Model step, by default', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(stepLabels(container).length).toBeGreaterThan(0));
    const labels = stepLabels(container);
    expect(labels).toContain('uploader.stepSpeakers');
    expect(labels.indexOf('uploader.stepModel')).toBeLessThan(
      labels.indexOf('uploader.stepSpeakers')
    );
  });

  it('is absent when the user has speaker detection off', async () => {
    settings({ diarization_source: 'off' });
    const { container } = render(FileUploader);
    await waitFor(() => expect(stepLabels(container)).not.toContain('uploader.stepSpeakers'));
    expect(stepLabels(container)).toContain('uploader.stepModel');
  });

  it('is absent once the Fast model is chosen, and a stale range is neither validated nor sent', async () => {
    // 6 > 2 would disable Submit if the hidden step's range were still validated.
    remember({ minSpeakers: 6, maxSpeakers: 2, selectedWhisperModel: 'base' });
    const { container } = render(FileUploader);
    await waitFor(() => expect(stepLabels(container).length).toBeGreaterThan(0));
    expect(stepLabels(container)).not.toContain('uploader.stepSpeakers');

    await submitFile(container);

    expect(mockUploadsStore.addFile).toHaveBeenCalledTimes(1);
    const [, params] = mockUploadsStore.addFile.mock.calls[0] as unknown as [
      File,
      Record<string, unknown>,
    ];
    expect(params).toMatchObject({ whisperModel: 'base', minSpeakers: null, maxSpeakers: null });
  });
});

describe('single-file audio extraction', () => {
  it('forwards the voiceprint consent and the chosen tags to the extraction job', async () => {
    remember({ tagNames: ['board'] });
    const { container } = render(FileUploader);
    await selectFile(container, sizedFile('clip.mp4', 'video/mp4', 150 * 1024 * 1024));

    const clickNext = async () => {
      await waitFor(() =>
        expect(container.querySelector('.nav-next:not([disabled])')).not.toBeNull()
      );
      await fireEvent.click(container.querySelector('.nav-next') as HTMLElement);
    };
    for (let i = 0; i < 12 && !container.querySelector('#voiceprint-consent'); i++) {
      await clickNext();
    }
    await waitFor(() => expect(container.querySelector('#voiceprint-consent')).not.toBeNull());
    await fireEvent.click(container.querySelector('#voiceprint-consent') as HTMLElement);
    for (let i = 0; i < 12 && !container.querySelector('.nav-submit'); i++) {
      await clickNext();
    }
    await fireEvent.click(container.querySelector('.nav-submit') as HTMLElement);

    expect(mockExtractAndQueue).toHaveBeenCalledTimes(1);
    const [, params, organize] = mockExtractAndQueue.mock.calls[0];
    expect(params.voiceprintConsent).toBe('granted');
    expect(organize.tagNames).toEqual(['board']);
  });
});
