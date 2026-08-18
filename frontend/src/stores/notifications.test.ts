/**
 * `notifications.ts` backs the notification bell/panel. These tests focus on:
 * `addNotification`'s id/timestamp stamping (id comes from the shared
 * `generateId` helper, prefixed `'notification'`), `clearAllNotifications`'s
 * logout-safety contract (it must also collapse the panel, not just empty
 * the list, per the function's own doc comment), and `getNotifications()`.
 *
 * `getNotifications()` used to be broken: `const unsubscribe =
 * notifications.subscribe(cb)` had `cb` call `unsubscribe()` on itself, but
 * Svelte's `writable().subscribe()` invokes `cb` SYNCHRONOUSLY, before the
 * `const` assignment finished — a TDZ `ReferenceError` that permanently
 * corrupted the store (the dead subscriber stayed registered and re-threw on
 * every later `.set()`/`.update()`). `grep -rn getNotifications src/` shows
 * zero production callers, which is why this went uncaught. Fixed by
 * hoisting `unsubscribe` to a `let` declared before `subscribe()` runs.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

const mockGenerateId = vi.hoisted(() => vi.fn());
vi.mock('$lib/utils/ids', () => ({ generateId: mockGenerateId }));

import {
  notifications,
  showNotificationsPanel,
  addNotification,
  removeNotification,
  clearAllNotifications,
  toggleNotificationsPanel,
  markAllAsRead,
} from './notifications';

beforeEach(() => {
  vi.clearAllMocks();
  mockGenerateId.mockReturnValue('notification-fixed-id');
  notifications.set([]);
  showNotificationsPanel.set(false);
});

describe('addNotification', () => {
  it('stamps an id from generateId("notification") and a Date timestamp', () => {
    addNotification({
      title: 'Done',
      message: 'Transcription complete',
      type: 'success',
      read: false,
    });

    expect(mockGenerateId).toHaveBeenCalledWith('notification');
    const [item] = get(notifications);
    expect(item.id).toBe('notification-fixed-id');
    expect(item.timestamp).toBeInstanceOf(Date);
    expect(item.title).toBe('Done');
  });

  it('prepends new notifications so the newest is first', () => {
    mockGenerateId.mockReturnValueOnce('id-1').mockReturnValueOnce('id-2');

    addNotification({ title: 'First', message: 'm1', type: 'info', read: false });
    addNotification({ title: 'Second', message: 'm2', type: 'info', read: false });

    const items = get(notifications);
    expect(items.map((n) => n.id)).toEqual(['id-2', 'id-1']);
  });
});

describe('getNotifications', () => {
  it('resolves with the current notification list without corrupting the store', async () => {
    vi.resetModules();
    const fresh = await import('./notifications');

    fresh.addNotification({ title: 'One', message: 'm', type: 'info', read: false });
    await expect(fresh.getNotifications()).resolves.toMatchObject([{ title: 'One' }]);

    // A second call must still work — the subscriber from the first call
    // must have actually unsubscribed, not stayed registered and corrupted
    // the store (the original TDZ bug's failure mode).
    fresh.addNotification({ title: 'Two', message: 'm', type: 'info', read: false });
    await expect(fresh.getNotifications()).resolves.toMatchObject([
      { title: 'Two' },
      { title: 'One' },
    ]);
  });
});

describe('removeNotification', () => {
  it('removes only the matching id', () => {
    mockGenerateId.mockReturnValueOnce('id-1').mockReturnValueOnce('id-2');
    addNotification({ title: 'First', message: 'm1', type: 'info', read: false });
    addNotification({ title: 'Second', message: 'm2', type: 'info', read: false });

    removeNotification('id-1');

    expect(get(notifications).map((n) => n.id)).toEqual(['id-2']);
  });

  it('is a no-op for an id that does not exist', () => {
    addNotification({ title: 'A', message: 'a', type: 'info', read: false });
    const before = get(notifications);

    removeNotification('missing-id');

    expect(get(notifications)).toEqual(before);
  });
});

describe('clearAllNotifications', () => {
  it('empties the list AND collapses the panel — logout-safety, not just a data reset', () => {
    addNotification({ title: 'A', message: 'a', type: 'info', read: false });
    showNotificationsPanel.set(true);

    clearAllNotifications();

    expect(get(notifications)).toEqual([]);
    expect(get(showNotificationsPanel)).toBe(false);
  });
});

describe('toggleNotificationsPanel', () => {
  it('flips panel visibility on each call', () => {
    expect(get(showNotificationsPanel)).toBe(false);

    toggleNotificationsPanel();
    expect(get(showNotificationsPanel)).toBe(true);

    toggleNotificationsPanel();
    expect(get(showNotificationsPanel)).toBe(false);
  });
});

describe('markAllAsRead', () => {
  it('marks every notification read without touching other fields or order', () => {
    mockGenerateId.mockReturnValueOnce('id-1').mockReturnValueOnce('id-2');
    addNotification({ title: 'First', message: 'm1', type: 'info', read: false });
    addNotification({ title: 'Second', message: 'm2', type: 'info', read: false });

    markAllAsRead();

    const items = get(notifications);
    expect(items.every((n) => n.read)).toBe(true);
    expect(items.map((n) => n.title)).toEqual(['Second', 'First']);
  });
});
