/**
 * `dataIntegrity.ts` wraps the orphan-cleanup routes. The assertions that matter are the
 * wire shape of the DESTRUCTIVE calls: the storage POST must always carry confirm=true,
 * and `force` must reach the server only when asked for.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockInstance }));

import {
  getSearchOrphanCounts,
  getStorageOrphanCounts,
  getStorageSweepStatus,
  removeSearchOrphans,
  removeStorageOrphans,
} from './dataIntegrity';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getSearchOrphanCounts', () => {
  it('lifts the per-index stats out of the mixed-key response', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        transcripts: { total_docs: 10, orphaned_docs: 2, refused: null },
        speakers: { total_docs: 5, orphaned_docs: 0, refused: 'ratio_guard' },
        summary: { total_orphans_found: 2, refused_indices: { speakers: 'ratio_guard' } },
      },
    });

    const report = await getSearchOrphanCounts();

    expect(mockInstance.get).toHaveBeenCalledWith('/admin/data-integrity/counts');
    expect(report.indices.map((i) => i.name)).toEqual(['transcripts', 'speakers']);
    expect(report.indices[0].orphaned_docs).toBe(2);
    expect(report.summary?.total_orphans_found).toBe(2);
    expect(report.unavailable).toBe(false);
  });

  it('flags an unreachable search backend instead of reporting zero orphans', async () => {
    mockInstance.get.mockResolvedValue({ data: { error: 'OpenSearch not available' } });

    const report = await getSearchOrphanCounts();

    expect(report.unavailable).toBe(true);
    expect(report.indices).toEqual([]);
  });
});

describe('storage reads', () => {
  it('fetches the dry run and the status from the storage routes', async () => {
    mockInstance.get.mockResolvedValue({ data: { orphan_files: 3 } });
    expect((await getStorageOrphanCounts()).orphan_files).toBe(3);
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/data-integrity/storage/counts');

    mockInstance.get.mockResolvedValue({ data: { running: true, last_run: null } });
    expect((await getStorageSweepStatus()).running).toBe(true);
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/data-integrity/storage/status');
  });
});

describe('removeStorageOrphans', () => {
  it('always sends confirm=true and only sends force when asked', async () => {
    mockInstance.post.mockResolvedValue({ data: { status: 'started', task_id: 'task-1' } });

    expect((await removeStorageOrphans(false)).task_id).toBe('task-1');
    expect(mockInstance.post).toHaveBeenLastCalledWith('/admin/data-integrity/storage', undefined, {
      params: { confirm: true },
    });

    expect((await removeStorageOrphans(true)).status).toBe('started');
    expect(mockInstance.post).toHaveBeenLastCalledWith('/admin/data-integrity/storage', undefined, {
      params: { confirm: true, force: true },
    });
  });
});

describe('removeSearchOrphans', () => {
  it('sends no flags unforced, and force with confirm when forced', async () => {
    mockInstance.post.mockResolvedValue({ data: { status: 'already_running' } });

    expect((await removeSearchOrphans(false)).status).toBe('already_running');
    expect(mockInstance.post).toHaveBeenLastCalledWith('/admin/data-integrity', undefined, {
      params: undefined,
    });

    expect((await removeSearchOrphans(true)).status).toBe('already_running');
    expect(mockInstance.post).toHaveBeenLastCalledWith('/admin/data-integrity', undefined, {
      params: { force: true, confirm: true },
    });
  });
});
