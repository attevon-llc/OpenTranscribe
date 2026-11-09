/**
 * Persistence and retention of the notification list: what is read from and written to
 * localStorage, and the retention policy (issue #1217) applied to a list.
 */
import { get } from 'svelte/store';
import { pruneNotifications } from '$lib/notifications/retention';
import { notificationRetention, clearedNotice } from '$stores/notificationRetention';
import { showNotificationsPanel } from '$stores/notificationsPanel';
import type { Notification } from '$lib/notifications/types';

/**
 * Apply the retention policy (issue #1217) to a list. Pure policy lives in
 * `$lib/notifications/retention`; this only supplies the clock, the user's
 * setting and the panel state, and tells the user later what was removed.
 * While the panel is open it removes nothing, so rows never vanish mid-read.
 */
export function retain(list: Notification[]): Notification[] {
  const { kept, removedVisible } = pruneNotifications(list, {
    now: Date.now(),
    retention: get(notificationRetention),
    panelOpen: get(showNotificationsPanel),
  });
  clearedNotice.add(removedVisible);
  return kept;
}

// Load notifications from localStorage if available
export const loadNotificationsFromStorage = (): Notification[] => {
  if (typeof window !== 'undefined') {
    try {
      const stored = localStorage.getItem('notifications');
      if (stored) {
        const parsed: Array<Notification & { timestamp: string; readAt?: string }> =
          JSON.parse(stored);
        // Convert timestamp strings back to Date objects. A read item saved before
        // `readAt` existed starts its retention clock now rather than being
        // judged against a read time nobody recorded.
        const loadedAt = new Date();
        return retain(
          parsed.map((n) => ({
            ...n,
            timestamp: new Date(n.timestamp),
            readAt: n.readAt ? new Date(n.readAt) : n.read ? loadedAt : undefined,
          }))
        );
      }
    } catch (error) {
      console.warn('Failed to load notifications from localStorage:', error);
    }
  }
  return [];
};

// Save notifications to localStorage
export const saveNotificationsToStorage = (notifications: Notification[]) => {
  if (typeof window !== 'undefined') {
    try {
      localStorage.setItem('notifications', JSON.stringify(notifications));
    } catch (error) {
      console.warn('Failed to save notifications to localStorage:', error);
    }
  }
};
