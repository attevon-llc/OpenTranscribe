import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const h = vi.hoisted(() => ({
  toast: { error: vi.fn() },
  store: {} as Record<string, ReturnType<typeof vi.fn>>,
}));
vi.mock('$stores/toast', () => ({ toastStore: h.toast }));
vi.mock('$stores/gallery', () => {
  const names = [
    'onUploadTrigger',
    'onCollectionsTrigger',
    'onAddToCollectionTrigger',
    'onAddTagsTrigger',
    'onTagsTrigger',
    'onRemoveTagsTrigger',
    'onQuarantineTrigger',
    'onHiddenChanged',
    'onHeldChanged',
    'onDeleteSelectedTrigger',
    'onReprocessTrigger',
    'onSummarizeTrigger',
    'onRedactTrigger',
    'onRetryFailedTrigger',
    'onExportTrigger',
    'onSpeakerIdTrigger',
    'onCancelProcessingTrigger',
  ];
  for (const n of names) h.store[n] = vi.fn(() => vi.fn());
  return { galleryStore: h.store };
});

import { createScrollSaver, restoreScrollTop } from './galleryScroll';
import { bindGalleryTriggers, listenForTrashEvents } from './galleryTriggers';
import { listenForUploadRequests } from './uploadModalEvents';
import { showFileErrorToast } from './fileErrorToast';
import { hasActiveFilters } from './galleryQuery';

const t = (key: string, o?: Record<string, unknown>) => (o ? `${key}:${JSON.stringify(o)}` : key);

describe('createScrollSaver', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('saves once per frame and cancel drops the pending save', () => {
    const save = vi.fn();
    const el = { scrollTop: 40 } as HTMLElement;
    const saver = createScrollSaver(() => el, save);
    saver.onScroll();
    saver.onScroll();
    vi.advanceTimersByTime(20);
    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith(40);
    saver.onScroll();
    saver.cancel();
    vi.advanceTimersByTime(20);
    expect(save).toHaveBeenCalledTimes(1);
  });

  it('restoreScrollTop retries until the offset sticks', async () => {
    let calls = 0;
    const el = {
      get scrollTop() {
        return calls >= 3 ? 500 : 0;
      },
      set scrollTop(_v: number) {
        calls++;
      },
    } as unknown as HTMLElement;
    restoreScrollTop(() => el, 500);
    await vi.advanceTimersByTimeAsync(200);
    expect(calls).toBe(3);
  });
});

describe('bindGalleryTriggers', () => {
  it('subscribes every trigger, calls handlers without the store payload, and unsubscribes all', () => {
    const handler = () => vi.fn();
    const hs = {
      upload: handler(),
      collections: handler(),
      addToCollection: handler(),
      addTags: handler(),
      tags: handler(),
      removeTags: handler(),
      quarantine: handler(),
      hiddenChanged: handler(),
      heldChanged: vi.fn(),
      deleteSelected: handler(),
      reprocess: handler(),
      summarize: handler(),
      redact: handler(),
      retryFailed: handler(),
      exportSelected: vi.fn(),
      speakerId: handler(),
      cancelProcessing: handler(),
    };
    const unsubs = Object.values(h.store).map(() => vi.fn());
    Object.values(h.store).forEach((fn, i) => fn.mockReturnValue(unsubs[i]));
    const stop = bindGalleryTriggers(hs);
    expect(Object.values(h.store).every((fn) => fn.mock.calls.length === 1)).toBe(true);
    h.store.onUploadTrigger.mock.calls[0][0](123);
    expect(hs.upload).toHaveBeenCalledWith();
    h.store.onExportTrigger.mock.calls[0][0]('srt');
    expect(hs.exportSelected).toHaveBeenCalledWith('srt');
    stop();
    expect(unsubs.every((u) => u.mock.calls.length === 1)).toBe(true);
  });
});

describe('window listeners', () => {
  it('upload requests open the modal and forward the tab or recorded file after mount', async () => {
    const open = vi.fn();
    const tab = vi.fn();
    const direct = vi.fn();
    window.addEventListener('setFileUploaderTab', tab);
    window.addEventListener('directFileUpload', direct);
    const stop = listenForUploadRequests(open);
    window.dispatchEvent(new CustomEvent('openAddMediaModal', { detail: { activeTab: 'url' } }));
    window.dispatchEvent(new CustomEvent('uploadRecordedFile', { detail: {} }));
    expect(open).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new CustomEvent('uploadRecordedFile', { detail: { file: 'f' } }));
    expect(open).toHaveBeenCalledTimes(2);
    await vi.waitFor(() => expect(tab).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(direct).toHaveBeenCalledTimes(1));
    stop();
    window.dispatchEvent(new CustomEvent('openAddMediaModal', { detail: {} }));
    expect(open).toHaveBeenCalledTimes(2);
    window.removeEventListener('setFileUploaderTab', tab);
    window.removeEventListener('directFileUpload', direct);
  });

  it('trash events pass the id as a string and stop after cleanup', () => {
    const trashed = vi.fn();
    const restored = vi.fn();
    const stop = listenForTrashEvents(trashed, restored);
    window.dispatchEvent(new CustomEvent('file-trashed', { detail: { file_id: 42 } }));
    window.dispatchEvent(new CustomEvent('file-trashed', { detail: {} }));
    window.dispatchEvent(new CustomEvent('file-restored'));
    expect(trashed).toHaveBeenCalledTimes(1);
    expect(trashed).toHaveBeenCalledWith('42');
    expect(restored).toHaveBeenCalledTimes(1);
    stop();
    window.dispatchEvent(new CustomEvent('file-restored'));
    expect(restored).toHaveBeenCalledTimes(1);
  });
});

describe('showFileErrorToast', () => {
  beforeEach(() => vi.clearAllMocks());

  it('lists suggestions and gives file-quality errors longer on screen', () => {
    showFileErrorToast(
      {
        filename: 'a.wav',
        error_reason: 'file_quality',
        error_suggestions: ['Re-record', 'Trim'],
        user_message: 'Bad audio',
      } as never,
      t
    );
    const [message, ms] = h.toast.error.mock.calls[0];
    expect(message).toContain(`"suggestions":${JSON.stringify('\u2022 Re-record\n\u2022 Trim')}`);
    expect(ms).toBe(10000);
  });

  it('falls back to the plain failure line without categorisation', () => {
    showFileErrorToast({ filename: 'a.wav', title: 'My file' } as never, t);
    expect(h.toast.error).toHaveBeenCalledWith('gallery.processingFailed:{"filename":"My file"}');
  });
});

describe('hasActiveFilters', () => {
  const none = {
    searchQuery: '',
    selectedTags: [],
    selectedSpeakers: [],
    dateRange: { from: null, to: null },
    durationRange: { min: null, max: null },
    selectedFileTypes: [],
    selectedStatuses: [],
    ownershipFilter: 'all' as const,
    hiddenMode: 'exclude' as const,
    held: false,
  };
  it('is false for the defaults', () => expect(hasActiveFilters(none)).toBe(false));
  it.each([
    ['search', { searchQuery: 'x' }],
    ['tag', { selectedTags: ['t'] }],
    ['date', { dateRange: { from: null, to: new Date() } }],
    ['duration', { durationRange: { min: null, max: 5 } }],
    ['ownership', { ownershipFilter: 'mine' as const }],
    ['hidden mode', { hiddenMode: 'only' as never }],
    ['held', { held: true }],
  ])('is true when %s narrows the list', (_n, patch) => {
    expect(hasActiveFilters({ ...none, ...patch })).toBe(true);
  });
});
