/**
 * Idle and absolute session timeout guard (issue #1106). Provider-agnostic: it knows
 * nothing about how the session is held or ended — it only decides WHEN, and reports
 * transitions through `onChange`. `sessionTimeouts.ts` wires it to the auth store.
 *
 * Rules that are easy to get wrong:
 *  - Only real user input counts as activity. Background API calls, WebSocket
 *    frames and silent token refresh never touch this clock; if they did, an
 *    unattended tab would stay signed in forever.
 *  - A tab becoming visible counts as activity, but the deadline is checked FIRST.
 *    A laptop that slept past the deadline must expire on wake, not be revived by
 *    the visibilitychange its own wake-up fires.
 *  - The clock is wall time (`Date.now()`), re-evaluated on a 1 s tick, so a
 *    throttled background timer can delay the transition but never skip it.
 *  - The absolute deadline cannot be extended by activity or "stay signed in".
 *  - Tabs share one clock via BroadcastChannel: input in any tab keeps all alive,
 *    and expiry in any tab expires all of them.
 */

export type IdleReason = 'idle' | 'absolute';

export type IdleGuardState =
  | { phase: 'active' }
  | { phase: 'warning'; reason: IdleReason; deadline: number }
  | { phase: 'expired'; reason: IdleReason };

/** The subset of BroadcastChannel the guard uses (lets tests supply a fake). */
export interface BroadcastLike {
  postMessage(data: unknown): void;
  close(): void;
  onmessage: ((ev: MessageEvent) => void) | null;
}

export interface IdleGuardOptions {
  /** Inactivity limit in ms; `<= 0` disables the idle half. */
  idleTimeoutMs: number;
  /** Epoch ms at which the session ends regardless of activity; `null` disables. */
  absoluteDeadline: number | null;
  /** How long before a deadline the warning phase starts. */
  warningLeadMs: number;
  onChange: (state: IdleGuardState) => void;
  /** Source of input events. Defaults to `window`. */
  target?: EventTarget;
  /** Source of `visibilitychange`. Defaults to `document`. */
  doc?: EventTarget & { visibilityState: string };
  /** Cross-tab channel. `undefined` = open the default one; `null` = none. */
  channel?: BroadcastLike | null;
  tickMs?: number;
}

export interface IdleGuard {
  getState(): IdleGuardState;
  /** Explicit "I'm still here" (the warning dialog's button). Idle only. */
  staySignedIn(): void;
  /** Expire now (e.g. the server refused the session) and tell other tabs. */
  expire(reason: IdleReason): void;
  stop(): void;
}

/** Real user input. Deliberately excludes scroll, focus and anything network-driven. */
export const ACTIVITY_EVENTS = [
  'pointerdown',
  'pointermove',
  'keydown',
  'touchstart',
  'wheel',
] as const;

export const IDLE_CHANNEL_NAME = 'opentranscribe-session-idle';

/** Cross-tab activity is advisory; one message per this interval is plenty. */
const ACTIVITY_BROADCAST_MS = 5_000;

type ChannelMessage = { type: 'activity'; at: number } | { type: 'expired'; reason: IdleReason };

function isChannelMessage(data: unknown): data is ChannelMessage {
  if (!data || typeof data !== 'object') return false;
  const msg = data as Record<string, unknown>;
  if (msg.type === 'activity') return typeof msg.at === 'number' && Number.isFinite(msg.at);
  if (msg.type === 'expired') return msg.reason === 'idle' || msg.reason === 'absolute';
  return false;
}

function openDefaultChannel(): BroadcastLike | null {
  if (typeof BroadcastChannel === 'undefined') return null;
  try {
    return new BroadcastChannel(IDLE_CHANNEL_NAME);
  } catch {
    return null;
  }
}

