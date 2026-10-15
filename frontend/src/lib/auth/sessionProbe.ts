/**
 * Bounded, visibly-retrying session probe.
 *
 * `initAuth()`'s `GET /auth/session` gates first render. It used to ride the 60 s axios
 * default, so a stalled backend left the visitor on a blank screen for up to a minute, and
 * then (on the timeout) treated them as logged out. Now each attempt is bounded, a failed
 * attempt is NOT read as "anonymous" (a valid session must not be bounced to /login because
 * the API blinked), the UI is told the server is unreachable, and the probe keeps retrying
 * with a short backoff until it gets an answer. `retrySessionProbeNow()` skips the wait.
 */
import { writable } from 'svelte/store';

/**
 * Per-attempt ceiling. Measured against the dev backend: `/auth/session` answers in 3-13 ms
 * (p50 4 ms anonymous, 9 ms authenticated), so 8 s is ~600x the worst sample, enough for a cold
 * or loaded host (uvicorn reload, a busy gate run) without a visitor staring at nothing for the
 * old 60 s.
 */
export const SESSION_PROBE_TIMEOUT_MS = 8000;

/** Wait before retry N (the last entry repeats). Short: the failure is already 8 s old. */
const RETRY_DELAYS_MS = [1000, 2000, 3000, 5000];

export const sessionProbe = writable<{ unreachable: boolean }>({ unreachable: false });

let wake: (() => void) | null = null;

/** Skip the backoff wait and probe again immediately (the "Retry now" button). */
export function retrySessionProbeNow(): void {
  wake?.();
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      wake = null;
      resolve();
    }
    wake = done;
  });
}

/** Run `attempt` until it resolves, publishing `sessionProbe.unreachable` while it fails. */
export async function probeSessionWithRetry<T>(attempt: () => Promise<T>): Promise<T> {
  for (let n = 0; ; n++) {
    try {
      const result = await attempt();
      sessionProbe.set({ unreachable: false });
      return result;
    } catch {
      sessionProbe.set({ unreachable: true });
      await wait(RETRY_DELAYS_MS[Math.min(n, RETRY_DELAYS_MS.length - 1)]);
    }
  }
}
