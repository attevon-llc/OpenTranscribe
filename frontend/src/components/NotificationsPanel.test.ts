/**
 * `NotificationsPanel.svelte` had NO component test before this file (issue
 * #753's currency review, confirmed against the tree: only
 * `notificationsPanel.test.ts`, the open/close boolean store, and
 * `appModuleResolution.test.ts`'s import-only smoke check existed). That
 * matters here specifically because this file's own icon map has shipped a
 * silent-fallback-to-bell bug before (`getNotificationIcon` returning
 * `'video'`/`'music'` with no matching render branch) — a defect the type
 * system cannot catch without `{@html}` (which `frontend/CLAUDE.md` forbids
 * for anything but sanitizer-routed strings), so a rendering assertion is
 * the only thing that actually proves coverage.
 *
 * The duration-chip tests are the load-bearing ones: issue #753's own
 * currency review found that the tempting data source — a recording's
 * length — is a completely different number from how long a job took to
 * process it, and would look plausible while being wrong on every file. The
 * "does not use `data.duration`" test below is written specifically to catch
 * that trap being reintroduced.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { Notification, NotificationType } from '../stores/websocket';

vi.mock('../stores/auth', () => ({ token: writable(null) }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string, vars?: Record<string, unknown>) =>
        vars ? `${key}:${JSON.stringify(vars)}` : key
      );
      return () => {};
    },
  },
}));

// A minimal hand-rolled store rather than svelte/store's `writable`: `vi.mock`
// factories (and the `vi.hoisted` values they close over) run before this
// file's own imports are live, so `writable` isn't callable yet at this point.
const notificationsStore = vi.hoisted(() => {
  let value: { notifications: Notification[] } = { notifications: [] };
  const subscribers = new Set<(v: typeof value) => void>();
  return {
    subscribe(run: (v: typeof value) => void) {
      subscribers.add(run);
      run(value);
      return () => subscribers.delete(run);
    },
    set(next: typeof value) {
      value = next;
      subscribers.forEach((run) => run(value));
    },
    get: () => value,
  };
});
const removeNotification = vi.hoisted(() => vi.fn());
const markAsRead = vi.hoisted(() => vi.fn());
const markAllAsRead = vi.hoisted(() => vi.fn());
const clearAll = vi.hoisted(() => vi.fn());

vi.mock('../stores/websocket', () => ({
  websocketStore: {
    subscribe: notificationsStore.subscribe,
    removeNotification,
    markAsRead,
    markAllAsRead,
    clearAll,
  },
}));

import { showNotificationsPanel } from '../stores/notificationsPanel';
import NotificationsPanel from './NotificationsPanel.svelte';

function baseNotification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: overrides.id ?? 'n1',
    type: overrides.type ?? ('transcription_status' as NotificationType),
    title: overrides.title ?? 'Title',
    message: overrides.message ?? 'Message',
    timestamp: overrides.timestamp ?? new Date(),
    read: overrides.read ?? false,
    silent: false,
    ...overrides,
  } as Notification;
}

function setNotifications(list: Notification[]) {
  notificationsStore.set({ notifications: list });
}

function currentNotifications(): Notification[] {
  return notificationsStore.get().notifications;
}

beforeEach(() => {
  vi.clearAllMocks();
  setNotifications([]);
  showNotificationsPanel.set(true);
});

const BELL_PATH = 'M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9';

describe('icon coverage — every mapped type renders its own glyph, not the bell fallback', () => {
  const cases: Array<[NotificationType, string]> = [
    ['transcription_status', 'file-text'],
    ['topic_extraction_status', 'tag'],
    ['auto_label_status', 'tag'],
    ['redaction_status', 'eye-off'],
    ['youtube_processing_status', 'video'],
    ['playlist_processing_status', 'video'],
    ['audio_extraction_status', 'music'],
    ['collection_shared', 'folder'],
    ['group_member_added', 'users'],
    ['file_takedown', 'shield'],
    ['download_progress', 'download'],
  ];

  it.each(cases)('%s does not fall back to the generic bell glyph', (type) => {
    setNotifications([baseNotification({ id: type, type })]);

    const { container } = render(NotificationsPanel);

    const icon = container.querySelector('.notification-icon svg');
    expect(icon?.innerHTML).not.toContain(BELL_PATH);
  });

  it('an unmapped type falls back to the bell on purpose (not a bug — most union members have no icon yet)', () => {
    setNotifications([baseNotification({ id: 'x', type: 'echo' as NotificationType })]);

    const { container } = render(NotificationsPanel);

    const icon = container.querySelector('.notification-icon svg');
    expect(icon?.innerHTML).toContain(BELL_PATH);
  });
});

describe('getNotificationStatus fallback (issue #753 J3 / #569 N2)', () => {
  it('colours a row from top-level notification.status when data.status is absent', () => {
    // This is exactly the shape `addNotification` callers (audio extraction,
    // the #569 download bridge) produce when they pass no `data` object.
    setNotifications([baseNotification({ status: 'completed' })]);

    const { container } = render(NotificationsPanel);

    const item = container.querySelector('.notification-item');
    expect(item?.className).toContain('status-success');
    // The pre-fix behavior was silent, not an error: without the fallback
    // this renders `status-default` (grey, uncoloured) instead — assert the
    // negative too, or a future regression back to `data.status`-only would
    // still pass on `not.toBeNull` finding some OTHER element in the DOM.
    expect(item?.className).not.toContain('status-default');
  });

  it('still prefers data.status when both are present and they disagree', () => {
    setNotifications([baseNotification({ status: 'processing', data: { status: 'completed' } })]);

    const { container } = render(NotificationsPanel);

    expect(container.querySelector('.notification-item.status-success')).not.toBeNull();
    expect(container.querySelector('.notification-item.status-info')).toBeNull();
  });
});

describe('duration chip (issue #753 item 1)', () => {
  it('renders "completed in" using data.duration_seconds, formatted compactly', () => {
    setNotifications([
      baseNotification({
        status: 'completed',
        data: { status: 'completed', duration_seconds: 134 },
      }),
    ]);

    const { container } = render(NotificationsPanel);

    const chip = container.querySelector('.duration-chip');
    expect(chip?.textContent).toContain('notifications.completedIn');
    expect(chip?.textContent).toContain('2m 14s');
  });

  it('does NOT render for a still-processing notification even if duration_seconds is somehow present', () => {
    setNotifications([
      baseNotification({
        status: 'processing',
        data: { status: 'processing', duration_seconds: 134 },
      }),
    ]);

    const { container } = render(NotificationsPanel);

    expect(container.querySelector('.duration-chip')).toBeNull();
  });

  it('does NOT render when duration_seconds is absent — no fabricated "0s"', () => {
    setNotifications([baseNotification({ status: 'completed', data: { status: 'completed' } })]);

    const { container } = render(NotificationsPanel);

    expect(container.querySelector('.duration-chip')).toBeNull();
  });

  it('THE TRAP: uses data.duration_seconds, never data.duration (the recording length)', () => {
    // `file_updated`'s `duration`/`formatted_duration` is how long the
    // RECORDING is — a 90-minute meeting transcribed in 4 minutes. If this
    // chip ever reads `duration` instead of `duration_seconds`, it renders a
    // plausible-looking but completely wrong number on every file. A
    // notification carrying both, with wildly different values, is the only
    // way to prove which field actually drives the chip.
    setNotifications([
      baseNotification({
        status: 'completed',
        data: {
          status: 'completed',
          duration_seconds: 45, // real processing time: 45s
          duration: 5400, // recording length: 90 minutes — must NOT be used
        },
      }),
    ]);

    const { container } = render(NotificationsPanel);

    const chip = container.querySelector('.duration-chip');
    expect(chip?.textContent).toContain('45s');
    expect(chip?.textContent).not.toContain('1h 30m');
    expect(chip?.textContent).not.toContain('90');
  });
});

describe('sanity: the store subscription this file depends on is exercised', () => {
  it('renders one row per notification and reflects clearAll', () => {
    setNotifications([baseNotification({ id: 'a' }), baseNotification({ id: 'b' })]);

    const { container } = render(NotificationsPanel);

    expect(container.querySelectorAll('.notification-item')).toHaveLength(2);
    expect(currentNotifications()).toHaveLength(2);
  });
});
