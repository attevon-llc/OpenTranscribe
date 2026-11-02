import { writable, type Readable } from 'svelte/store';

/**
 * Counts renames whose per-file digest regeneration is still running server-side.
 *
 * The backend reports completion with one `speaker-rename-propagation` message per batch and
 * sends no "started" frame, so the count is bumped optimistically by `begin()` right after the
 * triggering action succeeds and dropped by `settle()` (completion event) or by a timeout, so a
 * stopped worker cannot leave the indicator stuck.
 */
export function createRenamePropagationTracker(timeoutMs: number): {
  pending: Readable<number>;
  begin: () => void;
  settle: () => void;
  dispose: () => void;
} {
  const pending = writable(0);
  let timers: ReturnType<typeof setTimeout>[] = [];
  const decrement = () => pending.update((n) => Math.max(0, n - 1));

  return {
    pending: { subscribe: pending.subscribe },
    begin() {
      pending.update((n) => n + 1);
      timers.push(setTimeout(decrement, timeoutMs));
    },
    settle() {
      const timer = timers.shift();
      if (timer) clearTimeout(timer);
      decrement();
    },
    dispose() {
      timers.forEach(clearTimeout);
      timers = [];
    },
  };
}
