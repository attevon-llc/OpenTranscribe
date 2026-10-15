import { get, writable } from 'svelte/store';
import { showNotificationsPanel } from '$stores/notificationsPanel';
import {
  DEFAULT_RETENTION,
  RETENTION_SETTINGS,
  type RetentionSetting,
} from '$lib/notifications/retention';

/**
 * "Keep notifications for" (issue #1217). A client UI preference persisted in
 * localStorage next to the notification list, and — like theme and locale —
 * deliberately NOT cleared by `clearUserState`: it describes how this browser
 * behaves, not any user's data.
 */
export const RETENTION_STORAGE_KEY = 'opentr:notificationRetention';

function readSetting(): RetentionSetting {
  try {
    const raw = typeof window !== 'undefined' ? localStorage.getItem(RETENTION_STORAGE_KEY) : null;
    return RETENTION_SETTINGS.includes(raw as RetentionSetting)
      ? (raw as RetentionSetting)
      : DEFAULT_RETENTION;
  } catch {
    return DEFAULT_RETENTION;
  }
}

const settingStore = writable<RetentionSetting>(readSetting());

export const notificationRetention = {
  subscribe: settingStore.subscribe,
  set(value: RetentionSetting): void {
    if (!RETENTION_SETTINGS.includes(value)) return;
    settingStore.set(value);
    try {
      localStorage.setItem(RETENTION_STORAGE_KEY, value);
    } catch {
      // Private browsing / quota: the choice still applies for this page load.
    }
  },
};

const unseenStore = writable(0);
const shownStore = writable(0);

// What the panel tells the user is fixed when it OPENS and acknowledged when it
// CLOSES, in the store rather than in a component: the panel is mounted from two
// places, and a prune that runs as the panel closes belongs to the NEXT open.
showNotificationsPanel.subscribe((open) => {
  if (open) {
    shownStore.set(get(unseenStore));
  } else {
    unseenStore.update((c) => Math.max(0, c - get(shownStore)));
    shownStore.set(0);
  }
});

/**
 * The count of visible notifications a prune removed that the user has not yet
 * been told about. `subscribe` yields the number to display right now (0 while
 * the panel is closed).
 */
export const clearedNotice = {
  subscribe: shownStore.subscribe,
  add(n: number): void {
    if (n > 0) unseenStore.update((c) => c + n);
  },
  dismiss(): void {
    unseenStore.update((c) => Math.max(0, c - get(shownStore)));
    shownStore.set(0);
  },
  reset(): void {
    unseenStore.set(0);
    shownStore.set(0);
  },
};
