/**
 * One cookie-session refresh at a time, across every tab of this browser.
 *
 * The refresh token is an httpOnly cookie shared by all tabs, and the server spends it
 * exactly once: a refresh token that was already rotated and comes back is treated as
 * reuse (RFC 9700 §4.14.2) and ends the whole session. Tabs whose access tokens expire
 * together (they share the access cookie too) would otherwise each POST
 * `/auth/token/refresh` with the same cookie, and the loser would sign everyone out.
 *
 * - **Web Locks** (`navigator.locks`, MDN "Web Locks API"): every refresh runs inside
 *   the `opentr-auth-refresh` lock, so only one tab's refresh is ever in flight. The
 *   cookie is set when the refresh response arrives, which is before the lock is
 *   released, so a tab that waited sends the already-rotated cookie automatically.
 * - **A tab that had to wait checks first**: the winner broadcasts on a
 *   `BroadcastChannel`, and the waiting tab also re-probes `GET /auth/session` (a message
 *   is not ordered against the lock grant). Either says "done" -> no second refresh. A
 *   tab that gets the lock at once (`ifAvailable`) refreshes with no extra request.
 * - **Fallback** (no Web Locks): one in-flight refresh per tab, preceded by a short
 *   random delay and a check for another tab's broadcast. It narrows the cross-tab
 *   window rather than closing it; every browser the app supports has Web Locks.
 */

export const REFRESH_LOCK_NAME = 'opentr-auth-refresh';
export const REFRESH_CHANNEL_NAME = 'opentr-auth-refresh';
/** Upper bound of the fallback's random delay before refreshing, in ms. */
export const FALLBACK_JITTER_MS = 250;

/** The refresh lock, adapted from `navigator.locks` (lets tests supply a shared fake). */
export interface LockLike {
  /** Run `callback` holding the lock only if it is free right now; `false` if it was not. */
  tryRun(callback: () => Promise<RefreshOutcome>): Promise<RefreshOutcome | false>;
  /** Wait for the lock, then run `callback` holding it. */
  run(callback: () => Promise<RefreshOutcome>): Promise<RefreshOutcome>;
}

/** The subset of `BroadcastChannel` used here. */
export interface ChannelLike {
  postMessage(message: unknown): void;
  addEventListener(type: 'message', listener: (event: { data: unknown }) => void): void;
}

export interface RefreshDeps {
  /** POST /auth/token/refresh; rejects when the server refuses. */
  refresh: (options: RefreshOptions) => Promise<void>;
  /** Whether the current access cookie is already valid (GET /auth/session). */
  sessionIsValid: () => Promise<boolean>;
  locks?: LockLike | null;
  channel?: ChannelLike | null;
  now?: () => number;
  random?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

export interface RefreshOptions {
  /** Request timeout for the refresh this call starts (a joined refresh keeps its own). */
  timeoutMs?: number;
}

/** What a call to `refresh()` did: refreshed itself, or found the session already fresh. */
export type RefreshOutcome = 'refreshed' | 'skipped';

interface RefreshedMessage {
  type: 'refreshed';
  at: number;
}

function isRefreshedMessage(data: unknown): data is RefreshedMessage {
  return (
    typeof data === 'object' &&
    data !== null &&
    (data as { type?: unknown }).type === 'refreshed' &&
    typeof (data as { at?: unknown }).at === 'number'
  );
}

export function createSessionRefresher(
  deps: RefreshDeps
): (options?: RefreshOptions) => Promise<RefreshOutcome> {
  const now = deps.now ?? (() => Date.now());
  const random = deps.random ?? Math.random;
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const locks = deps.locks ?? null;
  const channel = deps.channel ?? null;

  let lastRefreshedAt = -Infinity;
  let inFlight: Promise<RefreshOutcome> | null = null;

  channel?.addEventListener('message', (event) => {
    if (isRefreshedMessage(event.data)) {
      lastRefreshedAt = Math.max(lastRefreshedAt, event.data.at);
    }
  });

  async function alreadyRefreshedSince(requestedAt: number, probe: boolean): Promise<boolean> {
    if (lastRefreshedAt >= requestedAt) return true;
    if (!probe) return false;
    try {
      return await deps.sessionIsValid();
    } catch {
      return false;
    }
  }

  async function refreshOnce(
    requestedAt: number,
    options: RefreshOptions,
    probe: boolean
  ): Promise<RefreshOutcome> {
    if (await alreadyRefreshedSince(requestedAt, probe)) return 'skipped';
    await deps.refresh(options);
    const at = now();
    lastRefreshedAt = Math.max(lastRefreshedAt, at);
    const message: RefreshedMessage = { type: 'refreshed', at };
    channel?.postMessage(message);
    return 'refreshed';
  }

  async function run(options: RefreshOptions): Promise<RefreshOutcome> {
    const requestedAt = now();
    if (locks) {
      const uncontended = await locks.tryRun(() => refreshOnce(requestedAt, options, false));
      if (uncontended !== false) return uncontended;
      return locks.run(() => refreshOnce(requestedAt, options, true));
    }
    await sleep(Math.floor(random() * FALLBACK_JITTER_MS));
    return refreshOnce(requestedAt, options, false);
  }

  return (options: RefreshOptions = {}) => {
    // One refresh per tab at a time; concurrent callers in this tab share it.
    inFlight ??= run(options).finally(() => {
      inFlight = null;
    });
    return inFlight;
  };
}

function browserLocks(): LockLike | null {
  if (typeof navigator === 'undefined') return null;
  const locks = (navigator as Navigator & { locks?: LockManager }).locks;
  if (!locks || typeof locks.request !== 'function') return null;
  return {
    tryRun: (callback) =>
      locks.request(REFRESH_LOCK_NAME, { ifAvailable: true }, (lock) =>
        lock ? callback() : Promise.resolve(false as const)
      ),
    run: (callback) => locks.request(REFRESH_LOCK_NAME, () => callback()),
  };
}

function browserChannel(): ChannelLike | null {
  if (typeof BroadcastChannel === 'undefined') return null;
  try {
    return new BroadcastChannel(REFRESH_CHANNEL_NAME);
  } catch {
    return null;
  }
}

let shared: ((options?: RefreshOptions) => Promise<RefreshOutcome>) | null = null;

/**
 * Refresh the cookie session, coordinated with every other tab.
 *
 * The only way the SPA calls `POST /auth/token/refresh`: the axios 401 handler, the
 * boot-time session restore and the chat stream's retry all go through here.
 */
export async function refreshSession(options: RefreshOptions = {}): Promise<RefreshOutcome> {
  if (!shared) {
    const { default: axiosInstance } = await import('$lib/axios');
    shared = createSessionRefresher({
      refresh: async ({ timeoutMs }) => {
        if (timeoutMs) {
          await axiosInstance.post('/auth/token/refresh', {}, { timeout: timeoutMs });
        } else {
          await axiosInstance.post('/auth/token/refresh', {});
        }
      },
      sessionIsValid: async () => {
        const { data } = await axiosInstance.get('/auth/session');
        return Boolean(data?.authenticated);
      },
      locks: browserLocks(),
      channel: browserChannel(),
    });
  }
  return shared(options);
}
