/**
 * Per-file post-processing state: the reducer behind the card's "Finishing up" / "Complete".
 *
 * The backend reports every task that runs after the transcript is committed through ONE
 * event, `post_processing_status` (`backend/app/services/post_processing_status.py`):
 * a frame with `run_at` is a plan that opens a new run (each dispatch decision, queued or
 * skipped), a frame without it is one task's state. `GET /files/{uuid}/post-processing`
 * returns the same shape, so a snapshot read after a missed frame merges exactly like a plan.
 *
 * Ordering is by SERVER timestamp, never by arrival: last write wins per task, and a plan
 * drops every entry older than its `run_at` (the previous run). Pure; `stores/websocket.ts`
 * keeps the result on a `post_processing_status` record that the file's card folds in.
 * Kept small on purpose: it sits in the root bundle with the store.
 */
import type { Notification, NotificationData } from '$stores/websocket';

export type PostProcessingWireState = 'queued' | 'started' | 'done' | 'failed' | 'skipped';

export interface PostProcessingEntry {
  state: PostProcessingWireState;
  /** Server epoch seconds of this state. */
  at: number;
  /** Server epoch seconds the task started, when known. */
  startedAt?: number;
}

export interface PostProcessingRun {
  /** Server epoch seconds the current run's plan was opened; null until a plan is seen. */
  runAt: number | null;
  tasks: Record<string, PostProcessingEntry>;
  /** Client epoch ms of the last frame or snapshot applied (staleness clock). */
  updatedAt: number;
  /** The server said it knows no run for this file (expired, or never reported). */
  unknown?: boolean;
}

export interface PostProcessingFrame {
  file_id?: string;
  run_at?: number | null;
  tasks?: Record<string, { state?: string; at?: number; started_at?: number | null }>;
}

const STATES = new Set<string>(['queued', 'started', 'done', 'failed', 'skipped']);

export const POST_PROCESSING_TYPE = 'post_processing_status';

/** Folds one frame (or REST snapshot) into a file's run. Never mutates `prev`. */
export function mergePostProcessing(
  prev: PostProcessingRun | undefined,
  frame: PostProcessingFrame,
  now: number
): PostProcessingRun {
  const isPlan = typeof frame.run_at === 'number';
  if (isPlan && prev?.runAt != null && (frame.run_at as number) < prev.runAt) {
    return prev;
  }
  const runAt = isPlan ? (frame.run_at as number) : prev?.runAt ?? null;
  const tasks: Record<string, PostProcessingEntry> = {};
  for (const [key, entry] of Object.entries(prev?.tasks ?? {})) {
    if (runAt === null || entry.at >= runAt) tasks[key] = entry;
  }
  for (const [key, raw] of Object.entries(frame.tasks ?? {})) {
    if (!raw || typeof raw.state !== 'string' || !STATES.has(raw.state)) continue;
    if (typeof raw.at !== 'number') continue;
    if (runAt !== null && raw.at < runAt) continue;
    const old = tasks[key];
    if (old && raw.at < old.at) continue;
    const state = raw.state as PostProcessingWireState;
    const startedAt =
      typeof raw.started_at === 'number'
        ? raw.started_at
        : state === 'started'
          ? raw.at
          : old?.startedAt;
    tasks[key] = startedAt === undefined ? { state, at: raw.at } : { state, at: raw.at, startedAt };
  }
  const empty = Object.keys(tasks).length === 0;
  return {
    runAt,
    tasks,
    updatedAt: now,
    unknown: frame.run_at === null && runAt === null && empty ? true : undefined,
  };
}

/** True while a known task is queued or running, or while no plan has been seen yet. */
export function postProcessingOutstanding(run: PostProcessingRun): boolean {
  const entries = Object.values(run.tasks);
  if (entries.some((e) => e.state === 'queued' || e.state === 'started')) return true;
  return run.runAt === null && !run.unknown && entries.length === 0;
}

/**
 * Applies a frame to the notification list: updates the file's `post_processing_status`
 * record, or creates it when the file already has a transcription record (a frame for a file
 * with no card at all, e.g. an admin reindex of an old file, is dropped). Returns null when
 * the list is unchanged. `force` creates the record regardless (a REST snapshot the card
 * asked for).
 */
export function upsertPostProcessingRecord(
  list: Notification[],
  frame: PostProcessingFrame,
  now: number,
  newId: () => string,
  title: string,
  force = false
): Notification[] | null {
  const fileId = frame.file_id;
  if (!fileId) return null;
  const progressId = `${POST_PROCESSING_TYPE}_${fileId}`;
  const index = list.findIndex((n) => n.progressId === progressId);
  const existing = index === -1 ? undefined : list[index];
  if (!existing && !force && !list.some((n) => n.progressId === `transcription_status_${fileId}`)) {
    return null;
  }

  const run = mergePostProcessing(existing?.postProcessing, frame, now);
  const outstanding = postProcessingOutstanding(run);
  const failed = Object.values(run.tasks).some((e) => e.state === 'failed');
  const status: Notification['status'] = outstanding
    ? 'processing'
    : failed
      ? 'error'
      : 'completed';
  const wasOutstanding = existing?.status === 'processing';
  // Becoming finished (or failed) is news; every intermediate frame is not.
  const read = existing ? existing.read && !(wasOutstanding && !outstanding) : false;
  const data: NotificationData = { file_id: fileId, status };
  const record: Notification = {
    ...(existing ?? {
      id: newId(),
      type: POST_PROCESSING_TYPE,
      title,
      progressId,
    }),
    message: '',
    timestamp: new Date(now),
    read,
    readAt: read ? existing?.readAt : undefined,
    data,
    status,
    // A finishing card can always be dismissed: nothing actionable is lost, and a lost
    // frame must never trap a card on screen.
    dismissible: true,
    postProcessing: run,
  };
  const rest = index === -1 ? list : [...list.slice(0, index), ...list.slice(index + 1)];
  return [record, ...rest];
}
