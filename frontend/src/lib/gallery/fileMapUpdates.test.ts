import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { writable } from 'svelte/store';

const h = vi.hoisted(() => ({ invalidate: vi.fn() }));
vi.mock('$lib/apiCache', () => ({ apiCache: { invalidate: h.invalidate } }));
vi.mock('$stores/websocket', async () => {
  const { writable } = await import('svelte/store');
  return { websocketStore: writable({ notifications: [] }) };
});
vi.mock('$stores/uploads', async () => {
  const { writable } = await import('svelte/store');
  return { uploadsStore: writable({ uploads: [] }) };
});

import { websocketStore } from '$stores/websocket';
import { uploadsStore } from '$stores/uploads';
import {
  applyNotificationBatch,
  createNotificationBatcher,
  createThrottledRefresher,
  markHeldRows,
  markQuarantinedRows,
  totalsAfterAdd,
  totalsAfterRemove,
  type GalleryNotification,
} from './fileMapUpdates';

const t = (key: string) => `T:${key}`;
const file = (uuid: string, extra = {}) =>
  ({ uuid, status: 'pending', display_status: 'p', ...extra }) as never;

function run(notifications: GalleryNotification[], map: Map<string, unknown>) {
  const handlers = { removeFile: vi.fn(), addNewFile: vi.fn(), refresh: vi.fn() };
  const updated = applyNotificationBatch(notifications, map as never, t, handlers);
  return { updated, handlers };
}

describe('applyNotificationBatch', () => {
  it('updates status and a translated display label, and schedules one refresh for errors', () => {
    const map = new Map([['1', file('1')]]);
    const { updated, handlers } = run(
      [
        { id: 'n1', type: 'transcription_status', data: { file_id: 1, status: 'completed' } },
        { id: 'n2', type: 'transcription_status', data: { file_id: 1, status: 'error' } },
        { id: 'n3', type: 'transcription_status', data: { file_id: 9, status: 'error' } },
      ],
      map
    );
    expect(updated).toBe(true);
    expect(map.get('1')).toMatchObject({ status: 'error', display_status: 'T:common.error' });
    expect(handlers.refresh).toHaveBeenCalledTimes(1);
    expect(handlers.refresh).toHaveBeenCalledWith('error-batch', 1000);
  });

  it('ignores updates for files outside the loaded pages', () => {
    const { updated } = run(
      [{ id: 'n', type: 'file_updated', data: { file_id: 5, status: 'completed' } }],
      new Map()
    );
    expect(updated).toBe(false);
  });

  it('merges a full file payload, or just the changed fields', () => {
    const map = new Map([['1', file('1', { thumbnail_url: 'old' })]]);
    run([{ id: 'a', type: 'file_updated', data: { file_id: 1, file: { title: 'New' } } }], map);
    expect(map.get('1')).toMatchObject({ title: 'New', thumbnail_url: 'old' });
    run([{ id: 'b', type: 'file_updated', data: { file_id: 1, thumbnail_url: 'new' } }], map);
    expect(map.get('1')).toMatchObject({ title: 'New', thumbnail_url: 'new', status: 'pending' });
  });

  it('routes deletes, creations and cache invalidation to the handlers', () => {
    const { handlers } = run(
      [
        { id: 'a', type: 'file_deleted', data: { file_id: 7 } },
        { id: 'b', type: 'file_created', data: { file: { uuid: 'n' } } },
        { id: 'c', type: 'file_upload', data: {} },
        { id: 'd', type: 'cache_invalidate', data: { scope: 'files' } },
        { id: 'e', type: 'cache_invalidate', data: { scope: 'tags' } },
      ],
      new Map()
    );
    expect(handlers.removeFile).toHaveBeenCalledWith('7');
    expect(handlers.addNewFile).toHaveBeenCalledWith({ uuid: 'n' });
    expect(handlers.refresh).toHaveBeenCalledWith('new-file', 500);
    expect(handlers.refresh).toHaveBeenCalledWith('cache-invalidate', 500);
    expect(h.invalidate).toHaveBeenCalledTimes(1);
    expect(h.invalidate).toHaveBeenCalledWith('files:');
  });
});

