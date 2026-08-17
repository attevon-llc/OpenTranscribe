import { writable, get } from 'svelte/store';
import { generateId } from '$lib/utils/ids';

// Create a store for the notifications panel visibility
export const showNotificationsPanel = writable(false);

// Notifications data store
export interface Notification {
  id: string;
  title: string;
  message: string;
  type: 'info' | 'success' | 'warning' | 'error';
  timestamp: Date;
  read: boolean;
  data?: {
    file_id?: string;
    [key: string]: unknown;
  };
}

// Seeded empty. This store previously shipped three hard-coded English demo
// notifications ("Transcription Complete", "New Comment", "System Update") that
// no locale could translate. Real entries are pushed by `addNotification`
// (see $stores/downloads), which already localizes its title/message.
export const notifications = writable<Notification[]>([]);

// Helper to toggle notification panel
export function toggleNotificationsPanel(): void {
  showNotificationsPanel.update((value) => !value);
}

// Helper to mark notifications as read
export function markAllAsRead(): void {
  notifications.update((items) => {
    return items.map((item) => ({ ...item, read: true }));
  });
}

// Add a notification
export function addNotification(notification: Omit<Notification, 'id' | 'timestamp'>): void {
  const id = generateId('notification');
  const timestamp = new Date();

  notifications.update((items) => {
    return [{ id, timestamp, ...notification }, ...items];
  });
}

// Get notifications (async function for API compatibility)
export async function getNotifications(): Promise<Notification[]> {
  // Return the current value of the notifications store. `get()` reads a
  // store's value via a subscribe-then-immediately-unsubscribe internally,
  // without the self-referencing-callback TDZ hazard of hand-rolling that
  // pattern (svelte's subscribe() invokes its callback SYNCHRONOUSLY, before
  // a `const`/`let` assignment capturing its own unsubscribe fn completes).
  return get(notifications);
}

// Remove a notification
export function removeNotification(id: string): void {
  notifications.update((items) => items.filter((item) => item.id !== id));
}

// Clear all notifications (called on logout to prevent stale notifications
// from leaking to the next user's session)
export function clearAllNotifications(): void {
  notifications.set([]);
  showNotificationsPanel.set(false);
}
