/**
 * A fixed-cadence poll-and-load timer.
 *
 * Replaces hand-rolled `setInterval(() => void load(true), MS)` plus a matching `clearInterval`
 * in `onDestroy`. Differences from the bare interval, all on purpose:
 *
 * - Ticks never overlap: a tick that fires while the previous async run is still pending is
 *   skipped, so a slow server is not stacked with requests.
 * - A rejecting or throwing callback is reported (`onError`, default `console.error`) and the
 *   loop keeps going.
 * - Polling pauses while the document is hidden and, on becoming visible again, catches up with
 *   one immediate run (`runOnResume`, default true) and restarts the cadence from then.
 * - `start()` and `stop()` are idempotent, so `stop()` is safe to call from `onDestroy` and from
 *   inside the callback itself.
 *
 * `start()` does not run the callback; callers do their own initial load.
 */
export interface PollerOptions {
  intervalMs: number;
  /** Pause while `document.hidden`. Default true. */
  pauseWhenHidden?: boolean;
  /** Run once immediately when the document becomes visible after a pause. Default true. */
  runOnResume?: boolean;
  onError?: (error: unknown) => void;
}

export interface Poller {
  start(): void;
  stop(): void;
  /** True between `start()` and `stop()`, including while paused for a hidden document. */
  readonly active: boolean;
}

export function createPoller(callback: () => unknown, options: PollerOptions): Poller {
  const { intervalMs, pauseWhenHidden = true, runOnResume = true } = options;
  const onError = options.onError ?? ((e: unknown) => console.error('Poll failed:', e));
  const hasDocument = typeof document !== 'undefined';

  let active = false;
  let timer: ReturnType<typeof setInterval> | null = null;
  let inFlight = false;

  const cancelTimer = () => {
    if (timer !== null) clearInterval(timer);
    timer = null;
  };

  async function run() {
    if (inFlight) return;
    inFlight = true;
    try {
      await callback();
    } catch (error) {
      onError(error);
    } finally {
      inFlight = false;
    }
  }

  const isHidden = () => pauseWhenHidden && hasDocument && document.hidden;

  function schedule() {
    cancelTimer();
    timer = setInterval(() => void run(), intervalMs);
  }

  function onVisibilityChange() {
    if (!active) return;
    if (isHidden()) {
      cancelTimer();
      return;
    }
    if (timer === null) {
      schedule();
      if (runOnResume) void run();
    }
  }

  return {
    get active() {
      return active;
    },
    start() {
      if (active) return;
      active = true;
      if (pauseWhenHidden && hasDocument) {
        document.addEventListener('visibilitychange', onVisibilityChange);
      }
      if (!isHidden()) schedule();
    },
    stop() {
      if (!active) return;
      active = false;
      cancelTimer();
      if (hasDocument) document.removeEventListener('visibilitychange', onVisibilityChange);
    },
  };
}
