/**
 * Store-level behaviour of notification retention (issue #1217): what the
 * websocket store does with the pure policy in `$lib/notifications/retention`.
 *
 * The policy itself is pinned in `retention.test.ts`; this file proves the
 * WIRING, which is where such features rot: that read time is recorded, that the
 * prune runs on add/load/panel-close and NOT while the panel is open, that the
 * user's setting is honoured, and that the "N cleared" count is reported.
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

import { websocketStore } from './websocket';
import { showNotificationsPanel } from './notificationsPanel';
import {
  RETENTION_STORAGE_KEY,
  clearedNotice,
  notificationRetention,
} from './notificationRetention';

const HOUR = 3_600_000;
const T0 = new Date('2026-10-11T12:00:00Z');

/** What the panel would announce if opened now; closes it again afterwards. */
function noticeOnNextOpen() {
  showNotificationsPanel.set(true);
  const shown = get(clearedNotice);
  showNotificationsPanel.set(false);
  return shown;
}

function list() {
  return get(websocketStore).notifications;
}

/** A finished, visible, clearable notification. */
function addDone(tag: string) {
  websocketStore.addNotification({
    type: 'audio_extraction_status',
    title: 'Audio Extraction',
    message: tag,
    progressId: `done-${tag}`,
    status: 'completed',
    dismissible: true,
  });
  return list()[0].id;
}

