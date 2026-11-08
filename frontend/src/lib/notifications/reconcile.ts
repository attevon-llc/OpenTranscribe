/**
 * Missed-frame fallback for the processing card.
 *
 * WebSocket frames can be lost (a reload, a dropped socket, a backgrounded tab). When the
 * card model says it cannot tell what post-processing is doing (`ProcessingJobModel.reconcile`),
 * the open panel reads `GET /files/{uuid}/post-processing`, which answers from the same store
 * the frames are written to, and folds the snapshot in exactly like a plan frame. Loaded with
 * the panel body, not the root bundle.
 */
import axiosInstance from '$lib/axios';
import type { PostProcessingFrame } from './postProcessing';

/** The first read of a file waits this long; each unanswered read doubles the wait. */
export const RECONCILE_FIRST_MS = 15_000;
/** The wait between reads never grows past this, however long a card stays uncertain. */
export const RECONCILE_MAX_MS = 60_000;

/** Wait before read number `attempts + 1` of a file: 15 s, 30 s, then 60 s, however often it re-renders. */
export function reconcileDelayMs(attempts: number): number {
  return Math.min(RECONCILE_MAX_MS, RECONCILE_FIRST_MS * 2 ** Math.max(0, attempts - 1));
}

const lastAsked = new Map<string, { at: number; attempts: number }>();

function statusOf(error: unknown): number | undefined {
  const response = (error as { response?: { status?: unknown } } | null)?.response;
  return typeof response?.status === 'number' ? response.status : undefined;
}

/**
 * The server's post-processing state for one file. A file the caller can no longer read
 * (deleted, taken down, access revoked) has nothing more to report, so it answers "no run
 * known"; a network failure answers null and is retried after the throttle.
 */
export async function fetchPostProcessing(fileId: string): Promise<PostProcessingFrame | null> {
  try {
    const { data } = await axiosInstance.get<PostProcessingFrame>(
      `/files/${fileId}/post-processing`
    );
    return { ...data, file_id: fileId };
  } catch (error) {
    const status = statusOf(error);
    if (status === 403 || status === 404) return { file_id: fileId, run_at: null, tasks: {} };
    return null;
  }
}

/** Reads the state of every job that asks for it, throttled per file. */
export function reconcileJobs(
  jobs: ReadonlyArray<{ fileId: string; reconcile: boolean }>,
  now: number,
  apply: (snapshot: PostProcessingFrame) => void,
  fetcher: (fileId: string) => Promise<PostProcessingFrame | null> = fetchPostProcessing
): void {
  for (const job of jobs) {
    if (!job.reconcile) continue;
    const last = lastAsked.get(job.fileId);
    if (last !== undefined && now - last.at < reconcileDelayMs(last.attempts)) continue;
    lastAsked.set(job.fileId, { at: now, attempts: (last?.attempts ?? 0) + 1 });
    fetcher(job.fileId)
      .then((snapshot) => {
        if (snapshot) apply(snapshot);
      })
      .catch(() => {});
  }
}

/** Forget the throttle: on a session or tenant change (`clearUserState`), and in tests. */
export function resetReconcileThrottle(): void {
  lastAsked.clear();
}
