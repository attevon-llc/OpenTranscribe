/**
 * Run background tasks one idle slice at a time with a small concurrency cap.
 *
 * Each task starts from its own `requestIdleCallback` (a short `setTimeout` where Safari lacks
 * it), so a busy page is never competed with for more than `concurrency` in-flight tasks. A task
 * that rejects is dropped: prefetching is best effort and the real request repeats the failure
 * where the user can see it. Returns a cancel function; tasks not yet started never start.
 */
export function runIdleQueue(
  tasks: Array<() => Promise<unknown> | void>,
  concurrency = 2
): () => void {
  const queue = [...tasks];
  let cancelled = false;
  let inFlight = 0;
  let pending: { id: number; idle: boolean } | null = null;

  const schedule = () => {
    if (cancelled || pending || queue.length === 0 || typeof window === 'undefined') return;
    const run = () => {
      pending = null;
      pump();
    };
    pending = window.requestIdleCallback
      ? { id: window.requestIdleCallback(run, { timeout: 2000 }), idle: true }
      : { id: window.setTimeout(run, 250), idle: false };
  };

  function pump() {
    if (cancelled || inFlight >= concurrency) return;
    const task = queue.shift();
    if (!task) return;
    inFlight += 1;
    void Promise.resolve()
      .then(task)
      .catch(() => {})
      .finally(() => {
        inFlight -= 1;
        schedule();
      });
    schedule();
  }

  schedule();
  return () => {
    cancelled = true;
    queue.length = 0;
    if (!pending) return;
    if (pending.idle) window.cancelIdleCallback?.(pending.id);
    else window.clearTimeout(pending.id);
  };
}