describe('totals', () => {
  const s = { currentPage: 1, pageSize: 50, totalFiles: 100, totalPages: 2, hasMoreFiles: true };
  it('adds one on page 1 without changing the page count', () => {
    expect(totalsAfterAdd(s)).toEqual({
      page: 1,
      pageSize: 50,
      total: 101,
      totalPages: 2,
      hasMore: true,
    });
  });
  it('off page 1 recomputes the page count and assumes there is more', () => {
    expect(totalsAfterAdd({ ...s, currentPage: 2, totalFiles: 100 })).toEqual({
      page: 2,
      pageSize: 50,
      total: 101,
      totalPages: 3,
      hasMore: true,
    });
  });
  it('never goes below zero when removing', () => {
    expect(totalsAfterRemove({ ...s, totalFiles: 1 }, 5)).toEqual({
      page: 1,
      pageSize: 50,
      total: 0,
      totalPages: 0,
      hasMore: false,
    });
    expect(totalsAfterRemove(s, 10)).toMatchObject({ total: 90, totalPages: 2, hasMore: true });
  });
});

describe('row markers', () => {
  it('flags only the named rows', () => {
    const rows = [file('a'), file('b')];
    expect(
      markQuarantinedRows(rows, ['b']).map(
        (f) => (f as { is_quarantined?: boolean }).is_quarantined
      )
    ).toEqual([undefined, true]);
    const held = markHeldRows(rows, ['a'], 'platform');
    expect((held[0] as { hold?: unknown }).hold).toBeTruthy();
    expect((held[1] as { hold?: unknown }).hold).toBeUndefined();
  });
});

describe('createThrottledRefresher', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('collapses calls on one key and keeps keys independent', () => {
    const run = vi.fn();
    const r = createThrottledRefresher(run);
    r.refresh('a', 100);
    r.refresh('a', 100);
    r.refresh('b', 100);
    vi.advanceTimersByTime(100);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it('cancelAll drops pending refreshes', () => {
    const run = vi.fn();
    const r = createThrottledRefresher(run);
    r.refresh('a', 100);
    r.cancelAll();
    vi.advanceTimersByTime(500);
    expect(run).not.toHaveBeenCalled();
  });
});

describe('createNotificationBatcher', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    (websocketStore as unknown as ReturnType<typeof writable>).set({ notifications: [] });
    (uploadsStore as unknown as ReturnType<typeof writable>).set({ uploads: [] });
  });
  afterEach(() => vi.useRealTimers());

  it('batches new notifications after 250 ms of quiet and never re-delivers a seen id', () => {
    const onBatch = vi.fn();
    const stop = createNotificationBatcher(onBatch, vi.fn());
    const ws = websocketStore as unknown as ReturnType<typeof writable>;
    ws.set({ notifications: [{ id: '1', type: 'x' }] });
    ws.set({
      notifications: [
        { id: '1', type: 'x' },
        { id: '2', type: 'x' },
      ],
    });
    vi.advanceTimersByTime(249);
    expect(onBatch).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onBatch).toHaveBeenCalledTimes(1);
    expect(onBatch.mock.calls[0][0].map((n: { id: string }) => n.id)).toEqual(['1', '2']);
    ws.set({ notifications: [{ id: '2', type: 'x' }] });
    vi.advanceTimersByTime(1000);
    expect(onBatch).toHaveBeenCalledTimes(1);
    stop();
  });

  it('fires onUploadsFinished only when active uploads drop to zero', () => {
    const finished = vi.fn();
    const stop = createNotificationBatcher(vi.fn(), finished);
    const up = uploadsStore as unknown as ReturnType<typeof writable>;
    up.set({ uploads: [{ status: 'uploading' }] });
    expect(finished).not.toHaveBeenCalled();
    up.set({ uploads: [{ status: 'completed' }] });
    expect(finished).toHaveBeenCalledTimes(1);
    stop();
  });

  it('stop cancels a pending flush', () => {
    const onBatch = vi.fn();
    const stop = createNotificationBatcher(onBatch, vi.fn());
    (websocketStore as unknown as ReturnType<typeof writable>).set({
      notifications: [{ id: '9', type: 'x' }],
    });
    stop();
    vi.advanceTimersByTime(1000);
    expect(onBatch).not.toHaveBeenCalled();
  });
});
