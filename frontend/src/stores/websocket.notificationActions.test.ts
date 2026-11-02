/**
 * Pins the notification-list actions of `$stores/websocket` through its public API:
 * the read/remove "regeneration" of a still-processing card, `updateNotification`,
 * `prune`, and the localStorage round trip (Date revival and the retention clock of a
 * read item saved before `readAt` existed). Written against the store, not a module,
 * so it holds across the split of these actions out of `websocket.ts`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));
vi.mock('$lib/axios', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: vi.fn().mockResolvedValue({ status: 200, data: {} }),
  },
  abortAllRequests: vi.fn(),
}));
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((key: string) => key) };
});

import { websocketStore, type Notification } from './websocket';

const list = () => get(websocketStore).notifications;
const stored = (): Array<Record<string, unknown>> =>
  JSON.parse(localStorage.getItem('notifications') ?? '[]');

function addProcessing(progressId = 'p1') {
  websocketStore.addNotification({
    type: 'transcription_status',
    title: 'T',
    message: 'working',
    progressId,
    status: 'processing',
    dismissible: false,
  });
  return list().find((n) => n.progressId === progressId) as Notification;
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  websocketStore.clearAll();
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe('markAsRead', () => {
  it('marks a finished card read and persists it', () => {
    websocketStore.addNotification({ type: 'file_deleted', title: 'T', message: 'gone' });
    const id = list()[0].id;
    websocketStore.markAsRead(id);
    expect(list()[0].read).toBe(true);
    expect(list()[0].readAt).toBeInstanceOf(Date);
    expect(stored()[0].read).toBe(true);
  });

  it('re-flags a still-processing card unread 100 ms later', () => {
    const n = addProcessing();
    websocketStore.markAsRead(n.id);
    expect(list()[0].read).toBe(true);
    vi.advanceTimersByTime(99);
    expect(list()[0].read).toBe(true);
    vi.advanceTimersByTime(1);
    expect(list()[0].read).toBe(false);
    expect(list()[0].readAt).toBeUndefined();
    expect(stored()[0].read).toBe(false);
  });
});

describe('removeNotification', () => {
  it('removes a finished card for good', () => {
    websocketStore.addNotification({ type: 'file_deleted', title: 'T', message: 'gone' });
    websocketStore.removeNotification(list()[0].id);
    vi.advanceTimersByTime(200);
    expect(list()).toEqual([]);
    expect(stored()).toEqual([]);
  });

  it('regenerates a still-processing card under a new id after 100 ms', () => {
    const n = addProcessing();
    websocketStore.removeNotification(n.id);
    expect(list()).toEqual([]);
    vi.advanceTimersByTime(100);
    expect(list()).toHaveLength(1);
    expect(list()[0].progressId).toBe('p1');
    expect(list()[0].id).not.toBe(n.id);
    expect(list()[0].read).toBe(false);
  });
});

describe('addNotification / updateNotification', () => {
  it('folds a repeated progressId into the existing card, keeping id and timestamp', () => {
    const first = addProcessing();
    websocketStore.addNotification({
      type: 'transcription_status',
      title: 'T',
      message: 'more',
      progressId: 'p1',
      status: 'processing',
    });
    expect(list()).toHaveLength(1);
    expect(list()[0]).toMatchObject({ id: first.id, message: 'more' });
    expect(list()[0].timestamp).toBe(first.timestamp);
  });

  it('patches the card with that progressId and persists, and ignores an unknown one', () => {
    addProcessing();
    websocketStore.updateNotification('p1', { message: 'patched', status: 'completed' });
    expect(list()[0]).toMatchObject({ message: 'patched', status: 'completed' });
    expect(stored()[0].message).toBe('patched');
    websocketStore.updateNotification('nope', { message: 'x' });
    expect(list()).toHaveLength(1);
  });
});

describe('markAllAsRead', () => {
  it('keeps the original read time of items already read', () => {
    websocketStore.addNotification({ type: 'file_deleted', title: 'A', message: 'a' });
    websocketStore.markAsRead(list()[0].id);
    const firstReadAt = list()[0].readAt;
    vi.advanceTimersByTime(5000);
    websocketStore.addNotification({ type: 'file_deleted', title: 'B', message: 'b' });
    websocketStore.markAllAsRead();
    const [b, a] = list();
    expect(a.readAt).toBe(firstReadAt);
    expect(b.read).toBe(true);
    expect(b.readAt?.getTime()).toBeGreaterThan(firstReadAt?.getTime() ?? 0);
  });
});

describe('localStorage round trip', () => {
  it('revives dates and starts the retention clock of a legacy read item at load', async () => {
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    localStorage.setItem(
      'notifications',
      JSON.stringify([
        {
          id: 'a',
          type: 'file_deleted',
          title: 'A',
          message: 'a',
          timestamp: '2025-12-31T23:00:00.000Z',
          read: true,
        },
        {
          id: 'b',
          type: 'file_deleted',
          title: 'B',
          message: 'b',
          timestamp: '2025-12-31T22:00:00.000Z',
          read: false,
        },
      ])
    );
    vi.resetModules();
    const fresh = await import('./websocket');
    const [a, b] = get(fresh.websocketStore).notifications;
    expect(a.timestamp).toBeInstanceOf(Date);
    expect(a.timestamp.toISOString()).toBe('2025-12-31T23:00:00.000Z');
    expect(a.readAt?.toISOString()).toBe('2026-01-01T00:00:00.000Z');
    expect(b.readAt).toBeUndefined();
  });

  it('starts empty on unparseable storage', async () => {
    localStorage.setItem('notifications', '{not json');
    vi.resetModules();
    const fresh = await import('./websocket');
    expect(get(fresh.websocketStore).notifications).toEqual([]);
  });
});
