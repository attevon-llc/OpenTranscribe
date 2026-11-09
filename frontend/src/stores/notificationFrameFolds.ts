/**
 * The folds of inbound frames into the notification list: post-processing records, progress
 * cards, silent and regular notifications, and cards rebuilt for tasks recovered on reconnect.
 * Each is one `update` of the websocket store's state; none touches anything but
 * `state.notifications`.
 */
import { get, type Writable } from 'svelte/store';
import { t } from '$stores/locale';
import { generateId } from '$lib/utils/ids';
import {
  advanceRunTiming,
  isTerminalState,
  taskStateFromStatus,
} from '$lib/notifications/runTiming';
import {
  upsertPostProcessingRecord,
  type PostProcessingFrame,
} from '$lib/notifications/postProcessing';
import type { Notification, NotificationData, NotificationType } from '$lib/notifications/types';
import { composeClientMessage, notificationTitle } from '$lib/notifications/titles';
import type { ProgressFields } from '$lib/notifications/progressFrame';
import {
  recoveredNotification,
  type ActiveTask,
  type RecoveredTask,
} from '$lib/notifications/activeTaskRecovery';
import { retain, saveNotificationsToStorage } from './notificationStorage';
import type { WebSocketState } from './websocketNotifications';

// Titles and the takedown notice text live in $lib/notifications/titles.
const getNotificationTitle = (type: string): string => notificationTitle(type, get(t));

/** Frame folds bound to the websocket store's `update`. */
export function createFrameFolds(update: Writable<WebSocketState>['update']) {
  // Fold a post_processing_status frame, or a REST snapshot the card asked for (`force`),
  // into the file's post-processing record ($lib/notifications/postProcessing).
  const applyPostProcessing = (frame: PostProcessingFrame, force = false) => {
    update((s: WebSocketState) => {
      const next = upsertPostProcessingRecord(
        s.notifications,
        frame,
        Date.now(),
        () => generateId('ws'),
        getNotificationTitle('post_processing_status'),
        force
      );
      if (next) {
        s.notifications = retain(next);
        saveNotificationsToStorage(s.notifications);
      }
      return s;
    });
  };

  // Fold a progress frame into its card: update in place and move to the front, or
  // create it ($lib/notifications/progressFrame computed the fields).
  const foldProgressFrame = (type: string, data: NotificationData, fields: ProgressFields) => {
    const { progressId, status, currentStep, progress } = fields;
    update((s: WebSocketState) => {
      // Find existing progressive notification
      const existingIndex = s.notifications.findIndex((n) => n.progressId === progressId);

      const existing = existingIndex !== -1 ? s.notifications[existingIndex] : undefined;
      // Any finished state, including 'done' (redaction) and 'disabled' /
      // 'not_configured' (LLM tasks), may be dismissed; the two creation and
      // update paths used to disagree, leaving a first-frame skip undismissable.
      const dismissible =
        isTerminalState(taskStateFromStatus(status)) ||
        (type === 'youtube_processing_status' && status === 'pending');
      const { timing } = advanceRunTiming(
        existing,
        existing ? existing.data?.status ?? existing.status : undefined,
        status,
        progress.percentage,
        Date.now(),
        typeof data.stage === 'string' ? data.stage : undefined
      );

      if (existing) {
        // Update existing notification with new data (important for summary completion)
        const updatedNotification: Notification = {
          ...existing,
          ...timing,
          message: currentStep,
          timestamp: new Date(),
          currentStep,
          progress,
          status: status as 'processing' | 'completed' | 'error',
          dismissible,
          read: false, // Mark as unread for updates
          readAt: undefined,
          data: {
            ...existing.data,
            ...data,
          }, // Merge old and new data
        };

        // Remove from current position and add to front to maintain most-recent-first ordering
        s.notifications.splice(existingIndex, 1);
        s.notifications.unshift(updatedNotification);
      } else {
        // Create new progressive notification
        const notification: Notification = {
          id: generateId('ws'),
          progressId,
          type: type as NotificationType,
          title: getNotificationTitle(type),
          message: currentStep,
          timestamp: new Date(),
          read: false,
          data,
          ...timing,
          currentStep,
          progress,
          status: status as 'processing' | 'completed' | 'error',
          dismissible,
        };

        s.notifications = retain([notification, ...s.notifications]);
      }

      saveNotificationsToStorage(s.notifications);
      return s;
    });
  };

  const addSilentFrame = (type: string, data: NotificationData) => {
    // Create a silent notification for gallery updates only
    const notification: Notification = {
      id: generateId('ws'),
      type: type as NotificationType,
      title: getNotificationTitle(type),
      message: data.message || get(t)('notifications.galleryUpdate'),
      timestamp: new Date(),
      read: false,
      data,
      dismissible: true,
      silent: true, // This prevents it from showing in notification panel
    };

    // Add notification (MediaLibrary will see it, but NotificationsPanel will filter it out)
    update((s: WebSocketState) => {
      s.notifications = retain([notification, ...s.notifications]);
      // Don't save silent notifications to localStorage to keep it clean
      return s;
    });
  };

  const addRegularFrame = (type: string, data: NotificationData) => {
    // Create a regular notification for other non-progressive types
    const notification: Notification = {
      id: generateId('ws'),
      type: type as NotificationType,
      title: getNotificationTitle(type),
      message:
        composeClientMessage(type, data, get(t)) ||
        data.message ||
        get(t)('notifications.noMessage'),
      timestamp: new Date(),
      read: false,
      data,
      dismissible: true,
    };

    // Add notification
    update((s: WebSocketState) => {
      s.notifications = retain([notification, ...s.notifications]);
      saveNotificationsToStorage(s.notifications);
      return s;
    });
  };

  // Create/update the progress card of a task recovered after a (re)connect
  const upsertRecoveredTask = (task: ActiveTask, recovered: RecoveredTask) => {
    update((s: WebSocketState) => {
      const existingIndex = s.notifications.findIndex((n) => n.progressId === recovered.progressId);

      const notificationData = recoveredNotification(
        task,
        recovered,
        existingIndex !== -1 ? s.notifications[existingIndex].id : generateId('ws'),
        getNotificationTitle(recovered.notificationType)
      );

      if (existingIndex !== -1) {
        s.notifications[existingIndex] = notificationData;
      } else {
        s.notifications = retain([notificationData, ...s.notifications]);
      }

      saveNotificationsToStorage(s.notifications);
      return s;
    });
  };

  return {
    applyPostProcessing,
    foldProgressFrame,
    addSilentFrame,
    addRegularFrame,
    upsertRecoveredTask,
  };
}
