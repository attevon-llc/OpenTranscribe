/**
 * `notificationsPanel.ts` backs the notification bell's open/close state only.
 *
 * It used to live inside `notifications.ts` alongside a notification LIST store
 * (`addNotification`/`getNotifications`/`removeNotification`), but that list was
 * written to by `$stores/downloads` and read by nothing — `NotificationsPanel.svelte`
 * renders exclusively from `$websocketStore.notifications`. The list management was
 * deleted as dead code (issue G9); this file covers what's left, which is genuinely
 * live: the panel's visibility.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';

import {
  showNotificationsPanel,
  toggleNotificationsPanel,
  clearAllNotifications,
} from './notificationsPanel';

beforeEach(() => {
  showNotificationsPanel.set(false);
});

describe('showNotificationsPanel', () => {
  it('starts closed', () => {
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

describe('clearAllNotifications', () => {
  it('collapses an open panel — logout-safety, so a stale open panel does not leak into the next session', () => {
    showNotificationsPanel.set(true);

    clearAllNotifications();

    expect(get(showNotificationsPanel)).toBe(false);
  });
});
