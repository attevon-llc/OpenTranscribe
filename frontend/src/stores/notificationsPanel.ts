import { writable } from 'svelte/store';

/**
 * Notification bell panel open/close state.
 *
 * Split out of `notifications.ts` (which used to also hold a notification
 * LIST store): that list was written to by `$stores/downloads` but never read
 * by anything — `NotificationsPanel.svelte` renders exclusively from
 * `$websocketStore.notifications`. This module is the genuinely live half:
 * the panel's visibility, toggled by the navbar bell and closed on
 * outside-click/logout.
 */
export const showNotificationsPanel = writable(false);

/** Toggle the notifications panel open/closed. */
export function toggleNotificationsPanel(): void {
  showNotificationsPanel.update((value) => !value);
}

/**
 * Collapse the panel (called on logout to prevent a stale open panel from
 * leaking into the next user's session — the notification LIST itself lives
 * in `$stores/websocket` and is cleared separately by `websocketStore.clearAll()`).
 */
export function clearAllNotifications(): void {
  showNotificationsPanel.set(false);
}
