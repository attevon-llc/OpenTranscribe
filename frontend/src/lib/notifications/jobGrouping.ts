/**
 * Which notification records belong to one file's processing card.
 *
 * Kept apart from the card model (`processingJob.ts`) because the bell badge
 * (`stores/websocket.ts`) and retention need only this, and both sit in the root layout's
 * static bundle; the model loads with the panel. Design:
 * `docs/design/consolidated_processing_notification_plan.md`.
 */
import type { Notification, NotificationType } from '$stores/websocket';

export type MemberTask =
  | 'import'
  | 'transcription'
  | 'redaction'
  | 'summary'
  | 'topics'
  | 'postProcessing';

/** Event types that fold into a file's card. `auto_label_status` is batch-scoped, so it is not. */
export const JOB_TASK_BY_TYPE: Partial<Record<NotificationType, MemberTask>> = {
  youtube_processing_status: 'import',
  transcription_status: 'transcription',
  redaction_status: 'redaction',
  summarization_status: 'summary',
  topic_extraction_status: 'topics',
  post_processing_status: 'postProcessing',
};

export type JobItem = { kind: 'job'; key: string; fileId: string; members: Notification[] };
export type PanelItem = JobItem | { kind: 'single'; key: string; notification: Notification };

function fileIdOf(n: Notification): string | null {
  const id = n.data?.file_id;
  if (typeof id === 'string' && id) return id;
  if (typeof id === 'number') return String(id);
  return null;
}

/** The card a record belongs to, or null when it renders as a row of its own. */
export function jobKeyOf(n: Notification): string | null {
  if (n.silent || !JOB_TASK_BY_TYPE[n.type]) return null;
  const fileId = fileIdOf(n);
  return fileId ? `job:${fileId}` : null;
}

/** Visible panel items, newest first (a card sits where its newest record sits). */
export function groupPanelItems(list: Notification[]): PanelItem[] {
  const items: PanelItem[] = [];
  const jobs = new Map<string, JobItem>();
  for (const n of list) {
    if (n.silent) continue;
    const key = jobKeyOf(n);
    const fileId = key ? fileIdOf(n) : null;
    if (!key || !fileId) {
      items.push({ kind: 'single', key: n.id, notification: n });
      continue;
    }
    const existing = jobs.get(key);
    if (existing) {
      existing.members.push(n);
    } else {
      const item: JobItem = { kind: 'job', key, fileId, members: [n] };
      jobs.set(key, item);
      items.push(item);
    }
  }
  return items;
}

/** Bell badge: a card counts once however many of its records are unread. */
export function countUnreadItems(list: Notification[]): number {
  return groupPanelItems(list).filter((item) =>
    item.kind === 'job' ? item.members.some((m) => !m.read) : !item.notification.read
  ).length;
}
