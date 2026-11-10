/**
 * Picks the websocket notification the file-detail page should act on: the newest one for
 * this file, and only when its content changed since the last one handled. The dispatch
 * itself is `notificationHandler.handleFileNotification`.
 */
import type { Notification } from '$stores/websocket';

/** The most recent notification (by timestamp) whose `data.file_id` is this file. */
export function latestNotificationForFile(
  notifications: Notification[],
  fileId: string
): Notification | null {
  const forFile = notifications.filter((n) => String(n.data?.file_id || '') === String(fileId));
  if (forFile.length === 0) return null;
  forFile.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());
  return forFile[0] ?? null;
}

/**
 * A signature of a notification's content, not just its id: progressive notifications
 * update in place, so the same id carries new status, progress and step values.
 */
export function notificationSignature(n: Notification): string {
  return `${n.id}_${n.status}_${n.progress?.percentage}_${n.currentStep}_${n.timestamp}`;
}

/** Whether `n` is for `fileId`, compared as strings and never matching an unset id. */
export function isForFile(n: Notification, fileId: string): boolean {
  const notificationFileId = String(n.data?.file_id);
  const currentFileId = String(fileId);
  return (
    notificationFileId === currentFileId &&
    notificationFileId !== 'undefined' &&
    currentFileId !== 'undefined'
  );
}