function sameState(a: IdleGuardState, b: IdleGuardState): boolean {
  if (a.phase !== b.phase) return false;
  if (a.phase === 'active' || b.phase === 'active') return true;
  if (a.reason !== b.reason) return false;
  if (a.phase === 'warning' && b.phase === 'warning') return a.deadline === b.deadline;
  return true;
}

export function createIdleGuard(options: IdleGuardOptions): IdleGuard {
  const target = options.target ?? window;
  const doc = options.doc ?? document;
  const channel = options.channel === undefined ? openDefaultChannel() : options.channel;
  const idleMs = options.idleTimeoutMs > 0 ? options.idleTimeoutMs : 0;
  const absoluteDeadline = options.absoluteDeadline;

  let lastActivity = Date.now();
  let lastBroadcast = 0;
  let state: IdleGuardState = { phase: 'active' };
  let stopped = false;

  function setState(next: IdleGuardState): void {
    if (sameState(state, next)) return;
    state = next;
    options.onChange(next);
  }

  function evaluate(now = Date.now()): void {
    if (stopped || state.phase === 'expired') return;
    const idleDeadline = idleMs > 0 ? lastActivity + idleMs : Infinity;
    const absDeadline = absoluteDeadline ?? Infinity;

    if (now >= absDeadline) return expireInternal('absolute', true);
    if (now >= idleDeadline) return expireInternal('idle', true);

    const reason: IdleReason = absDeadline <= idleDeadline ? 'absolute' : 'idle';
    const deadline = Math.min(absDeadline, idleDeadline);
    if (Number.isFinite(deadline) && now >= deadline - options.warningLeadMs) {
      setState({ phase: 'warning', reason, deadline });
    } else {
      setState({ phase: 'active' });
    }
  }

  function recordActivity(at: number, broadcast: boolean, force = false): void {
    if (stopped || state.phase === 'expired') return;
    lastActivity = Math.max(lastActivity, at);
    if (broadcast && channel && (force || at - lastBroadcast >= ACTIVITY_BROADCAST_MS)) {
      lastBroadcast = at;
      channel.postMessage({ type: 'activity', at } satisfies ChannelMessage);
    }
    evaluate(at);
  }

  function expireInternal(reason: IdleReason, broadcast: boolean): void {
    if (stopped || state.phase === 'expired') return;
    teardownListeners();
    setState({ phase: 'expired', reason });
    if (broadcast && channel)
      channel.postMessage({ type: 'expired', reason } satisfies ChannelMessage);
  }

  const onInput = (): void => {
    const now = Date.now();
    // Check the deadline before crediting the input: see the module comment.
    evaluate(now);
    recordActivity(now, true);
  };

  const onVisibility = (): void => {
    if (doc.visibilityState !== 'visible') return;
    onInput();
  };

  const onMessage = (ev: MessageEvent): void => {
    if (!isChannelMessage(ev.data)) return;
    if (ev.data.type === 'activity') recordActivity(ev.data.at, false);
    else expireInternal(ev.data.reason, false);
  };

  for (const type of ACTIVITY_EVENTS) {
    target.addEventListener(type, onInput, { passive: true, capture: true });
  }
  doc.addEventListener('visibilitychange', onVisibility);
  if (channel) channel.onmessage = onMessage;
  const tick = setInterval(() => evaluate(), options.tickMs ?? 1_000);

  let listenersAttached = true;
  function teardownListeners(): void {
    if (!listenersAttached) return;
    listenersAttached = false;
    clearInterval(tick);
    for (const type of ACTIVITY_EVENTS) {
      target.removeEventListener(type, onInput, { capture: true });
    }
    doc.removeEventListener('visibilitychange', onVisibility);
  }

  evaluate();

  return {
    getState: () => state,
    staySignedIn: () => recordActivity(Date.now(), true, true),
    expire: (reason) => expireInternal(reason, true),
    stop: () => {
      teardownListeners();
      stopped = true;
      if (channel) {
        channel.onmessage = null;
        channel.close();
      }
    },
  };
}