function addError(tag: string) {
  websocketStore.addNotification({
    type: 'audio_extraction_status',
    title: 'Audio Extraction',
    message: tag,
    progressId: `err-${tag}`,
    status: 'error',
    dismissible: true,
  });
  return list()[0].id;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  localStorage.clear();
  showNotificationsPanel.set(false);
  notificationRetention.set('read24h');
  websocketStore.clearAll();
  clearedNotice.reset();
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe('read time', () => {
  it('markAsRead stamps readAt; markAllAsRead stamps unread rows and keeps earlier read times', () => {
    const first = addDone('a');
    websocketStore.markAsRead(first);
    const firstReadAt = list().find((n) => n.id === first)!.readAt;
    expect(firstReadAt?.getTime()).toBe(T0.getTime());

    vi.setSystemTime(new Date(T0.getTime() + HOUR));
    const second = addDone('b');
    websocketStore.markAllAsRead();

    expect(
      list()
        .find((n) => n.id === second)!
        .readAt?.getTime()
    ).toBe(T0.getTime() + HOUR);
    expect(
      list()
        .find((n) => n.id === first)!
        .readAt?.getTime()
    ).toBe(T0.getTime());
  });

  it('a progress update that re-marks a row unread clears its read time', () => {
    const id = addDone('a');
    websocketStore.markAsRead(id);
    websocketStore.addNotification({
      type: 'audio_extraction_status',
      title: 'Audio Extraction',
      message: 'a',
      progressId: 'done-a',
      status: 'completed',
      dismissible: true,
    });
    const row = list().find((n) => n.id === id)!;
    expect(row.read).toBe(false);
    expect(row.readAt).toBeUndefined();
  });
});

describe('prune on add', () => {
  it('removes a read success 24 h after reading when the next notification arrives', () => {
    const old = addDone('old');
    websocketStore.markAsRead(old);

    vi.setSystemTime(new Date(T0.getTime() + 25 * HOUR));
    addDone('new');

    expect(list().map((n) => n.id)).not.toContain(old);
    expect(noticeOnNextOpen()).toBe(1);
  });

  it('never prunes an error, an unread row, or a processing row', () => {
    const err = addError('e');
    websocketStore.markAsRead(err);
    const unread = addDone('u');
    websocketStore.addNotification({
      type: 'transcription_status',
      title: 'T',
      message: 'working',
      progressId: 'p-run',
      status: 'processing',
      dismissible: false,
    });
    const processing = list()[0].id;
    websocketStore.markAsRead(processing);

    vi.setSystemTime(new Date(T0.getTime() + 10 * 24 * HOUR));
    addDone('trigger');

    const ids = list().map((n) => n.id);
    expect(ids).toEqual(expect.arrayContaining([err, unread, processing]));
  });

  it("honours the 'never' setting", () => {
    notificationRetention.set('never');
    const old = addDone('old');
    websocketStore.markAsRead(old);

    vi.setSystemTime(new Date(T0.getTime() + 90 * 24 * HOUR));
    addDone('new');

    expect(list().map((n) => n.id)).toContain(old);
    expect(noticeOnNextOpen()).toBe(0);
  });
});

describe('panel-open deferral', () => {
  it('removes nothing while the panel is open and prunes when it closes', () => {
    const old = addDone('old');
    websocketStore.markAsRead(old);
    vi.setSystemTime(new Date(T0.getTime() + 30 * HOUR));

    showNotificationsPanel.set(true);
    addDone('arrives-while-open');
    expect(list().map((n) => n.id)).toContain(old);
    expect(get(clearedNotice)).toBe(0);

    showNotificationsPanel.set(false);
    expect(list().map((n) => n.id)).not.toContain(old);
    expect(noticeOnNextOpen()).toBe(1);
  });
});

describe('prune on load', () => {
  async function loadFresh() {
    vi.resetModules();
    const mod = await import('./websocket');
    return get(mod.websocketStore).notifications;
  }

  function stored(over: Record<string, unknown>) {
    return {
      id: `s${Math.random()}`,
      type: 'audio_extraction_status',
      title: 't',
      message: 'm',
      timestamp: new Date(T0.getTime() - 40 * 24 * HOUR).toISOString(),
      read: false,
      status: 'completed',
      ...over,
    };
  }

  it('drops stale read successes and 14-day-old unread ones, keeps errors and legacy read rows', async () => {
    const readLongAgo = stored({
      id: 'read-old',
      read: true,
      readAt: new Date(T0.getTime() - 3 * 24 * HOUR).toISOString(),
    });
    const unreadAncient = stored({ id: 'unread-ancient' });
    const errorAncient = stored({ id: 'err', status: 'error', read: true });
    // Saved before readAt existed: its clock starts at load, so it must survive.
    const legacyRead = stored({ id: 'legacy', read: true });
    localStorage.setItem(
      'notifications',
      JSON.stringify([readLongAgo, unreadAncient, errorAncient, legacyRead])
    );

    const loaded = await loadFresh();

    expect(loaded.map((n) => n.id).sort()).toEqual(['err', 'legacy']);
  });
});

describe('the persisted setting', () => {
  it('persists to localStorage and ignores junk values', () => {
    notificationRetention.set('read7d');
    expect(localStorage.getItem(RETENTION_STORAGE_KEY)).toBe('read7d');
    notificationRetention.set('forever' as never);
    expect(get(notificationRetention)).toBe('read7d');
  });

  it('falls back to the 24 h default for a corrupt stored value', async () => {
    localStorage.setItem(RETENTION_STORAGE_KEY, 'garbage');
    vi.resetModules();
    const fresh = await import('./notificationRetention');
    expect(get(fresh.notificationRetention)).toBe('read24h');
  });
});

describe('cleared-notice count', () => {
  it('is fixed at open, dismissed once, and a prune that ran at close waits for the next open', () => {
    clearedNotice.add(3);
    expect(get(clearedNotice)).toBe(0); // nothing is shown while the panel is closed

    showNotificationsPanel.set(true);
    expect(get(clearedNotice)).toBe(3);

    // Closing acknowledges only what was shown; a prune landing at close is kept.
    clearedNotice.add(2);
    showNotificationsPanel.set(false);
    expect(noticeOnNextOpen()).toBe(2);
  });

  it('dismiss removes the note for this and later opens', () => {
    clearedNotice.add(4);
    showNotificationsPanel.set(true);
    clearedNotice.dismiss();
    expect(get(clearedNotice)).toBe(0);
    showNotificationsPanel.set(false);
    expect(noticeOnNextOpen()).toBe(0);
  });
});
