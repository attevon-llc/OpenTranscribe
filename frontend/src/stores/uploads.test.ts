/**
 * Tests for `$stores/uploads` — the SPA's upload-queue store. This is the FIRST test
 * file for this module (zero prior coverage): the store is not a thin re-export of
 * `uploadService`, it has its own logic layered on top:
 *
 *   - an event-driven rebuild: every `uploadService` event triggers a fresh
 *     `getAllUploads()` read and merge into local state (uploads.ts:26-42);
 *   - a `hasNewActivity` state machine that is asymmetric on purpose —
 *     `added`/`completed`/`failed` events set it, `expand()` always clears it, but
 *     `toggle()` clears it ONLY when transitioning collapsed -> expanded, never on
 *     the reverse transition (uploads.ts:69-75);
 *   - 9 derived stores, including `totalProgress`'s divide-by-zero guard and
 *     `uploadStats`'s six independent per-status `.filter().length` calls that must
 *     stay in sync with the status enum;
 *   - `reset()` deliberately uses `set()` rather than `update()`, bypassing the
 *     event-driven rebuild entirely — the logout-safety path that must produce a
 *     clean slate regardless of whatever event-driven state came before it.
 *
 * `uploadService` is mocked wholesale; the listener it captures via
 * `addEventListener` is invoked manually so each test can control exactly what
 * `getAllUploads()` returns at the moment of a given event.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import type { UploadItem, UploadEvent } from '$lib/services/uploadService';

type Listener = (event: UploadEvent) => void;

let capturedListener: Listener | null = null;
let getAllUploadsReturn: UploadItem[] = [];

const mockUploadService = vi.hoisted(() => ({
  addEventListener: vi.fn(),
  getAllUploads: vi.fn(),
  addUpload: vi.fn(),
  addMultipleFiles: vi.fn(),
  addExtractedAudio: vi.fn(),
  cancelUpload: vi.fn(),
  retryUpload: vi.fn(),
  removeUpload: vi.fn(),
  clearCompleted: vi.fn(),
  reset: vi.fn(),
}));

vi.mock('$lib/services/uploadService', () => ({
  uploadService: mockUploadService,
}));

function makeUpload(overrides: Partial<UploadItem> = {}): UploadItem {
  return {
    id: 'u-1',
    type: 'file',
    source: 'irrelevant',
    name: 'file.mp3',
    status: 'queued',
    progress: 0,
    retryCount: 0,
    ...overrides,
  } as UploadItem;
}

describe('uploadsStore', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    capturedListener = null;
    getAllUploadsReturn = [];

    mockUploadService.addEventListener.mockImplementation((listener: Listener) => {
      capturedListener = listener;
      return vi.fn(); // cleanup fn
    });
    mockUploadService.getAllUploads.mockImplementation(() => getAllUploadsReturn);

    // The store is created once at module scope, wiring its listener at import
    // time — re-import fresh per test via `vi.resetModules()` so every test gets
    // its own store instance and its own captured listener, rather than sharing
    // state (and an already-consumed listener slot) across tests.
    vi.resetModules();
  });

  async function loadStore() {
    return await import('./uploads');
  }

  function emit(event: UploadEvent) {
    expect(capturedListener).not.toBeNull();
    capturedListener!(event);
  }

  describe('event-driven rebuild', () => {
    it('rebuilds uploads from a fresh getAllUploads() read on an "added" event', async () => {
      const { uploadsStore } = await loadStore();

      const upload = makeUpload({ id: 'u-1', status: 'queued' });
      getAllUploadsReturn = [upload];
      emit({ type: 'added', uploadId: 'u-1', data: upload });

      const state = get(uploadsStore);
      expect(state.uploads).toEqual([upload]);
    });

    it('reflects whatever getAllUploads() returns at the moment of a "progress" event', async () => {
      const { uploadsStore } = await loadStore();

      const uploadA = makeUpload({ id: 'u-1', status: 'uploading', progress: 40 });
      getAllUploadsReturn = [uploadA];
      emit({ type: 'progress', uploadId: 'u-1', data: { progress: 40 } });
      expect(get(uploadsStore).uploads).toEqual([uploadA]);

      const uploadAUpdated = { ...uploadA, progress: 80 };
      getAllUploadsReturn = [uploadAUpdated];
      emit({ type: 'progress', uploadId: 'u-1', data: { progress: 80 } });
      expect(get(uploadsStore).uploads).toEqual([uploadAUpdated]);
    });

    it('reflects a removal on the next event even though the event itself carries no list', async () => {
      const { uploadsStore } = await loadStore();

      const upload = makeUpload({ id: 'u-1' });
      getAllUploadsReturn = [upload];
      emit({ type: 'added', uploadId: 'u-1', data: upload });
      expect(get(uploadsStore).uploads).toHaveLength(1);

      getAllUploadsReturn = [];
      emit({ type: 'cancelled', uploadId: 'u-1' });
      expect(get(uploadsStore).uploads).toEqual([]);
    });
  });

  describe('hasNewActivity asymmetric state machine', () => {
    it.each([['added'], ['completed'], ['failed']] as const)(
      'sets hasNewActivity on a %s event',
      async (eventType) => {
        const { uploadsStore } = await loadStore();
        expect(get(uploadsStore).hasNewActivity).toBe(false);

        emit({ type: eventType, uploadId: 'u-1' });

        expect(get(uploadsStore).hasNewActivity).toBe(true);
      }
    );

    it.each([['started'], ['progress'], ['cancelled'], ['retry']] as const)(
      'does NOT set hasNewActivity on a %s event',
      async (eventType) => {
        const { uploadsStore } = await loadStore();

        emit({ type: eventType, uploadId: 'u-1' });

        expect(get(uploadsStore).hasNewActivity).toBe(false);
      }
    );

    it('expand() always clears hasNewActivity', async () => {
      const { uploadsStore } = await loadStore();
      emit({ type: 'added', uploadId: 'u-1' });
      expect(get(uploadsStore).hasNewActivity).toBe(true);

      uploadsStore.expand();

      const state = get(uploadsStore);
      expect(state.hasNewActivity).toBe(false);
      expect(state.isExpanded).toBe(true);
    });

    it('toggle() clears hasNewActivity when transitioning collapsed -> expanded', async () => {
      const { uploadsStore } = await loadStore();
      emit({ type: 'added', uploadId: 'u-1' });
      expect(get(uploadsStore).isExpanded).toBe(false);
      expect(get(uploadsStore).hasNewActivity).toBe(true);

      uploadsStore.toggle();

      const state = get(uploadsStore);
      expect(state.isExpanded).toBe(true);
      expect(state.hasNewActivity).toBe(false);
    });

    it('toggle() does NOT clear hasNewActivity when transitioning expanded -> collapsed', async () => {
      const { uploadsStore } = await loadStore();
      uploadsStore.expand(); // isExpanded: true, hasNewActivity: false

      // New activity arrives while the panel is already expanded.
      emit({ type: 'completed', uploadId: 'u-1' });
      expect(get(uploadsStore).hasNewActivity).toBe(true);

      uploadsStore.toggle(); // now collapsing

      const state = get(uploadsStore);
      expect(state.isExpanded).toBe(false);
      // This is the asymmetry: collapsing must NOT clear activity that arrived
      // while the panel was open, unlike expand()/the expanding branch of toggle().
      expect(state.hasNewActivity).toBe(true);
    });

    it('collapse() never touches hasNewActivity', async () => {
      const { uploadsStore } = await loadStore();
      uploadsStore.expand();
      emit({ type: 'failed', uploadId: 'u-1' });
      expect(get(uploadsStore).hasNewActivity).toBe(true);

      uploadsStore.collapse();

      const state = get(uploadsStore);
      expect(state.isExpanded).toBe(false);
      expect(state.hasNewActivity).toBe(true);
    });

    it('clearNewActivity() clears the flag without touching isExpanded', async () => {
      const { uploadsStore } = await loadStore();
      emit({ type: 'added', uploadId: 'u-1' });
      expect(get(uploadsStore).hasNewActivity).toBe(true);

      uploadsStore.clearNewActivity();

      const state = get(uploadsStore);
      expect(state.hasNewActivity).toBe(false);
      expect(state.isExpanded).toBe(false);
    });
  });

  describe('derived stores', () => {
    async function seed(uploads: UploadItem[]) {
      const mod = await loadStore();
      getAllUploadsReturn = uploads;
      // 'progress' does not set hasNewActivity (see the state-machine tests
      // above), so seeding here doesn't contaminate tests that assert on it.
      emit({ type: 'progress', uploadId: 'seed', data: uploads });
      return mod;
    }

    const uploading = makeUpload({ id: 'a', status: 'uploading', progress: 20 });
    const processing = makeUpload({ id: 'b', status: 'processing', progress: 60 });
    const preparing = makeUpload({ id: 'c', status: 'preparing', progress: 0 });
    const queued = makeUpload({ id: 'd', status: 'queued', progress: 0 });
    const completed = makeUpload({ id: 'e', status: 'completed', progress: 100 });
    const failed = makeUpload({ id: 'f', status: 'failed', progress: 0 });
    const cancelled = makeUpload({ id: 'g', status: 'cancelled', progress: 0 });

    const mixed = [uploading, processing, preparing, queued, completed, failed, cancelled];

    it('activeUploads includes uploading, processing, and preparing only', async () => {
      const { activeUploads } = await seed(mixed);
      expect(
        get(activeUploads)
          .map((u) => u.id)
          .sort()
      ).toEqual(['a', 'b', 'c']);
    });

    it('queuedUploads includes queued only', async () => {
      const { queuedUploads } = await seed(mixed);
      expect(get(queuedUploads).map((u) => u.id)).toEqual(['d']);
    });

    it('completedUploads includes completed only', async () => {
      const { completedUploads } = await seed(mixed);
      expect(get(completedUploads).map((u) => u.id)).toEqual(['e']);
    });

    it('failedUploads includes failed only', async () => {
      const { failedUploads } = await seed(mixed);
      expect(get(failedUploads).map((u) => u.id)).toEqual(['f']);
    });

    it('uploadCount is the total number of uploads', async () => {
      const { uploadCount } = await seed(mixed);
      expect(get(uploadCount)).toBe(7);
    });

    it('activeUploadCount is the count of active uploads', async () => {
      const { activeUploadCount } = await seed(mixed);
      expect(get(activeUploadCount)).toBe(3);
    });

    it('hasActiveUploads is true when there is at least one active upload', async () => {
      const { hasActiveUploads } = await seed(mixed);
      expect(get(hasActiveUploads)).toBe(true);
    });

    it('hasActiveUploads is false when there are no active uploads', async () => {
      const { hasActiveUploads } = await seed([queued, completed, failed, cancelled]);
      expect(get(hasActiveUploads)).toBe(false);
    });

    it('totalProgress averages progress across active uploads only', async () => {
      const { totalProgress } = await seed(mixed);
      // active: uploading(20) + processing(60) + preparing(0) = 80 / 3 = 26.67 -> round 27
      expect(get(totalProgress)).toBe(27);
    });

    it('totalProgress guards divide-by-zero when there are no active uploads', async () => {
      const { totalProgress } = await seed([queued, completed, failed, cancelled]);
      expect(get(totalProgress)).toBe(0);
    });

    it('uploadStats counts each status independently', async () => {
      const { uploadStats } = await seed(mixed);
      expect(get(uploadStats)).toEqual({
        total: 7,
        active: 3,
        queued: 1,
        completed: 1,
        failed: 1,
        cancelled: 1,
      });
    });

    it('uploadStats reports all zeros for an empty upload list', async () => {
      const { uploadStats } = await seed([]);
      expect(get(uploadStats)).toEqual({
        total: 0,
        active: 0,
        queued: 0,
        completed: 0,
        failed: 0,
        cancelled: 0,
      });
    });

    it('isExpanded and hasNewActivity derived stores track store state', async () => {
      const { uploadsStore, isExpanded, hasNewActivity: hasNewActivityDerived } = await seed([]);
      expect(get(isExpanded)).toBe(false);
      expect(get(hasNewActivityDerived)).toBe(false);

      emit({ type: 'added', uploadId: 'x' });
      expect(get(hasNewActivityDerived)).toBe(true);

      uploadsStore.expand();
      expect(get(isExpanded)).toBe(true);
      expect(get(hasNewActivityDerived)).toBe(false);
    });
  });

  describe('action delegation to uploadService', () => {
    it('addFile delegates to uploadService.addUpload with type "file"', async () => {
      const { uploadsStore } = await loadStore();
      const file = new File(['x'], 'a.mp3');
      mockUploadService.addUpload.mockReturnValue('new-id');

      const id = uploadsStore.addFile(file, { minSpeakers: 1 }, ['col-1'], ['tag-1']);

      expect(mockUploadService.addUpload).toHaveBeenCalledWith(
        'file',
        file,
        undefined,
        { minSpeakers: 1 },
        ['col-1'],
        ['tag-1']
      );
      expect(id).toBe('new-id');
    });

    it('cancel() delegates to uploadService.cancelUpload and refreshes uploads', async () => {
      const { uploadsStore } = await loadStore();
      getAllUploadsReturn = [makeUpload({ id: 'u-1', status: 'cancelled' })];

      uploadsStore.cancel('u-1');

      expect(mockUploadService.cancelUpload).toHaveBeenCalledWith('u-1');
      expect(get(uploadsStore).uploads).toEqual(getAllUploadsReturn);
    });

    it('retry() delegates to uploadService.retryUpload without refreshing uploads directly', async () => {
      const { uploadsStore } = await loadStore();
      const before = get(uploadsStore).uploads;
      // Unlike cancel()/remove()/clearCompleted(), retry() does not read
      // getAllUploads() itself — the eventual 'retry' event (emitted by the real
      // service) is what would trigger a rebuild, not the delegating call.
      getAllUploadsReturn = [makeUpload({ id: 'should-not-appear' })];

      uploadsStore.retry('u-1');

      expect(mockUploadService.retryUpload).toHaveBeenCalledWith('u-1');
      expect(get(uploadsStore).uploads).toBe(before);
    });
  });

  describe('reset()', () => {
    it('calls uploadService.reset() and clears the store via set(), independent of prior state', async () => {
      const { uploadsStore } = await loadStore();

      // Build up event-driven state: expanded, activity flagged, uploads present.
      const upload = makeUpload({ id: 'u-1', status: 'failed' });
      getAllUploadsReturn = [upload];
      emit({ type: 'failed', uploadId: 'u-1', data: upload });
      uploadsStore.expand();
      // Re-flag activity after expanding so hasNewActivity is true going into reset().
      emit({ type: 'failed', uploadId: 'u-1', data: upload });
      expect(get(uploadsStore)).toEqual({
        uploads: [upload],
        isExpanded: true,
        hasNewActivity: true,
      });

      uploadsStore.reset();

      expect(mockUploadService.reset).toHaveBeenCalledTimes(1);
      expect(get(uploadsStore)).toEqual({
        uploads: [],
        isExpanded: false,
        hasNewActivity: false,
      });
    });

    it('reset() ignores whatever getAllUploads() currently returns (uses set(), not update())', async () => {
      const { uploadsStore } = await loadStore();
      // If getAllUploads() were consulted by reset(), this would leak into state.
      getAllUploadsReturn = [makeUpload({ id: 'leftover' })];

      uploadsStore.reset();

      expect(get(uploadsStore).uploads).toEqual([]);
    });
  });
});
