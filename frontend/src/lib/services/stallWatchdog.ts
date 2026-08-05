/**
 * Stall watchdog for long-running body uploads.
 *
 * A *total-request* timeout is the wrong control for a whole-file PUT: it is a
 * deadline on the upload's duration, so it fails healthy uploads purely for
 * being big. With the app's 15 GB limit and a 5-minute cap, anything slower than
 * ~50 MB/s was guaranteed to fail (then retry and fail again). What we actually
 * want to detect is a *dead* connection, which means: no bytes moved for a while.
 *
 * Two phases, because they fail differently:
 *  - **transfer** — bytes are still being written to the socket. Every progress
 *    tick that advances `loaded` re-arms the timer.
 *  - **finalizing** — the whole body has been sent and we are waiting on the
 *    server's response. No further progress events will ever fire here, so the
 *    transfer timeout must not apply: the object store still has to write and
 *    checksum the upload (minutes, for a multi-GB file). A separate, longer
 *    ceiling covers a genuinely hung server.
 *
 * Not a singleton — one watchdog per in-flight request (see this folder's
 * CLAUDE.md; the "one shared instance" rule covers the service objects, not this
 * per-request factory).
 */

/** No bytes moved for this long during transfer → the connection is dead. */
export const DEFAULT_STALL_TIMEOUT_MS = 120_000; // 2 minutes

/** Body fully sent; server has this long to answer before we give up. */
export const DEFAULT_FINALIZE_TIMEOUT_MS = 900_000; // 15 minutes

export interface StallWatchdogOptions {
  /** Idle time tolerated while bytes are still in flight. */
  stallTimeoutMs?: number;
  /** Idle time tolerated after the body is fully sent. */
  finalizeTimeoutMs?: number;
}

export interface StallWatchdog {
  /** Hand to axios/fetch as `signal`; aborts when the transfer goes quiet. */
  readonly signal: AbortSignal;
  /** True once this watchdog aborted the request (vs. a user cancellation). */
  readonly stalled: boolean;
  /** Feed on every progress tick. `total` omitted → still in the transfer phase. */
  notifyProgress(loaded: number, total?: number): void;
  /** Stop the timer. Always call from a `finally`. */
  dispose(): void;
}

/**
 * Create a watchdog that aborts a request only when its byte stream goes quiet.
 *
 * @param options - Phase timeouts; both default to the constants above.
 * @returns A watchdog whose `signal` fires on stall and never on slowness alone.
 */
export function createStallWatchdog(options: StallWatchdogOptions = {}): StallWatchdog {
  const stallTimeoutMs = options.stallTimeoutMs ?? DEFAULT_STALL_TIMEOUT_MS;
  const finalizeTimeoutMs = options.finalizeTimeoutMs ?? DEFAULT_FINALIZE_TIMEOUT_MS;

  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | null = null;
  let lastLoaded = -1;
  let finalizing = false;
  let stalled = false;
  let disposed = false;

  function clear() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function arm(delayMs: number) {
    clear();
    if (disposed) return;
    timer = setTimeout(() => {
      timer = null;
      stalled = true;
      controller.abort();
    }, delayMs);
  }

  // Armed from the start: a connection that never delivers its first byte is
  // stalled too.
  arm(stallTimeoutMs);

  return {
    signal: controller.signal,

    get stalled() {
      return stalled;
    },

    notifyProgress(loaded: number, total?: number) {
      if (disposed || stalled) return;

      // Body fully sent — switch to the response-wait ceiling, once.
      if (total !== undefined && total > 0 && loaded >= total) {
        if (!finalizing) {
          finalizing = true;
          arm(finalizeTimeoutMs);
        }
        return;
      }

      // Only real forward movement counts; a repeated `loaded` is not progress.
      if (loaded > lastLoaded) {
        lastLoaded = loaded;
        arm(stallTimeoutMs);
      }
    },

    dispose() {
      disposed = true;
      clear();
    },
  };
}
