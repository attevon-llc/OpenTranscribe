/**
 * Issue #752 (defect 4): the tray had no memory of an explicit collapse, so the
 * control did not work from the user's point of view. `userCollapsed` must
 * survive new uploads arriving and a page reload, and be cleared on logout.
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
  reset: vi.fn(),
}));
vi.mock('$lib/services/uploadService', () => ({ uploadService: mockUploadService }));

function makeUpload(id: string): UploadItem {
  return {
    id,
    type: 'file',
    source: 'x',
    name: `${id}.mp3`,
    status: 'queued',
    progress: 0,
    retryCount: 0,
  } as UploadItem;
}

function addUpload(id: string) {
  const u = makeUpload(id);
  getAllUploadsReturn = [...getAllUploadsReturn, u];
  capturedListener!({ type: 'added', uploadId: id, data: u });
}

describe('upload tray collapse persistence (issue #752)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    localStorage.clear();
    capturedListener = null;
    getAllUploadsReturn = [];
    mockUploadService.addEventListener.mockImplementation((l: Listener) => {
      capturedListener = l;
      return vi.fn();
    });
    mockUploadService.getAllUploads.mockImplementation(() => getAllUploadsReturn);
  });

  const load = () => import('./uploads');

  it('auto-expands when the first upload starts and the user has not collapsed it', async () => {
    const { uploadsStore } = await load();
    addUpload('a');
    expect(get(uploadsStore).isExpanded).toBe(true);
  });

  it('an explicit collapse sticks when further uploads are added', async () => {
    const { uploadsStore } = await load();
    addUpload('a');
    uploadsStore.collapse();
    addUpload('b');
    expect(get(uploadsStore).isExpanded).toBe(false);
  });

  it('an explicit collapse sticks when the list empties and a new upload arrives', async () => {
    const { uploadsStore } = await load();
    addUpload('a');
    uploadsStore.toggle(); // expanded -> collapsed
    getAllUploadsReturn = [];
    capturedListener!({ type: 'cancelled', uploadId: 'a' });
    addUpload('c');
    expect(get(uploadsStore).isExpanded).toBe(false);
  });

  it('re-expanding clears the collapsed preference', async () => {
    const { uploadsStore } = await load();
    addUpload('a');
    uploadsStore.collapse();
    uploadsStore.expand();
    getAllUploadsReturn = [];
    capturedListener!({ type: 'cancelled', uploadId: 'a' });
    uploadsStore.collapse();
    uploadsStore.toggle();
    expect(get(uploadsStore).isExpanded).toBe(true);
    getAllUploadsReturn = [];
    capturedListener!({ type: 'cancelled', uploadId: 'a' });
    addUpload('d');
    expect(get(uploadsStore).isExpanded).toBe(true);
  });

  it('the collapsed preference survives a page reload', async () => {
    let mod = await load();
    addUpload('a');
    mod.uploadsStore.collapse();

    vi.resetModules();
    capturedListener = null;
    getAllUploadsReturn = [];
    mod = await load();
    addUpload('b');
    expect(get(mod.uploadsStore).isExpanded).toBe(false);
  });

  it('reset() (logout) forgets the preference so the next user starts fresh', async () => {
    const { uploadsStore } = await load();
    addUpload('a');
    uploadsStore.collapse();
    uploadsStore.reset();
    getAllUploadsReturn = [];
    addUpload('z');
    expect(get(uploadsStore).isExpanded).toBe(true);
  });
});
