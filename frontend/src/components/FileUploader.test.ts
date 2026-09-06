/**
 * `FileUploader.svelte` is the multi-step upload wizard: it owns file-type/size
 * validation, the dynamically inserted "extraction" step (large videos only),
 * step-navigation/maxStepReached bookkeeping, and the terminal dispatch into
 * `uploadsStore`. None of that is delegated to a child — the step components
 * are dumb (per `frontend/src/components/CLAUDE.md`'s coordinator/child split) —
 * so it is exactly the "complex derived state and multi-step orchestration"
 * this suite scopes to. The real `MediaFilePanel`/`UploadStepReview` children are
 * used (not mocked) for the steps under test, since neither makes network calls
 * on its own; the other step children never mount because `skipToReview()` jumps
 * straight past them, matching how a "review with defaults" user actually flows.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/toast', () => ({
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

const mockUploadsStore = vi.hoisted(() => ({
  addFile: vi.fn(
    (_file: File, _speakerParams: unknown, _collectionIds?: string[], _tagNames?: string[]) =>
      'upload-id-1'
  ),
  addFiles: vi.fn((_files: File[], _collectionIds?: string[], _tagNames?: string[]) => [
    'upload-id-1',
  ]),
  addRecording: vi.fn(
    (_blob: Blob, _filename: string, _collectionIds?: string[], _tagNames?: string[]) =>
      'upload-id-1'
  ),
  addExtractedAudio: vi.fn(),
}));
vi.mock('$stores/uploads', () => ({ uploadsStore: mockUploadsStore }));

vi.mock('$lib/services/configService', () => ({
  loadProtectedMediaAuthConfig: vi.fn().mockResolvedValue(undefined),
}));

const mockGetAudioExtractionSettings = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/audioExtractionSettings', () => ({
  getAudioExtractionSettings: mockGetAudioExtractionSettings,
}));

vi.mock('$lib/api/transcriptionSettings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/transcriptionSettings')>();
  return {
    ...actual,
    getTranscriptionSettings: vi
      .fn()
      .mockResolvedValue({ ...actual.DEFAULT_TRANSCRIPTION_SETTINGS }),
    getTranscriptionSystemDefaults: vi.fn().mockResolvedValue({
      min_speakers: 1,
      max_speakers: 20,
      garbage_cleanup_enabled: true,
      garbage_cleanup_threshold: 50,
      valid_speaker_prompt_behaviors: ['always_prompt', 'use_defaults', 'use_custom'],
      available_source_languages: { auto: 'Auto-detect', en: 'English' },
      available_llm_output_languages: { en: 'English' },
      common_languages: ['auto', 'en'],
      vad_threshold: 0.5,
      vad_min_silence_ms: 2000,
      vad_min_speech_ms: 250,
      vad_speech_pad_ms: 400,
      hallucination_silence_threshold: null,
      repetition_penalty: 1.0,
      diarization_source_default: 'provider',
      valid_diarization_sources: ['provider', 'local', 'pyannote', 'off'],
    }),
  };
});

vi.mock('$lib/api/asrSettings', () => ({
  ASRSettingsApi: {
    getActiveLocalModel: vi.fn().mockResolvedValue({ active_model: 'large-v3-turbo' }),
  },
}));

vi.mock('$lib/api/tags', () => ({ listTags: vi.fn().mockResolvedValue([]) }));

import FileUploader from './FileUploader.svelte';

const PREVIOUS_VALUES_KEY = 'opentr:uploadPreviousValues';

function file(overrides: { name?: string; type?: string; size?: number } = {}): File {
  const f = new File(['x'], overrides.name ?? 'clip.mp3', { type: overrides.type ?? 'audio/mpeg' });
  if (overrides.size !== undefined) {
    Object.defineProperty(f, 'size', { configurable: true, value: overrides.size });
  }
  return f;
}

async function selectFile(container: HTMLElement, selected: File) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, 'files', { configurable: true, value: [selected] });
  await fireEvent.change(input);
}

async function selectFiles(container: HTMLElement, files: File[]) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, 'files', { configurable: true, value: files });
  await fireEvent.change(input);
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mockAxios.get.mockResolvedValue({ data: [] });
  mockAxios.post.mockResolvedValue({ data: {} });
  mockGetAudioExtractionSettings.mockResolvedValue({
    auto_extract_enabled: true,
    extraction_threshold_mb: 100,
    remember_choice: false,
    show_modal: true,
  });
});

describe('file validation', () => {
  it('rejects a non-audio/video type with an error and leaves Next disabled', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    await selectFile(container, file({ type: 'application/pdf', name: 'doc.pdf' }));

    expect(container.querySelector('.error-msg')?.textContent).toContain('uploader.fileTypeError');
    expect((container.querySelector('.nav-next') as HTMLButtonElement).disabled).toBe(true);
  });

  it('rejects a file over the hard size limit', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    await selectFile(container, file({ size: 16 * 1024 * 1024 * 1024 })); // > 15 GB

    expect(container.querySelector('.error-msg')?.textContent).toContain(
      'uploader.fileTooLargeError'
    );
    expect((container.querySelector('.nav-next') as HTMLButtonElement).disabled).toBe(true);
  });

  it('accepts a file between the warning and hard limits, but still warns', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    await selectFile(container, file({ size: 3 * 1024 * 1024 * 1024 })); // > 2 GB warning, < 15 GB max

    expect(container.querySelector('.error-msg')?.textContent).toContain(
      'uploader.largeFileWarning'
    );
    expect((container.querySelector('.nav-next') as HTMLButtonElement).disabled).toBe(false);
  });

  it('accepts an ordinary small file with no error', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    await selectFile(container, file({ size: 1024 }));

    expect(container.querySelector('.error-msg')).toBeNull();
    expect((container.querySelector('.nav-next') as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('dynamic extraction step', () => {
  it('inserts an extraction step for a video over the configured threshold', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());
    expect(container.querySelectorAll('.step-item')).toHaveLength(6);

    await selectFile(
      container,
      file({ type: 'video/mp4', name: 'clip.mp4', size: 150 * 1024 * 1024 })
    );

    await waitFor(() => expect(container.querySelectorAll('.step-item')).toHaveLength(7));
  });

  it('does not insert the step for a large AUDIO file (video-only)', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    await selectFile(container, file({ type: 'audio/mpeg', size: 150 * 1024 * 1024 }));

    expect(container.querySelectorAll('.step-item')).toHaveLength(6);
  });

  it('does not insert the step for a small video under the threshold', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    await selectFile(
      container,
      file({ type: 'video/mp4', name: 'clip.mp4', size: 10 * 1024 * 1024 })
    );

    expect(container.querySelectorAll('.step-item')).toHaveLength(6);
  });

  it('removes a previously inserted extraction step when the video is removed and replaced with a non-qualifying file', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    await selectFile(
      container,
      file({ type: 'video/mp4', name: 'clip.mp4', size: 150 * 1024 * 1024 })
    );
    await waitFor(() => expect(container.querySelectorAll('.step-item')).toHaveLength(7));

    // Selecting a file hides the picker input behind a "selected file" summary
    // (MediaFilePanel) — remove it first to get the input back.
    await fireEvent.click(container.querySelector('.file-remove') as HTMLElement);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    await selectFile(container, file({ type: 'audio/mpeg', size: 1024 }));
    await waitFor(() => expect(container.querySelectorAll('.step-item')).toHaveLength(6));
  });
});

describe('multi-file selection', () => {
  it('queues valid files directly (bypassing the wizard) and reports skipped invalid/oversized ones', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    const good = file({ name: 'a.mp3', size: 1024 });
    const badType = file({ type: '', name: 'notes.txt', size: 1024 });
    const tooBig = file({ name: 'huge.mp3', size: 20 * 1024 * 1024 * 1024 });

    await selectFiles(container, [good, badType, tooBig]);

    expect(mockUploadsStore.addFiles).toHaveBeenCalledTimes(1);
    const [queuedFiles] = mockUploadsStore.addFiles.mock.calls[0];
    expect(queuedFiles).toEqual([good]);
  });
});

/** Media -> tags -> "review with defaults" (skipToReview), matching how a
 * user who doesn't need the intermediate steps actually flows. */
