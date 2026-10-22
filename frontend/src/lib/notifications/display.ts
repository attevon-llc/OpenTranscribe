/**
 * Presentational helpers shared by the notification panel's rows and processing cards.
 * Pure: the translator and the clock are passed in.
 */
import type { Notification, NotificationType } from '$stores/websocket';

type Translate = (key: string, options?: Record<string, unknown>) => string;

/**
 * One icon registry (issue #753): every glyph has exactly one name here, and
 * `components/notifications/NotificationIcon.svelte` has a render branch per name. A name
 * with no branch silently shows the bell, which `NotificationsPanel.test.ts` catches.
 */
export type IconName =
  | 'bell'
  | 'logo'
  | 'file-text'
  | 'tag'
  | 'folder'
  | 'search'
  | 'database'
  | 'users'
  | 'bar-chart'
  | 'download'
  | 'video'
  | 'music'
  | 'eye-off'
  | 'shield';

export const ICON_BY_TYPE: Partial<Record<NotificationType, IconName>> = {
  // Transcription is the app's core job, so it carries the product mark.
  transcription_status: 'logo',
  summarization_status: 'file-text',
  // Topics and tags share the tag glyph (TopicsList renders none of its own).
  topic_extraction_status: 'tag',
  auto_label_status: 'tag',
  redaction_status: 'eye-off',
  youtube_processing_status: 'video',
  playlist_processing_status: 'video',
  audio_extraction_status: 'music',
  analytics_status: 'bar-chart',
  reindex_progress: 'search',
  reindex_complete: 'search',
  reindex_stopped: 'search',
  migration_progress: 'database',
  migration_complete: 'database',
  migration_finalized: 'database',
  clustering_progress: 'users',
  clustering_complete: 'users',
  attribute_migration_progress: 'users',
  attribute_migration_complete: 'users',
  group_member_added: 'users',
  group_member_removed: 'users',
  collection_shared: 'folder',
  collection_share_revoked: 'folder',
  collection_share_updated: 'folder',
  file_takedown: 'shield',
  file_takedown_released: 'shield',
  download_progress: 'download',
};

/** Unmapped types fall back to the bell on purpose. */
export function iconFor(type: NotificationType): IconName {
  return ICON_BY_TYPE[type] ?? 'bell';
}

export type Tone = 'success' | 'error' | 'info' | 'warning' | 'default';

/**
 * Colour of a plain row. Falls back to the top-level `status` when `data.status` is absent
 * (issue #753 N2/J3): client-originated rows (audio extraction, the download bridge) set only
 * the top-level field.
 */
export function notificationTone(n: Pick<Notification, 'status' | 'data'>): Tone {
  switch (n.data?.status ?? n.status) {
    case 'completed':
      return 'success';
    case 'error':
    case 'failed':
      return 'error';
    case 'processing':
    case 'in_progress':
      return 'info';
    case 'not_configured':
    case 'warning':
      return 'warning';
    default:
      return 'default';
  }
}

/** "Just now" / "5m ago" / "3h ago" / "2d ago". */
export function formatRelativeTime(date: Date, now: number, t: Translate): string {
  const diff = Math.floor((now - new Date(date).getTime()) / 1000);
  if (diff < 60) return t('notifications.justNow');
  if (diff < 3600) return t('notifications.minutesAgo', { count: Math.floor(diff / 60) });
  if (diff < 86400) return t('notifications.hoursAgo', { count: Math.floor(diff / 3600) });
  return t('notifications.daysAgo', { count: Math.floor(diff / 86400) });
}

/** Keeps the start and the extension of a long filename so it fits one line. */
export function shortenFilename(filename: string, max = 40): string {
  if (filename.length <= max) return filename;
  const dot = filename.lastIndexOf('.');
  if (dot <= 0) return `${filename.slice(0, max - 3)}...`;
  const extension = filename.slice(dot + 1);
  const name = filename.slice(0, dot);
  const keep = max - 5;
  return name.length > keep
    ? `${name.slice(0, keep)}...${extension ? `.${extension}` : ''}`
    : filename;
}

export function fileLinkFor(fileId: string | undefined | null): string | null {
  return fileId ? `/files/${fileId}` : null;
}
