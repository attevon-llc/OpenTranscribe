/**
 * Timers owned by the file-detail page.
 *
 * Every deferred callback on that page touches the DOM (`document.querySelector`, a
 * `scrollIntoView`, a `classList` mutation). An uncancelled one keeps running
 * against a page the user has already navigated away from: in the browser it
 * scrolls or restyles a detached node, and under jsdom the environment is torn
 * down first, so the callback throws `ReferenceError: document is not defined`
 * as an *unhandled* error — an intermittent red run with no failing test.
 *
 * Schedule through `schedule` rather than calling `setTimeout` directly: it refuses
 * to schedule once `destroy()` has run (a late-resolving fetch can still reach the
 * page's code after destroy), and `destroy()` cancels whatever is still pending.
 */
export interface PageTimers {
  /** `setTimeout` that is a no-op after `destroy()` and is cancelled by it. */
  schedule: (callback: () => void, delayMs: number) => void;
  /** Cancel every pending callback and refuse any later `schedule` call. */
  destroy: () => void;
}

export function createPageTimers(): PageTimers {
  let destroyed = false;
  let pending: ReturnType<typeof setTimeout>[] = [];

  function schedule(callback: () => void, delayMs: number): void {
    if (destroyed) return;
    const handle = setTimeout(() => {
      pending = pending.filter((entry) => entry !== handle);
      if (destroyed) return;
      callback();
    }, delayMs);
    pending.push(handle);
  }

  function destroy(): void {
    destroyed = true;
    for (const handle of pending) {
      clearTimeout(handle);
    }
    pending = [];
  }

  return { schedule, destroy };
}