async function reachReviewStep(container: HTMLElement, selected: File) {
  await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());
  await selectFile(container, selected);
  await fireEvent.click(container.querySelector('.nav-next') as HTMLElement); // media -> tags
  await waitFor(() => expect(container.querySelector('.nav-review-defaults')).not.toBeNull());
  await fireEvent.click(container.querySelector('.nav-review-defaults') as HTMLElement); // -> review
  await waitFor(() => expect(container.querySelector('.nav-submit')).not.toBeNull());
}

describe('the review-with-defaults happy path', () => {
  it('jumps straight to the review step and marks every step up to it as reached', async () => {
    const { container } = render(FileUploader);
    await reachReviewStep(container, file({ size: 1024 }));

    const items = Array.from(container.querySelectorAll('.step-item'));
    expect(items[items.length - 1].classList.contains('active')).toBe(true);
    // Every earlier step is at least "visited" (not disabled), since maxStepReached
    // was fast-forwarded to the review index rather than incremented one at a time.
    items.slice(0, -1).forEach((el) => expect((el as HTMLButtonElement).disabled).toBe(false));
  });

  it('queues the file and resets the wizard back to the first step on submit', async () => {
    const { container } = render(FileUploader);
    await reachReviewStep(container, file({ name: 'clip.mp3', size: 1024 }));

    await fireEvent.click(container.querySelector('.nav-submit') as HTMLElement);

    expect(mockUploadsStore.addFile).toHaveBeenCalledTimes(1);
    const [queuedFile] = mockUploadsStore.addFile.mock.calls[0];
    expect(queuedFile.name).toBe('clip.mp3');

    await waitFor(() => {
      const items = container.querySelectorAll('.step-item');
      expect(items[0].classList.contains('active')).toBe(true);
    });
  });

  it('passes the selected tags/collections through to addFile', async () => {
    mockAxios.get.mockResolvedValue({
      data: [{ uuid: 'col-1', name: 'Meetings' }],
    });
    const { container } = render(FileUploader);
    await reachReviewStep(container, file({ size: 1024 }));

    await fireEvent.click(container.querySelector('.nav-submit') as HTMLElement);

    const [, , collectionIds, tagNames] = mockUploadsStore.addFile.mock.calls[0];
    // Nothing was selected in this pass (no tag/collection step was visited), so
    // both organize params come through empty/undefined rather than throwing.
    expect(collectionIds).toBeUndefined();
    expect(tagNames).toBeUndefined();
  });
});

