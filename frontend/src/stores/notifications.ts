import { writable } from 'svelte/store';
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

// Sample notifications for demonstration
const sampleNotifications: Notification[] = [
  {
    id: 'notification-1',
    title: 'Transcription Complete',
    message: 'Your audio file has been successfully transcribed.',
    type: 'success',
    timestamp: new Date(Date.now() - 3600000), // 1 hour ago
    read: false,
    data: { file_id: '1' },
  },
  {
    id: 'notification-2',
    title: 'New Comment',
    message: 'Someone commented on your transcript.',
    type: 'info',
    timestamp: new Date(Date.now() - 86400000), // 1 day ago
    read: true,
    data: { file_id: '2' },
  },
  {
    id: 'notification-3',
    title: 'System Update',
    message: 'The application has been updated with new features.',
    type: 'info',
    timestamp: new Date(Date.now() - 172800000), // 2 days ago
    read: true,
  },
];

// Create a store for notifications
export const notifications = writable<Notification[]>(sampleNotifications);

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
  // Return the current value of the notifications store
  return new Promise((resolve) => {
    const unsubscribe = notifications.subscribe((value) => {
      unsubscribe();
      resolve(value);
    });
  });
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