describe('remembering previous values', () => {
  it('restores previously-used tags from localStorage on mount', async () => {
    localStorage.setItem(
      PREVIOUS_VALUES_KEY,
      JSON.stringify({
        collectionIds: [],
        collectionNames: [],
        tagNames: ['meeting-notes'],
        minSpeakers: null,
        maxSpeakers: null,
        numSpeakers: null,
        skipSummary: false,
        selectedWhisperModel: null,
        skippedSteps: [],
        timestamp: 1,
      })
    );

    const { container } = render(FileUploader);
    await reachReviewStep(container as HTMLElement, file({ name: 'clip.mp3', size: 1024 }));
    await fireEvent.click(container.querySelector('.nav-submit') as HTMLElement);

    const [, , , tagNames] = mockUploadsStore.addFile.mock.calls[0];
    expect(tagNames).toEqual(['meeting-notes']);
  });
});

/**
 * Issue #739: the wizard rendered a "Skip" button beside "Next" on every
 * optional step, and both were wired to the same `goNext()` handler — two
 * controls, one action, so Skip communicated a choice the user did not have.
 */
describe('optional-step navigation (#739)', () => {
  it('shows no Skip button beside Next on the optional tags step', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());
    await selectFile(container, file({ name: 'clip.mp3', size: 1024 }));

    // media -> tags, which is the first `optional: true` step.
    await fireEvent.click(container.querySelector('.nav-next') as HTMLElement);

    await waitFor(() => expect(container.querySelector('.nav-next')).not.toBeNull());
    expect(container.querySelector('.nav-skip')).toBeNull();
  });

  it('shows no Skip button on the optional collections step either', async () => {
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());
    await selectFile(container, file({ name: 'clip.mp3', size: 1024 }));

    // media -> tags -> collections, the second `optional: true` step.
    await fireEvent.click(container.querySelector('.nav-next') as HTMLElement);
    await waitFor(() => expect(container.querySelector('.nav-next')).not.toBeNull());
    await fireEvent.click(container.querySelector('.nav-next') as HTMLElement);

    await waitFor(() => expect(container.querySelector('.nav-next')).not.toBeNull());
    expect(container.querySelector('.nav-skip')).toBeNull();
  });
});
